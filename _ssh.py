"""SSH transport layer extracted from rmtool.py.

SSHClientWrapper, UnknownHostKeyError, remount_rw context manager, and the
require_connection decorator live here to keep rmtool.py focused on UI.
"""

import base64
import inspect
import logging
import socket
from contextlib import contextmanager
from functools import wraps
from typing import Dict, Iterator, Optional, Tuple

import paramiko
from PyQt5 import QtCore, QtWidgets


def _get_known_hosts_path():
    from rmtool import known_hosts_path
    return known_hosts_path()


def _get_host_key_fingerprint(host_key):
    from rmtool import host_key_fingerprint
    return host_key_fingerprint(host_key)


def _get_app_name():
    from rmtool import APP_NAME
    return APP_NAME


@contextmanager
def remount_rw(ssh_client: "SSHClientWrapper") -> Iterator[None]:
    """Remount root as read-write, guaranteeing read-only on exit."""
    ssh_client.exec_checked("mount -o remount,rw /")
    try:
        yield
    finally:
        try:
            ssh_client.exec_checked("mount -o remount,ro /")
        except Exception:
            logging.exception("Failed to remount root as read-only")


def require_connection(method):
    """Decorator: show a warning if the device is not connected."""
    signature = inspect.signature(method)
    accepts_positional_signal_args = any(
        parameter.name != "self"
        and parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
        for parameter in signature.parameters.values()
    )

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        if not self.ssh_client.is_connected():
            QtWidgets.QMessageBox.warning(self, _get_app_name(), "请先连接设备后再操作。")
            return None
        if args and not accepts_positional_signal_args:
            # Qt button signals often send a trailing `checked` bool even for
            # slots that conceptually take no explicit arguments.
            return method(self, **kwargs)
        return method(self, *args, **kwargs)

    return wrapper


class UnknownHostKeyError(RuntimeError):
    def __init__(self, host: str, host_key: paramiko.PKey):
        self.host = host
        self.host_key = host_key
        self.fingerprint = _get_host_key_fingerprint(host_key)
        super().__init__(
            f"首次连接到 {host}，检测到新的 SSH 主机指纹：{self.fingerprint}"
        )


