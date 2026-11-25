"""Immutable, on-demand RM2 QR receivers. Never consulted by the boot launcher."""
from __future__ import annotations

import hashlib
import logging
import re
import shlex
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import _device_storage as storage

DIRECTORY = "/home/root/.local/share/rmtool/qr-upload/receivers"
ARCHIVE_PATH = "helpers/rmtool-qr-upload"


def _parents(action):
    # Never create or remove anything on the underlying root if /home is offline.
    paths = PurePosixPath(DIRECTORY).parents
    directories = " ".join(shlex.quote(str(p)) for p in (*reversed(paths), DIRECTORY) if str(p) != "/")
    return f"""awk '$5 == "/home" {{ok=1}} END {{exit !ok}}' /proc/self/mountinfo || exit 1
for dir in {directories}; do
    [ ! -L "$dir" ] || exit 1
    if [ ! -e "$dir" ]; then {action}; fi
    [ -d "$dir" ] && [ "$(stat -c '%u:%g' "$dir")" = 0:0 ] &&
        [ $((0$(stat -c %a "$dir") & 0022)) -eq 0 ] || exit 1
    if [ "$dir" = /home ]; then
        root_dev=$(stat -c %d /) && home_dev=$(stat -c %d "$dir") &&
            [ "$root_dev" != "$home_dev" ] || exit 1
    fi
done"""


@dataclass(frozen=True)
class Receiver:
    digest: str
    size: int

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{64}", self.digest) or not 0 < self.size <= 10 * 1024**2:
            raise ValueError("无效的扫码接收程序身份。")

    @property
    def path(self):
        return f"{DIRECTORY}/{self.digest}"

    def verify(self, path):
        quoted = shlex.quote(path)
        return (f"[ ! -L {quoted} ] && [ \"$(stat -c '%f:%u:%g:%s:%h' {quoted})\" = 81ed:0:0:{self.size}:1 ] && "
                f"[ \"$(sha256sum {quoted} | awk '{{print $1}}')\" = {self.digest} ]")

    def present(self, ssh):
        try:
            result = ssh.exec_checked(f"""set -eu
{_parents("echo missing; exit 0")}
if [ ! -e {self.path} ] && [ ! -L {self.path} ]; then echo missing
else {self.verify(self.path)} || exit 1; echo verified; fi""").strip()
        except RuntimeError as exc:
            raise RuntimeError("无法校验 /home 中的扫码接收程序或其目录，未覆盖现有文件。") from exc
        if result not in ("verified", "missing"):
            raise RuntimeError("扫码接收程序状态不确定。")
        return result == "verified"

    def require_space(self, ssh):
        storage.require_capacity("/home", storage.df_free_bytes(ssh, "/home"),
                                 24 * 1024**2 + ((self.size + 4095) // 4096) * 4096)

    def check_capacity(self, ssh):
        if not self.present(ssh):
            self.require_space(ssh)

@dataclass(frozen=True)
class Prepared:
    receiver: Receiver
    temporary: str

    @property
    def commit(self):
        r = self.receiver
        source = self.temporary + "/receiver"
        return f"""{_parents('exit 1')}
if [ -e {r.path} ] || [ -L {r.path} ]; then
    {r.verify(r.path)} || exit 1
else
    [ ! -L {self.temporary} ] && [ "$(stat -c '%a:%u:%g' {self.temporary})" = 700:0:0 ] || exit 1
    {r.verify(source)} || exit 1
    QR_CREATED=1
    mv {source} {r.path}
fi"""

    @property
    def undo(self):
        r = self.receiver
        return f"""if [ "$QR_CREATED" = 1 ] && [ "$ROLLBACK_OK" = 1 ]; then
    ( {_parents('exit 1')}
      if [ -e {r.path} ] || [ -L {r.path} ]; then {r.verify(r.path)} && rm -f {r.path}; fi
    ) || ROLLBACK_OK=0
fi"""


@dataclass(frozen=True)
class Retirement:
    receiver: Receiver
    directory: str

    @property
    def commit(self):
        r = self.receiver
        return f"""{_parents('exit 1')}
{r.verify(r.path)} || exit 1
mkdir -m 0700 {self.directory}
QR_RETIRING=1
mv {r.path} {self.directory}/receiver"""

    @property
    def undo(self):
        r = self.receiver
        return f"""if [ "$QR_RETIRING" = 1 ]; then
    ( {_parents('exit 1')}
      if [ -f {self.directory}/receiver ]; then
          {r.verify(self.directory + '/receiver')} && [ ! -e {r.path} ] && [ ! -L {r.path} ] &&
              mv {self.directory}/receiver {r.path} || exit 1
      fi
      rmdir {self.directory}
    ) || ROLLBACK_OK=0
fi"""

    @property
    def finish(self):
        return f"rm -f {self.directory}/receiver && rmdir {self.directory}"


def retire(ssh, receiver, token):
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("无效的扫码卸载事务。")
    if receiver.present(ssh):
        return Retirement(receiver, f"{DIRECTORY}/.retired-{token}")
    return None


def matching(runtime, spec):
    if spec is None or runtime.platform != "rm2" or spec.feature_id != "qr-upload":
        return None
    import _qr_upload as qr
    p = qr.select_package(qr._trusted_catalog(), runtime)
    if p is None or p.package_revision != 2 or qr._shared_specs(p)[1] != spec:
        return None
    item = p.file(ARCHIVE_PATH)
    return Receiver(item.sha256, item.size)


@contextmanager
def prepare(ssh, receiver, source: Path, token):
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("无效的扫码安装事务。")
    temp = f"{DIRECTORY}/.stage-{token}"
    prepared = Prepared(receiver, temp)
    if receiver.present(ssh):
        yield prepared
        return
    if source.is_symlink() or not source.is_file() or source.stat().st_size != receiver.size or (
        hashlib.sha256(source.read_bytes()).hexdigest() != receiver.digest
    ):
        raise RuntimeError("扫码接收程序与精确包不符，未写入设备。")
    receiver.require_space(ssh)
    create_parents = _parents('mkdir -m 0755 "$dir"')
    ssh.exec_checked(f"set -eu\n{create_parents}\nmkdir -m 0700 {temp}")
    try:
        path = temp + "/receiver"
        ssh.transfer_file(str(source), path)
        ssh.exec_checked(f"set -eu\nchmod 0755 {path}\n{receiver.verify(path)} || exit 1")
        yield prepared
    finally:
        try:
            ssh.exec_checked(f"set -eu\n{_parents('exit 1')}\n[ ! -L {temp} ] && "
                             f"[ \"$(stat -c '%a:%u:%g' {temp})\" = 700:0:0 ] || exit 1\n"
                             f"rm -f {temp}/receiver\nrmdir {temp}")
        except Exception:
            logging.warning("Could not clean private QR staging directory %s", temp, exc_info=True)
