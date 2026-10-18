"""Stream documents to the selected tablet's native importer over SSH."""

from contextlib import ExitStack, contextmanager
import http.client
import ipaddress
import os
from pathlib import Path
import secrets
import shlex
import stat
import threading
import time

from _device_storage import df_free_bytes


_CONTROL_STATE = (
    "if [ -e /run/rmtool-native-import-control/active ] || "
    "[ -L /run/rmtool-native-import-control/active ] || "
    "[ -e /home/root/.rmtool-native-import-web-state ] || "
    "[ -L /home/root/.rmtool-native-import-web-state ] || "
    "[ -e /run/rmtool-qr-upload-ui ] || [ -L /run/rmtool-qr-upload-ui ] || "
    "[ -e /home/root/.rmtool-qr-upload-web-state ] || [ -L /home/root/.rmtool-qr-upload-web-state ]; "
    "then echo busy; else echo free; fi"
)
_NATIVE_ADDRESS_STATE = "ip -4 addr show"


class _NativeHTTPConnection(http.client.HTTPConnection):
    def __init__(self, transport, *, timeout=120):
        super().__init__("10.11.99.1", 80, timeout=timeout)
        self._transport = transport

    def connect(self):
        # Never route via the PC's USB address, which may belong to another tablet.
        self.sock = self._transport.open_channel(
            "direct-tcpip", (self.host, self.port), ("127.0.0.1", 0), timeout=10
        )
        self.sock.settimeout(self.timeout)


