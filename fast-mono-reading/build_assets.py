"""Build and self-verify the exact fast-mono asset matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import _fast_mono_reading as fast
import _tap_page_turn as tap


BASE_QMD_PATH = "exthome/qt-resource-rebuilder/tap-page-turn.qmd"
QMD_VARIANTS = {
    "3.27": (
        "643f5569e65149798888d267f616b77034b3abb9f1b695806d12f6c22a378cea",
        12017,
    ),
    "3.28": (
        "6949f58896651a3254c9e143461b384892f4d779e8f2553f9adf11ff8fe5707d",
        11138,
    ),
    "3.28.0.164": (
        "9eb1e98a731458f1b46b170e11bfd29d11edbec04caf8befedc859fefd9acf5d",
        11339,
    ),
}
DEFAULT_QMD_PATHS = {
    "3.27": REPO_ROOT
    / "build/fast-mono-matrix/results/chiappa-20260612085811/qmd/fast-mono-reading.qmd",
    "3.28": REPO_ROOT
    / "build/fast-mono-matrix/results/chiappa-20260629074044/qmd/fast-mono-reading.qmd",
    "3.28.0.164": REPO_ROOT
    / "build/fast-mono-matrix/results-164/chiappa-20260702125656/qmd/fast-mono-reading.qmd",
}
DEFAULT_CACHE_ROOTS = (
    Path(r"E:\remarkable\firmware-cache\work\tap-matrix\cloud-verify-20260721"),
    Path(r"E:\remarkable\firmware-cache\work\tap-matrix\release-163"),
    REPO_ROOT / ".rmtool/cache/tap-page-turn",
    Path(r"E:\remarkable\firmware-cache\work\tap-matrix\release"),
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _variant(release_version: str) -> str:
    if release_version == "3.28.0.164":
        return release_version
    return "3.28" if release_version.startswith("3.28.") else "3.27"


def _asset_name(base: tap.TapPageTurnPackage) -> str:
    return fast._expected_asset_name(
        base.platform, base.firmware, base.release_version
    )


def _verified_file(path: Path, *, size: int, digest: str) -> bool:
    if not path.is_file() or path.stat().st_size != size:
        return False
    return sha256(path.read_bytes()) == digest


def _find_base_archive(
    package: tap.TapPageTurnPackage,
    cache_roots: tuple[Path, ...],
    download_root: Path,
    *,
    allow_download: bool,
) -> Path:
    for root in cache_roots:
        if not root.is_dir():
            continue
        candidates = [root / package.asset, *root.glob(f"**/{package.asset}")]
        for candidate in dict.fromkeys(candidates):
            if _verified_file(candidate, size=package.size, digest=package.sha256):
                return candidate
    if not allow_download:
        raise RuntimeError(f"Missing verified tap base archive: {package.asset}")
    data = tap._download_limited(package.download_url, tap.MAX_PACKAGE_BYTES)
    if len(data) != package.size or sha256(data) != package.sha256:
        raise RuntimeError(f"Downloaded tap base archive failed verification: {package.asset}")
    destination = download_root / package.asset
    tap._write_atomic(destination, data)
    return destination


def _target_base_packages() -> tuple[tap.TapPageTurnPackage, ...]:
    tap_catalog = tap.parse_manifest(
        (REPO_ROOT / "tap-page-turn/manifest.json").read_bytes()
    )
    packages = []
    for identity, policy in fast.ALLOWED_TARGETS.items():
        platform, firmware, architecture, xochitl_sha = identity
        release_version, channel, _offline_verified, _device_verified = policy
        matches = [
            package
            for package in tap_catalog
            if package.platform == platform
            and package.firmware == firmware
            and package.architecture == architecture
            and package.xochitl_sha256 == xochitl_sha
            and package.release_version == release_version
            and package.channel == channel
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"Tap manifest does not contain one exact base for {platform}/{firmware}"
            )
        packages.append(matches[0])
    return tuple(sorted(packages, key=lambda package: (package.firmware, package.platform)))


def _qmd_bytes(paths: dict[str, Path]) -> dict[str, bytes]:
    result = {}
    for variant, path in paths.items():
        data = path.read_bytes()
        expected_hash, expected_size = QMD_VARIANTS[variant]
        if len(data) != expected_size or sha256(data) != expected_hash:
            raise RuntimeError(f"Compiled {variant} QMD does not match the offline gate")
        result[variant] = data
    return result


def build_archive(
    base: tap.TapPageTurnPackage,
    base_archive: Path,
    compiled_qmd: bytes,
) -> tuple[bytes, dict]:
    base_data = base_archive.read_bytes()
    if len(base_data) != base.size or sha256(base_data) != base.sha256:
        raise RuntimeError(f"Base archive does not match tap manifest: {base.asset}")

    with tempfile.TemporaryDirectory() as temporary:
        extracted = tap.extract_verified_package(base_archive, base, temporary)
        files = {
            item.path: (
                extracted.joinpath(*PurePosixPath(item.path).parts).read_bytes(),
                item.mode,
            )
            for item in base.files
            if item.path != BASE_QMD_PATH
        }
    files[fast.QMD_PAYLOAD_PATH] = (compiled_qmd, 0o644)
    if set(files) != fast._PAYLOAD_PATHS:
        raise RuntimeError("Finished fast-mono payload does not match the fixed whitelist")

    archive = tap._gzip_member(
        tap._tar_member(files, apk_checksums=False, include_directories=False)
    )
    policy = fast.ALLOWED_TARGETS[
        (base.platform, base.firmware, base.architecture, base.xochitl_sha256)
    ]
    release_version, channel, offline_verified, device_verified = policy
    asset = _asset_name(base)
    entry = {
        "firmware": base.firmware,
        "release_version": release_version,
        "channel": channel,
        "platform": base.platform,
        "architecture": base.architecture,
        "xochitl_sha256": base.xochitl_sha256,
        "offline_verified": offline_verified,
        "device_verified": device_verified,
        "package_revision": 3,
        "asset": asset,
        "sha256": sha256(archive),
        "size": len(archive),
        "files": [
            {
                "path": path,
                "sha256": sha256(data),
                "size": len(data),
                "mode": mode,
            }
            for path, (data, mode) in sorted(files.items())
        ],
    }
    return archive, entry


def _qmd_check(qmd_tool: Path, archive: Path, package: fast.FastMonoReadingPackage) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = fast.extract_verified_package(archive, package, temporary)
        check = root / "check"
        hashtabs = check / "hashtabs"
        qmds = check / "qmd"
        hashtabs.mkdir(parents=True)
        qmds.mkdir()
        shutil.copy2(
            root.joinpath(*PurePosixPath("exthome/qt-resource-rebuilder/hashtab").parts),
            hashtabs / f"hashtab-{package.platform}-{package.firmware}",
        )
        shutil.copy2(
            root.joinpath(*PurePosixPath(fast.QMD_PAYLOAD_PATH).parts),
            qmds / "fast-mono-reading.qmd",
        )
        result = subprocess.run(
            [
                str(qmd_tool),
                "check",
                "-hashtabs",
                str(hashtabs),
                "-qmd",
                str(qmds),
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode or b"ALL OK" not in result.stdout + result.stderr:
            output = (result.stdout + result.stderr).decode(errors="replace")
            raise RuntimeError(
                f"Packaged hashtab QMD check failed for {package.package_id}:\n{output}"
            )


def _combined_qmd_check(
    qmd_tool: Path,
    base_archive: Path,
    base: tap.TapPageTurnPackage,
    fast_archive: Path,
    package: fast.FastMonoReadingPackage,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        tap_root = tap.extract_verified_package(base_archive, base, root / "tap")
        fast_root = fast.extract_verified_package(fast_archive, package, root / "fast")
        hashtabs = root / "hashtabs"
        hashtabs.mkdir()
        shutil.copy2(
            fast_root.joinpath(*PurePosixPath("exthome/qt-resource-rebuilder/hashtab").parts),
            hashtabs / f"hashtab-{package.platform}-{package.firmware}",
        )
        qmds = {
            "tap": tap_root.joinpath(*PurePosixPath(BASE_QMD_PATH).parts),
            "fast": fast_root.joinpath(*PurePosixPath(fast.QMD_PAYLOAD_PATH).parts),
        }
        for order in (("tap", "fast"), ("fast", "tap")):
            directory = root / ("qmd-" + "-".join(order))
            directory.mkdir()
            for index, name in enumerate(order, start=1):
                shutil.copy2(qmds[name], directory / f"{index:02d}-{name}.qmd")
            result = subprocess.run(
                [str(qmd_tool), "check", "-hashtabs", str(hashtabs), "-qmd", str(directory)],
                capture_output=True,
                check=False,
            )
            if result.returncode or b"ALL OK" not in result.stdout + result.stderr:
                output = (result.stdout + result.stderr).decode(errors="replace")
                raise RuntimeError(
                    f"Combined QMD check failed for {package.package_id} ({'/'.join(order)}):\n{output}"
                )


def _write_atomic(path: Path, data: bytes) -> None:
    tap._write_atomic(path, data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", action="append", type=Path, default=[])
    parser.add_argument("--qmd-3-27", type=Path, default=DEFAULT_QMD_PATHS["3.27"])
    parser.add_argument("--qmd-3-28", type=Path, default=DEFAULT_QMD_PATHS["3.28"])
    parser.add_argument(
        "--qmd-3-28-164",
        type=Path,
        default=DEFAULT_QMD_PATHS["3.28.0.164"],
    )
    parser.add_argument("--qmd-tool", type=Path, required=True)
    parser.add_argument("--no-download", action="store_true")
    parser.add_argument(
        "--output-dir", type=Path, default=REPO_ROOT / "build/fast-mono-reading"
    )
    parser.add_argument("--local-cache", type=Path)
    args = parser.parse_args()

    if not args.qmd_tool.is_file():
        raise FileNotFoundError(args.qmd_tool)
    cache_roots = tuple(args.cache_root) or DEFAULT_CACHE_ROOTS
    qmds = _qmd_bytes(
        {
            "3.27": args.qmd_3_27,
            "3.28": args.qmd_3_28,
            "3.28.0.164": args.qmd_3_28_164,
        }
    )
    bases = _target_base_packages()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    download_root = args.output_dir / "tap-base-cache"

    built: list[tuple[bytes, dict, Path, tap.TapPageTurnPackage]] = []
    for base in bases:
        base_archive = _find_base_archive(
            base,
            cache_roots,
            download_root,
            allow_download=not args.no_download,
        )
        variant = _variant(base.release_version)
        first = build_archive(base, base_archive, qmds[variant])
        second = build_archive(base, base_archive, qmds[variant])
        if first != second:
            raise RuntimeError(f"Fast-mono build is not deterministic: {base.package_id}")
        built.append((*first, base_archive, base))
        print(f"base_{base.package_id}={base_archive}")

    manifest = {"schema_version": 1, "packages": [entry for _data, entry, _base_path, _base in built]}
    manifest_data = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    catalog = fast.parse_manifest(manifest_data, require_local_match=False)
    if len(catalog) != len(fast.ALLOWED_TARGETS) or len(catalog) != len(built):
        raise RuntimeError("Generated fast-mono manifest is not the complete allowlist")

    for (archive_data, entry, base_archive, base), package in zip(built, catalog, strict=True):
        output = args.output_dir / entry["asset"]
        _write_atomic(output, archive_data)
        _qmd_check(args.qmd_tool, output, package)
        _combined_qmd_check(args.qmd_tool, base_archive, base, output, package)
        if args.local_cache is not None:
            cached = args.local_cache / package.firmware / package.asset
            _write_atomic(cached, archive_data)
        print(f"archive={output}")
        print(f"archive_sha256={entry['sha256']}")
        print(f"archive_size={entry['size']}")

    manifest_output = args.output_dir / "manifest.json"
    _write_atomic(manifest_output, manifest_data)
    if args.local_cache is not None:
        _write_atomic(args.local_cache / "manifest.json", manifest_data)
    print(f"manifest={manifest_output}")
    print(f"targets={len(built)}")
    print("verification=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