class SSHClientWrapper(QtCore.QObject):
    connection_changed = QtCore.pyqtSignal(bool)

    def __init__(self, parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        self._client: Optional[paramiko.SSHClient] = None
        self.connection_info: Dict[str, str] = {}

    def _build_client(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        # Keep rmtool trust isolated from the user's global SSH known_hosts.
        # reMarkable devices often reuse the same USB/WiFi address, so global
        # host records for 10.11.99.1 can otherwise override the selected device.
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        return client

    @staticmethod
    def _is_unknown_host_error(exc: Exception) -> bool:
        if isinstance(exc, paramiko.BadHostKeyException):
            return False
        message = str(exc).lower()
        return "known_hosts" in message and "not found" in message

    @staticmethod
    def _fetch_remote_host_key(host: str, timeout: int = 10) -> paramiko.PKey:
        sock = socket.create_connection((host, 22), timeout=timeout)
        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=timeout)
            return transport.get_remote_server_key()
        finally:
            transport.close()
            sock.close()

    @staticmethod
    def _trust_host_key(host: str, host_key: paramiko.PKey) -> None:
        host_keys_file = _get_known_hosts_path()
        host_keys = paramiko.HostKeys()
        if host_keys_file.exists():
            host_keys.load(str(host_keys_file))
        host_keys.add(host, host_key.get_name(), host_key)
        host_keys_file.parent.mkdir(parents=True, exist_ok=True)
        host_keys.save(str(host_keys_file))

    @staticmethod
    def _trust_identity(
        host: str,
        connection_mode: str,
        device_name: str,
    ) -> str:
        normalized_name = device_name.strip()
        if normalized_name:
            encoded_name = base64.urlsafe_b64encode(
                normalized_name.encode("utf-8")
            ).decode("ascii").rstrip("=")
            return f"rmtool-device-{encoded_name}"
        return host

    @staticmethod
    def _lookup_trusted_host_key(
        trust_identity: str,
        host: str,
    ) -> Tuple[Optional[str], Optional[paramiko.PKey]]:
        host_keys_file = _get_known_hosts_path()
        if not host_keys_file.exists():
            return None, None

        host_keys = paramiko.HostKeys()
        host_keys.load(str(host_keys_file))
        lookup_order = [trust_identity]
        if host != trust_identity:
            lookup_order.append(host)

        for lookup_name in lookup_order:
            keys = host_keys.lookup(lookup_name)
            if keys:
                return lookup_name, next(iter(keys.values()))
        return None, None

    @staticmethod
    def _apply_trusted_host_key(
        client: paramiko.SSHClient,
        host: str,
        host_key: paramiko.PKey,
    ) -> None:
        client.get_host_keys().add(host, host_key.get_name(), host_key)

    @staticmethod
    def _connect_client(
        client: paramiko.SSHClient,
        host: str,
        password: str,
        timeout: int,
    ) -> None:
        client.connect(
            hostname=host,
            username="root",
            password=password,
            timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
        )

    def connect(
        self,
        host: str,
        password: str,
        timeout: int = 10,
        trust_unknown_host: bool = False,
        device_name: str = "",
        connection_mode: str = "wifi",
    ) -> None:
        logging.info("Connecting to %s", host)
        self.close()
        self.connection_info = {}
        trust_identity = self._trust_identity(host, connection_mode, device_name)
        trusted_from, trusted_key = self._lookup_trusted_host_key(trust_identity, host)
        client = self._build_client()
        if trusted_key:
            self._apply_trusted_host_key(client, host, trusted_key)
        try:
            self._connect_client(client, host, password, timeout)
        except paramiko.SSHException as exc:
            try:
                client.close()
            except Exception:
                logging.exception("Failed to close SSH client after connect error")
            legacy_host_trust = trusted_from == host and trust_identity != host
            if isinstance(exc, paramiko.BadHostKeyException) and not legacy_host_trust:
                raise RuntimeError(
                    f"{host} 的 SSH 主机指纹与已保存记录不匹配，连接已被拒绝。"
                ) from exc
            if not legacy_host_trust and not self._is_unknown_host_error(exc):
                raise
            host_key = self._fetch_remote_host_key(host, timeout)
            if not trust_unknown_host:
                raise UnknownHostKeyError(host, host_key) from exc
            self._trust_host_key(trust_identity, host_key)
            client = self._build_client()
            self._apply_trusted_host_key(client, host, host_key)
            self._connect_client(client, host, password, timeout)
        else:
            if trusted_key and trusted_from == host and trust_identity != host:
                self._trust_host_key(trust_identity, trusted_key)

        # Enable TCP keepalive so the connection survives idle periods
        transport = client.get_transport()
        if transport:
            transport.set_keepalive(30)
        self._client = client
        self.connection_info = {"host": host, "device_name": device_name}
        self.connection_changed.emit(True)

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        self.connection_info = {}
        self.connection_changed.emit(False)

    def ensure_client(self) -> paramiko.SSHClient:
        if not self._client:
            raise RuntimeError("未连接到设备")
        # Heartbeat: verify the underlying transport is still alive
        try:
            transport = self._client.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError("transport dead")
            transport.send_ignore()
        except Exception:
            self._client = None
            self.connection_changed.emit(False)
            raise RuntimeError("连接已断开，请重新连接")
        return self._client

    @contextmanager
    def sftp_session(self) -> Iterator[paramiko.SFTPClient]:
        client = self.ensure_client()
        sftp = client.open_sftp()
        try:
            yield sftp
        finally:
            try:
                sftp.close()
            except Exception:
                logging.exception("Failed to close SFTP session")

    def is_connected(self) -> bool:
        if not self._client:
            return False
        try:
            transport = self._client.get_transport()
            return transport is not None and transport.is_active()
        except Exception:
            return False

    # -- Command execution ---------------------------------------------------
    def exec_command(self, command: str) -> Tuple[str, str, int]:
        """Execute *command* and return ``(stdout, stderr, exit_code)``."""
        client = self.ensure_client()
        logging.info("Executing command: %s", command)
        _stdin, stdout, stderr = client.exec_command(command)
        exit_code = stdout.channel.recv_exit_status()
        return (
            stdout.read().decode("utf-8"),
            stderr.read().decode("utf-8"),
            exit_code,
        )

    def exec_checked(self, command: str) -> str:
        """Execute *command* and raise on non-zero exit code.  Returns stdout."""
        stdout, stderr, code = self.exec_command(command)
        if code != 0:
            error_msg = stderr.strip() or stdout.strip() or f"exit code {code}"
            raise RuntimeError(f"命令执行失败: {error_msg}")
        return stdout

    # -- File operations -----------------------------------------------------
    def transfer_file(self, local_path: str, remote_path: str) -> None:
        with self.sftp_session() as sftp:
            logging.info("Transferring %s -> %s", local_path, remote_path)
            sftp.put(local_path, remote_path)

    def file_exists(self, remote_path: str) -> bool:
        with self.sftp_session() as sftp:
            try:
                sftp.stat(remote_path)
                return True
            except IOError:
                return False

    def listdir_attr(self, remote_path: str):
        with self.sftp_session() as sftp:
            return sftp.listdir_attr(remote_path)

    def open_remote(self, remote_path: str, mode: str = "r"):
        @contextmanager
        def _remote_file() -> Iterator[paramiko.SFTPFile]:
            with self.sftp_session() as sftp:
                with sftp.open(remote_path, mode) as fh:
                    yield fh

        return _remote_file()

    def download_file(
        self,
        remote_path: str,
        local_path: str,
        callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        with self.sftp_session() as sftp:
            Path(local_path).parent.mkdir(parents=True, exist_ok=True)
            sftp.get(remote_path, local_path, callback=callback)

    def download_directory(self, remote_dir: str, local_dir: str) -> None:
        with self.sftp_session() as sftp:
            self._download_directory_recursive(sftp, remote_dir, local_dir)

    def _download_directory_recursive(
        self, sftp: paramiko.SFTPClient, remote_dir: str, local_dir: str
    ) -> None:
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        for entry in sftp.listdir_attr(remote_dir):
            remote_path = f"{remote_dir}/{entry.filename}"
            local_path = os.path.join(local_dir, entry.filename)
            if stat.S_ISDIR(entry.st_mode):
                self._download_directory_recursive(sftp, remote_path, local_path)
            else:
                sftp.get(remote_path, local_path)