class NativeDocumentImporter:
    def __init__(self, ssh_client, document_root, client):
        self.ssh_client = ssh_client
        self.document_root = document_root
        self.client = client

    def transfer_batch(self, file_paths, progress_callback=None, status_callback=None):
        if not file_paths:
            return 0
        with ExitStack() as files, self.ssh_client.operation_session():
            if self.ssh_client.ensure_client() is not self.client:
                raise RuntimeError("设备连接已变化，未上传文档，请重新选择文件。")
            prepared = []
            for path in file_paths:
                name = Path(path).name.strip()
                if (Path(name).suffix.lower() not in {".pdf", ".epub"}
                        or any(ord(c) < 32 or ord(c) == 127 or c in '/\\' for c in name)):
                    raise RuntimeError("仅支持文件名有效的 PDF 或 EPUB 文件。")
                source = files.enter_context(open(path, "rb"))
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                    raise RuntimeError(f"文件为空或不是普通文件：{name}")
                prepared.append((name, source, info))
            total = sum(info.st_size for _, _, info in prepared)
            transport = self.client.get_transport()
            self._check_space(total + max(info.st_size for _, _, info in prepared))
            files.enter_context(self._native_session(transport, status_callback))
            sent = accepted = 0
            if progress_callback:
                progress_callback(0, total)
            for name, source, info in prepared:
                submitted = False
                connection = _NativeHTTPConnection(transport)
                try:
                    self._check_space(2 * info.st_size)
                    if not self._native_address_present():
                        raise RuntimeError("设备原生导入地址已变化，未发送当前文件。")
                    if self._signature(os.fstat(source.fileno())) != self._signature(info):
                        raise RuntimeError("本地文件已变化，请重新选择。")
                    boundary = "rmtool-" + secrets.token_hex(24)
                    escaped = name.replace('"', '\\"')
                    prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                              f'filename="{escaped}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode("utf-8")
                    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
                    if status_callback:
                        status_callback(f"正在发送 {accepted + 1}/{len(prepared)}：{name}")
                    connection.putrequest("POST", "/upload")
                    connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
                    connection.putheader("Content-Length", str(len(prefix) + info.st_size + len(suffix)))
                    connection.putheader("Connection", "close")
                    connection.endheaders()
                    submitted = True
                    connection.send(prefix)
                    remaining = info.st_size
                    while remaining:
                        chunk = source.read(min(64 * 1024, remaining))
                        if not chunk:
                            raise RuntimeError("读取文件时内容被截断。")
                        connection.send(chunk)
                        remaining -= len(chunk)
                        sent += len(chunk)
                        if progress_callback:
                            progress_callback(min(sent, total - 1), total)
                    if self._signature(os.fstat(source.fileno())) != self._signature(info):
                        raise RuntimeError("发送期间本地文件已变化。")
                    connection.send(suffix)
                    if status_callback:
                        status_callback(f"等待设备确认导入 {accepted + 1}/{len(prepared)}：{name}")
                    response = connection.getresponse()
                    try:
                        if response.status not in (200, 201):
                            raise RuntimeError(f"原生导入接口返回 HTTP {response.status}。")
                    finally:
                        response.close()
                    accepted += 1
                except Exception as exc:
                    detail = ("当前文件的导入结果未确认，请先检查设备资料库再决定是否重传。"
                              if submitted else "当前文件未发送。")
                    raise RuntimeError(
                        f"已确认导入 {accepted}/{len(prepared)} 个文档；{name} 未完成。"
                        f"{detail}后续文件未发送，已导入文件保留。\n{exc}"
                    ) from exc
                finally:
                    connection.close()
        if progress_callback:
            progress_callback(total, total)
        return accepted

    @contextmanager
    def _native_session(self, transport, status_callback):
        state = self.ssh_client.exec_checked(_CONTROL_STATE).strip()
        if state != "free":
            raise RuntimeError("设备已有传输会话或待恢复设置，请先停止扫码接收或电脑上传；异常退出后请重启设备恢复设置。")
        if self._native_address_present():
            try:
                self._check_ready(transport)
            except RuntimeError:
                pass
            else:
                yield
                return
        import _native_import_control as control
        helper = control.loaded_helper(self.ssh_client)
        if helper is None:
            raise RuntimeError(
                "设备自动传输组件尚未就绪，请在「文档中心 > 原生传输服务」初始化或更新组件后"
                "手动重启设备一次；也可手动开启设备原生 USB 网页界面。"
            )
        if status_callback:
            status_callback("正在准备设备原生导入服务…")
        channel = transport.open_session(timeout=10)
        # Older ARM devices need longer to hash the exact firmware binary.
        channel.settimeout(30)
        stop = threading.Event()
        heartbeat = None
        reply = None
        acquired = False
        primary_error = None
        try:
            channel.exec_command(f"{shlex.quote(helper)} --desktop-native-lease")
            reply = channel.makefile("rb")
            if reply.readline(65) != b"ready\n":
                raise RuntimeError("无法准备原生导入服务，请解锁设备、停止扫码接收，并在文档中心初始化或更新原生传输组件。")
            acquired = True
            channel.settimeout(5)

            def keep_alive():
                while not stop.wait(2):
                    try:
                        channel.sendall(b"keep\n")
                    except Exception:
                        return

            heartbeat = threading.Thread(target=keep_alive, daemon=True)
            heartbeat.start()
            for attempt in range(10):
                try:
                    if not self._native_address_present():
                        raise RuntimeError("无法确认设备本机的原生导入地址，未上传任何文件。")
                    self._check_ready(transport)
                    break
                except RuntimeError:
                    if attempt == 9:
                        raise
                    time.sleep(0.3)
            yield
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            stop.set()
            if heartbeat:
                heartbeat.join(6)
            try:
                if acquired:
                    if status_callback:
                        status_callback("正在恢复设备原设置…")
                    channel.sendall(b"release\n")
                    if reply.readline(65) != b"restored\n":
                        raise RuntimeError("原设置恢复未确认")
                    deadline = time.monotonic() + 5
                    while not channel.exit_status_ready():
                        if time.monotonic() >= deadline:
                            raise RuntimeError("传输会话退出未确认")
                        time.sleep(0.05)
                    if channel.recv_exit_status() != 0:
                        raise RuntimeError("原设置恢复未确认")
            except Exception as exc:
                detail = "设备原设置恢复未确认，请重新连接设备检查；已导入文件保留，请勿直接重复上传。"
                if primary_error is not None:
                    detail = f"{primary_error}\n{detail}"
                raise RuntimeError(detail) from exc
            finally:
                if reply:
                    reply.close()
                channel.close()

    @staticmethod
    def _signature(info):
        return info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _native_address_present(self):
        # A same-address LAN host must not be mistaken for the tablet's importer.
        output = self.ssh_client.exec_checked(_NATIVE_ADDRESS_STATE)
        for line in output.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "inet":
                try:
                    if ipaddress.IPv4Interface(fields[1]).ip == ipaddress.IPv4Address("10.11.99.1"):
                        return True
                except ValueError:
                    continue
        return False

    def _check_space(self, required):
        available = df_free_bytes(self.ssh_client, self.document_root)
        required += 64 * 1024 * 1024
        if available < required:
            raise RuntimeError(
                f"设备文档区空间不足：需要约 {required / 1024**2:.1f} MiB（含导入预留），"
                f"可用约 {available / 1024**2:.1f} MiB。"
            )

    @staticmethod
    def _check_ready(transport):
        connection = _NativeHTTPConnection(transport, timeout=5)
        try:
            connection.request("GET", "/", headers={"Connection": "close"})
            response = connection.getresponse()
            try:
                if response.status != 200:
                    raise RuntimeError("原生接口未就绪")
            finally:
                response.close()
        except Exception as exc:
            raise RuntimeError(
                "设备原生导入服务未就绪，未上传任何文件。请解锁设备并停止扫码接收后重试；"
                "仍不可用时，请在「文档中心 > 原生传输服务」初始化或更新组件。"
            ) from exc
        finally:
            connection.close()
