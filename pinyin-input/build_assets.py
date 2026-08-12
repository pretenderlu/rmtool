"""Build and verify the complete Paper Pro/Move Pinyin package matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import _fast_mono_reading as fast
import _native_chinese as native
import _pinyin_input as pinyin
import _tap_page_turn as tap
import _xovi_standalone


COMMON_PATHS = (
    "xovi.so",
    "extensions.d/qt-resource-rebuilder.so",
    "exthome/qt-resource-rebuilder/hashtab",
    "qmd-tool",
)
PEER_PATHS = {
    "native": native.QMD_PATH,
    "tap": "exthome/qt-resource-rebuilder/tap-page-turn.qmd",
    "fast": "exthome/qt-resource-rebuilder/fast-mono-reading.qmd",
}
XOCHITL_ROOTS = (
    Path(r"E:\remarkable\firmware-cache\official"),
    Path(r"E:\remarkable\firmware-cache\official-beta"),
    Path(r"E:\remarkable\firmware-cache\device-beta"),
)
ARCHIVE_ROOTS = (
    REPO_ROOT / "build/native-chinese",
    REPO_ROOT / "build/native-chinese-label-owner",
    REPO_ROOT / "build/tap-page-turn-166",
    REPO_ROOT / "build/tap-page-turn-164",
    REPO_ROOT / "build/cos-sync/tap-page-turn",
    REPO_ROOT / "build/fast-mono-reading-expanded",
    REPO_ROOT / "build/fast-mono-r4/assets",
    REPO_ROOT / "build/fast-mono-reading-166",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identity(package) -> tuple[str, str, str, str]:
    return (
        package.firmware,
        package.platform,
        package.architecture,
        package.xochitl_sha256,
    )


def _find_exact_file(roots: tuple[Path, ...], name: str, size: int, digest: str) -> Path:
    for root in roots:
        if not root.is_dir():
            continue
        for candidate in (root / name, *root.glob(f"**/{name}")):
            if (
                candidate.is_file()
                and candidate.stat().st_size == size
                and sha256(candidate.read_bytes()) == digest
            ):
                return candidate
    raise RuntimeError(f"Missing verified archive: {name}")


def _find_xochitl(package, roots: tuple[Path, ...]) -> Path:
    suffix = Path(package.release_version) / package.platform
    candidates = []
    for root in roots:
        if not root.is_dir():
            continue
        candidates.extend(root.glob(f"**/{suffix.as_posix()}/**/usr/bin/xochitl"))
    matches = [
        path for path in candidates
        if path.is_file() and sha256(path.read_bytes()) == package.xochitl_sha256
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one exact xochitl for {package.platform}/{package.release_version}, "
            f"found {len(matches)}"
        )
    return matches[0]


def _packages_by_identity(module) -> dict[tuple[str, str, str, str], object]:
    return {_identity(package): package for package in module._trusted_catalog()}


def _qmd_check(qmd_tool: Path, hashtab: bytes, qmds: tuple[tuple[str, bytes], ...]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        hashtabs = root / "hashtabs"
        qmd_dir = root / "qmd"
        hashtabs.mkdir()
        qmd_dir.mkdir()
        (hashtabs / "hashtab-target").write_bytes(hashtab)
        for index, (name, data) in enumerate(qmds):
            (qmd_dir / f"{index:02d}-{name}.qmd").write_bytes(data)
        result = subprocess.run(
            [str(qmd_tool), "check", "-hashtabs", str(hashtabs), "-qmd", str(qmd_dir)],
            capture_output=True,
            check=False,
        )
        output = result.stdout + result.stderr
        if result.returncode or b"ALL OK" not in output:
            raise RuntimeError(output.decode("utf-8", errors="replace"))


def _check_orders(
    qmd_tool: Path,
    hashtab: bytes,
    pinyin_qmd: bytes,
    peers: dict[str, bytes],
) -> None:
    _qmd_check(qmd_tool, hashtab, (("pinyin", pinyin_qmd),))
    for name, data in peers.items():
        _qmd_check(qmd_tool, hashtab, (("pinyin", pinyin_qmd), (name, data)))
        _qmd_check(qmd_tool, hashtab, ((name, data), ("pinyin", pinyin_qmd)))
    all_qmds = (("native", peers["native"]), ("tap", peers["tap"]),
                ("fast", peers["fast"]), ("pinyin", pinyin_qmd))
    _qmd_check(qmd_tool, hashtab, all_qmds)
    _qmd_check(qmd_tool, hashtab, tuple(reversed(all_qmds)))


def build_target(
    package: native.NativeChinesePackage,
    runtime_archive: Path,
    peer_archives: dict[str, tuple[Path, object]],
    rmkit_root: Path,
    qmd_tool: Path,
    xochitl_roots: tuple[Path, ...],
) -> tuple[dict, bytes]:
    xochitl = _find_xochitl(package, xochitl_roots)
    if sha256(xochitl.read_bytes()) != package.xochitl_sha256:
        raise RuntimeError("xochitl does not match the exact target")

    with tempfile.TemporaryDirectory() as temporary:
        extracted = tap.extract_verified_package(runtime_archive, package, temporary)
        files = {
            path: (
                extracted.joinpath(*PurePosixPath(path).parts).read_bytes(),
                package.file(path).mode,
            )
            for path in COMMON_PATHS
        }
        peers = {
            "native": extracted.joinpath(*PurePosixPath(native.QMD_PATH).parts).read_bytes()
        }
    for name, (archive, peer_package) in peer_archives.items():
        with tempfile.TemporaryDirectory() as temporary:
            extracted = tap.extract_verified_package(archive, peer_package, temporary)
            peers[name] = extracted.joinpath(*PurePosixPath(PEER_PATHS[name]).parts).read_bytes()

    fixed_files = {
        pinyin.QMD_PATH: (REPO_ROOT / "pinyin-input/qmd/pinyin-input.qmd", 0o644),
        pinyin.HOOK_PATH: (rmkit_root / "dist/ime_hook.so", 0o644),
        pinyin.RCC_PATH: (REPO_ROOT / "pinyin-input/zh_CN.rcc", 0o644),
        pinyin.SERVER_PATH: (rmkit_root / "dist/ime-server", 0o755),
        pinyin.UNIT_PATH: (REPO_ROOT / "pinyin-input/rmtool-pinyin-input.service", 0o644),
        pinyin.NOTICE_PATH: (rmkit_root / "NOTICE.md", 0o644),
        pinyin.LICENSE_PATH: (rmkit_root / "LICENSE", 0o644),
    }
    files.update({path: (source.read_bytes(), mode) for path, (source, mode) in fixed_files.items()})
    unit = files[pinyin.UNIT_PATH][0].decode("utf-8")
    server = files[pinyin.SERVER_PATH][0]
    if (
        pinyin.REMOTE_SERVER not in unit
        or str(len(server)) not in unit
        or sha256(server) not in unit
        or "stat -c %%a:%%u:%%g:%%s" not in unit
        or "After=home.mount" not in unit
        or "PartOf=xochitl.service" not in unit
    ):
        raise RuntimeError("Pinyin service unit does not match the exact server")
    if set(files) != pinyin.PAYLOAD_PATHS:
        raise RuntimeError("Pinyin payload whitelist mismatch")
    pinyin_qmd = files[pinyin.QMD_PATH][0]
    _check_orders(
        qmd_tool,
        files["exthome/qt-resource-rebuilder/hashtab"][0],
        pinyin_qmd,
        peers,
    )

    payload = tap._gzip_member(
        tap._tar_member(files, apk_checksums=False, include_directories=False)
    )
    if payload != tap._gzip_member(
        tap._tar_member(files, apk_checksums=False, include_directories=False)
    ):
        raise RuntimeError("Pinyin package build is not deterministic")
    identity = _identity(package)
    release, channel, offline_verified, device_verified = pinyin.ALLOWED_TARGETS[identity]
    asset = pinyin.EXPECTED_ASSETS[identity]
    entry = {
        "firmware": package.firmware,
        "release_version": release,
        "channel": channel,
        "platform": package.platform,
        "architecture": package.architecture,
        "xochitl_sha256": package.xochitl_sha256,
        "asset": asset,
        "sha256": sha256(payload),
        "size": len(payload),
        "urls": [f"{pinyin.COS_URL}/{asset}", f"{pinyin.GITHUB_URL}/{asset}"],
        "offline_verified": offline_verified,
        "device_verified": device_verified,
        "files": [
            {"path": path, "sha256": sha256(data), "size": len(data), "mode": mode}
            for path, (data, mode) in sorted(files.items())
        ],
    }
    return entry, payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-root", action="append", type=Path, default=[])
    parser.add_argument("--xochitl-root", action="append", type=Path, default=[])
    parser.add_argument("--rmkit-root", type=Path, default=Path(r"E:\rmkit-cn-v1.1.1"))
    parser.add_argument(
        "--qmd-tool", type=Path,
        default=Path(r"E:\rmkit-cn-v1.1.1\dist\qmd-tool-windows-amd64.exe"),
    )
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "build/pinyin-input")
    parser.add_argument("--manifest", type=Path, default=REPO_ROOT / "pinyin-input/manifest.json")
    parser.add_argument("--local-cache", type=Path)
    args = parser.parse_args()
    if not args.qmd_tool.is_file():
        raise FileNotFoundError(args.qmd_tool)

    archive_roots = tuple(args.archive_root) or ARCHIVE_ROOTS
    xochitl_roots = tuple(args.xochitl_root) or XOCHITL_ROOTS
    native_packages = native._trusted_catalog()
    tap_packages = _packages_by_identity(tap)
    fast_packages = _packages_by_identity(fast)
    built = []
    for package in native_packages:
        identity = _identity(package)
        if identity not in pinyin.ALLOWED_TARGETS:
            continue
        runtime_archive = _find_exact_file(
            archive_roots, package.asset, package.size, package.sha256
        )
        peer_archives = {}
        for name, catalog in (("tap", tap_packages), ("fast", fast_packages)):
            peer = catalog.get(identity)
            if peer is None:
                raise RuntimeError(f"Missing exact {name} peer for {identity}")
            peer_archives[name] = (
                _find_exact_file(archive_roots, peer.asset, peer.size, peer.sha256),
                peer,
            )
        built.append(
            build_target(
                package, runtime_archive, peer_archives, args.rmkit_root,
                args.qmd_tool, xochitl_roots,
            )
        )

    entries = [entry for entry, _payload in built]
    manifest = (
        json.dumps({"schema_version": 1, "packages": entries}, ensure_ascii=False, indent=2)
        + "\n"
    ).encode("utf-8")
    packages = pinyin.parse_manifest(manifest)
    if len(packages) != len(pinyin.ALLOWED_TARGETS):
        raise RuntimeError("Generated Pinyin manifest is incomplete")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    payloads = {_identity(package): payload for package, (_entry, payload) in zip(packages, built)}
    for package in packages:
        payload = payloads[_identity(package)]
        output = args.output_dir / package.asset
        tap._write_atomic(output, payload)
        with tempfile.TemporaryDirectory() as temporary:
            tap.extract_verified_package(output, package, temporary)
        runtime, feature = pinyin._shared_specs(package)
        _xovi_standalone.assert_feature_layout(runtime, (feature,))
        if args.local_cache is not None:
            tap._write_atomic(args.local_cache / package.firmware / package.asset, payload)
        print(f"archive={output}")
        print(f"archive_sha256={package.sha256}")
        print(f"archive_size={package.size}")
    tap._write_atomic(args.output_dir / "manifest.json", manifest)
    tap._write_atomic(args.manifest, manifest)
    print(f"targets={len(packages)}")
    print("verification=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
