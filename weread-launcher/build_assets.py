"""Build and replay-check exact WeRead launcher packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import _reading_enhancements as reading
import _tap_page_turn as tap
import _weread_launcher as weread


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(command: list[str], label: str) -> bytes:
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{label} failed: {(result.stdout + result.stderr).decode(errors='replace')}")
    return result.stdout


def compile_qmd(qmd_tool: Path, qmldiff: Path, source: Path, target_root: Path, work: Path) -> bytes:
    hashtab = target_root / "hashtab"
    qrex = target_root / "qrex-out"
    if not hashtab.is_file() or not qrex.is_dir():
        raise RuntimeError(f"missing 3.28 validation tree: {target_root}")
    compiled = run(
        [str(qmd_tool), "hash", "-hashtab", str(hashtab), str(source)],
        f"compile {target_root.name}",
    )
    check = work / target_root.name
    hashtabs = check / "hashtabs"
    qmds = check / "qmd"
    replay = check / "replay"
    hashtabs.mkdir(parents=True)
    qmds.mkdir()
    shutil.copy2(hashtab, hashtabs / f"hashtab-{target_root.name}")
    qmd_path = qmds / "weread-launcher.qmd"
    qmd_path.write_bytes(compiled)
    if b"ALL OK" not in run(
        [str(qmd_tool), "check", "-hashtabs", str(hashtabs), "-qmd", str(qmds)],
        f"qmd check {target_root.name}",
    ):
        raise RuntimeError(f"QMD check was incomplete: {target_root.name}")
    run(
        [str(qmldiff), "apply-diffs", "--hashtab", str(hashtab), "-c",
         str(qrex), str(replay), str(qmd_path)],
        f"replay {target_root.name}",
    )
    settings = (replay / "qml/device/view/settings/Settings.qml").read_text(encoding="utf-8")
    for marker in (
        "import cn.rmtool.WeReadLauncher 1.0",
        "rmtoolWeReadSidebarItem",
        "rmtoolWeReadLauncherPage",
        'text: "微信读书"',
        'text: "普通"',
        'text: "彩色快刷"',
        'text: "黑白快刷"',
        'label: "强制完整刷新"',
        'iconSource: "qrc:/rmtool/wereader.png"',
        "rmtoolWeReadBridge.launch(nativeMode(), refreshPages)",
        "fastModeAvailable: true",
    ):
        if marker not in settings:
            raise RuntimeError(f"replay {target_root.name} missing {marker}")
    for forbidden in ("executeCommand", "systemctl", "LD_PRELOAD"):
        if forbidden in settings:
            raise RuntimeError(f"QML exposes forbidden launch surface: {forbidden}")
    return compiled


def find_carrier(package, roots: tuple[Path, ...]) -> Path:
    for root in roots:
        for candidate in dict.fromkeys((root / package.asset, *root.glob(f"**/{package.asset}"))):
            if (candidate.is_file() and candidate.stat().st_size == package.size
                    and sha256(candidate.read_bytes()) == package.sha256):
                return candidate
    raise RuntimeError(f"missing verified reading carrier: {package.asset}")


def build_archive(package, carrier: Path, qmd: bytes, bridge: bytes, shim: bytes):
    with tempfile.TemporaryDirectory() as temporary:
        extracted = reading.extract_verified_package(carrier, package, temporary)
        files = {
            item.path: (
                extracted.joinpath(*PurePosixPath(item.path).parts).read_bytes(),
                item.mode,
            )
            for item in package.files
            if item.path in weread._PAYLOAD_PATHS
        }
    files[weread.QMD_PAYLOAD_PATH] = (qmd, 0o644)
    files[weread.BRIDGE_PAYLOAD_PATH] = (bridge, 0o644)
    files[weread.SHIM_PAYLOAD_PATH] = (shim, 0o644)
    if set(files) != weread._PAYLOAD_PATHS:
        raise RuntimeError("WeRead launcher payload drifted from fixed whitelist")
    return tap._gzip_member(tap._tar_member(files, apk_checksums=False, include_directories=False)), files


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qmd-tool", type=Path, required=True)
    parser.add_argument("--qmldiff", type=Path, required=True)
    parser.add_argument("--matrix-root", type=Path, required=True)
    parser.add_argument("--bridge", type=Path, default=ROOT / "weread-launcher/native/rmtool-weread-launcher.so")
    parser.add_argument("--shim", type=Path, default=ROOT / "weread-launcher/native/rmtool-weread-fast.so")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--write-manifest", type=Path, required=True)
    parser.add_argument("--cache-root", action="append", type=Path, default=[])
    args = parser.parse_args()

    bridge = args.bridge.read_bytes()
    if not bridge.startswith(b"\x7fELF") or len(bridge) > 4 * 1024 * 1024:
        raise RuntimeError("WeRead launcher bridge is not a bounded ELF payload")
    shim = args.shim.read_bytes()
    if not shim.startswith(b"\x7fELF") or len(shim) > 256 * 1024:
        raise RuntimeError("WeRead fast refresh shim is not a bounded ELF payload")
    roots = tuple(args.cache_root) or (ROOT / ".rmtool/cache/reading-enhancements",)
    carriers = tuple(
        package for package in reading._trusted_catalog()
        if package.release_version == "3.28.0.172" and package.platform in {"ferrari", "chiappa"}
    )
    if len(carriers) != len(weread.ALLOWED_TARGETS):
        raise RuntimeError("reading carrier set does not cover WeRead launcher targets")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        for package in sorted(carriers, key=lambda item: item.platform):
            qmd = compile_qmd(
                args.qmd_tool,
                args.qmldiff,
                ROOT / "weread-launcher/qmd-src/weread-launcher-3.28.qmd",
                args.matrix_root / f"{package.platform}-{package.firmware}",
                work,
            )
            archive, files = build_archive(
                package, find_carrier(package, roots), qmd, bridge, shim
            )
            release, channel, offline, device = weread.ALLOWED_TARGETS[
                (package.platform, package.firmware, package.architecture, package.xochitl_sha256)
            ]
            asset = weread._expected_asset_name(package.platform, package.firmware, release)
            (args.output_dir / asset).write_bytes(archive)
            entries.append({
                "firmware": package.firmware,
                "release_version": release,
                "channel": channel,
                "platform": package.platform,
                "architecture": package.architecture,
                "xochitl_sha256": package.xochitl_sha256,
                "offline_verified": offline,
                "device_verified": device,
                "package_revision": weread.PACKAGE_REVISION,
                "asset": asset,
                "sha256": sha256(archive),
                "size": len(archive),
                "urls": [f"{base}/{asset}" for base in weread.REMOTE_BASE_URLS],
                "files": [
                    {"path": path, "sha256": sha256(data), "size": len(data), "mode": mode}
                    for path, (data, mode) in sorted(files.items())
                ],
            })
    manifest = (json.dumps({"schema_version": 1, "packages": entries}, indent=2) + "\n").encode()
    args.write_manifest.parent.mkdir(parents=True, exist_ok=True)
    args.write_manifest.write_bytes(manifest)
    weread._trusted_catalog.cache_clear()
    if weread.parse_manifest(manifest, require_local_match=False) == ():
        raise RuntimeError("generated manifest is empty")
    print(f"built {len(entries)} exact WeRead launcher packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
