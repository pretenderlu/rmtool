"""Build and verify the complete native Simplified Chinese package matrix."""

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

import _native_chinese as native
import _tap_page_turn as tap
import _xovi_standalone


QMD_VARIANTS = {
    "3.27": (
        REPO_ROOT / "native-chinese/qmd/chiappa-3.27.3.0.qmd",
        1721,
        "57a44ef3ff56b8d84fd2cafbdd5a678aaf2121c671676886c782b30b9b2e8395",
    ),
    # Keep the original bytes for already published 3.28.162-164 packages.
    "3.28.162-164": (
        REPO_ROOT / "native-chinese/qmd/ferrari-3.28.0.162-164.qmd",
        1802,
        "342bf869065f9b5378fe726b5b73ea9141aa14dc18ad92e780914db19e0b7682",
    ),
    "3.28.166": (
        REPO_ROOT / "native-chinese/qmd/ferrari-3.28.0.166.qmd",
        1569,
        "38800be2f6954fb95c9dfbbf3f6e29a84dde6183d1d385ff757e37c92e5c9e75",
    ),
}
CATALOG_PATHS = {
    ("chiappa", "3.27.1.0"): REPO_ROOT / "translations/reMarkable_zh_CN.qm",
    ("chiappa", "3.27.3.0"): REPO_ROOT / "translations/reMarkable_zh_CN.qm",
    ("ferrari", "3.27.1.0"): REPO_ROOT / "translations/reMarkable_zh_CN_ferrari.qm",
    ("ferrari", "3.27.3.0"): REPO_ROOT / "translations/reMarkable_zh_CN_ferrari.qm",
    ("chiappa", "3.28.0.162"): REPO_ROOT / "translations/reMarkable_zh_CN-20260629074044.qm",
    ("ferrari", "3.28.0.162"): REPO_ROOT / "translations/reMarkable_zh_CN-20260629074044.qm",
    ("chiappa", "3.28.0.163"): REPO_ROOT / "translations/reMarkable_zh_CN-20260629074044.qm",
    ("ferrari", "3.28.0.163"): REPO_ROOT / "translations/reMarkable_zh_CN-20260629074044.qm",
    ("chiappa", "3.28.0.164"): REPO_ROOT / "translations/reMarkable_zh_CN-3.28.0.164-chiappa.qm",
    ("ferrari", "3.28.0.164"): REPO_ROOT / "translations/reMarkable_zh_CN-3.28.0.164-ferrari.qm",
    ("ferrari", "3.28.0.166"): REPO_ROOT / "translations/reMarkable_zh_CN-3.28.0.166-ferrari.qm",
    ("chiappa", "3.28.0.166"): REPO_ROOT / "translations/reMarkable_zh_CN-3.28.0.166-chiappa.qm",
    # 3.28.0.169 ships byte-identical stock catalogs on both platforms, so the
    # exact 3.28.0.166 Chinese catalogs apply unchanged.
    ("ferrari", "3.28.0.169"): REPO_ROOT / "translations/reMarkable_zh_CN-3.28.0.166-ferrari.qm",
    ("chiappa", "3.28.0.169"): REPO_ROOT / "translations/reMarkable_zh_CN-3.28.0.166-chiappa.qm",
}
DEFAULT_CACHE_ROOTS = (
    REPO_ROOT / "build/tap-page-turn-166",
    REPO_ROOT / "build/release-verify-164-current/tap",
    REPO_ROOT / "build/tap-page-turn-164",
    REPO_ROOT / "build/cos-sync/tap-page-turn",
    Path(r"E:\remarkable\firmware-cache\work\tap-matrix\cloud-verify-20260721"),
    Path(r"E:\remarkable\firmware-cache\work\tap-matrix\release-163"),
)
TRANSLATOR_SIZE = 3976
TRANSLATOR_SHA256 = (
    "4408c4ecf1e2774cbbc10374aae544e3d600525663eba8893cdceb83374b8734"
)
TAP_QMD_PATH = "exthome/qt-resource-rebuilder/tap-page-turn.qmd"
HASHTAB_PATH = "exthome/qt-resource-rebuilder/hashtab"
RUNTIME_PATHS = {
    "xovi.so",
    "extensions.d/qt-resource-rebuilder.so",
    HASHTAB_PATH,
    "qmd-tool",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _variant(release_version: str) -> str:
    if release_version == "3.28.0.166":
        return "3.28.166"
    return "3.28.162-164" if release_version.startswith("3.28.") else "3.27"


def _qmd_bytes() -> dict[str, bytes]:
    result = {}
    for name, (path, size, digest) in QMD_VARIANTS.items():
        data = path.read_bytes()
        if len(data) != size or sha256(data) != digest:
            raise RuntimeError(f"Native-Chinese {name} QMD does not match the gate")
        result[name] = data
    return result


def _translation_records() -> dict[tuple[str, str, str], tuple[int, str]]:
    document = json.loads(
        (REPO_ROOT / "translations/manifest.json").read_text(encoding="utf-8")
    )
    records = {}
    for firmware, base in document["firmwares"].items():
        for item in (base, *base.get("variants", ())):
            platform = item.get("platform")
            release = item.get("release_version")
            if platform in {"ferrari", "chiappa"}:
                records[(platform, release, firmware)] = (
                    item["size"],
                    item["sha256"],
                )
    return records


def _catalog_bytes(
    package: tap.TapPageTurnPackage,
    records: dict[tuple[str, str, str], tuple[int, str]],
) -> bytes:
    path = CATALOG_PATHS.get((package.platform, package.release_version))
    # 3.28.0.169 keeps the exact 3.28.0.166 stock carrier bytes, so its catalog
    # is gated by the 3.28.0.166 localization record.
    release = package.release_version
    if release == "3.28.0.169":
        release = "3.28.0.166"
    expected = records.get(
        (package.platform, release, package.firmware)
    )
    if path is None or expected is None:
        raise RuntimeError(
            f"Missing exact Chinese catalog for {package.platform}/{package.release_version}"
        )
    data = path.read_bytes()
    if (len(data), sha256(data)) != expected:
        raise RuntimeError(f"Chinese catalog does not match its manifest: {path}")
    return data


def _target_base_packages() -> tuple[tap.TapPageTurnPackage, ...]:
    catalog = tap.parse_manifest(
        (REPO_ROOT / "tap-page-turn/manifest.json").read_bytes()
    )
    packages = []
    for identity, policy in native.ALLOWED_TARGETS.items():
        firmware, platform, architecture, xochitl_sha256 = identity
        release_version, channel, _offline, _device = policy
        matches = [
            package
            for package in catalog
            if package.firmware == firmware
            and package.platform == platform
            and package.architecture == architecture
            and package.xochitl_sha256 == xochitl_sha256
            and package.release_version == release_version
            and package.channel == channel
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"Tap manifest lacks one exact base for {platform}/{release_version}"
            )
        packages.append(matches[0])
    return tuple(packages)


def _find_base_archive(
    package: tap.TapPageTurnPackage,
    cache_roots: tuple[Path, ...],
) -> Path:
    for root in cache_roots:
        if not root.is_dir():
            continue
        for candidate in (root / package.asset, *root.glob(f"**/{package.asset}")):
            if (
                candidate.is_file()
                and candidate.stat().st_size == package.size
                and sha256(candidate.read_bytes()) == package.sha256
            ):
                return candidate
    raise RuntimeError(f"Missing verified tap base archive: {package.asset}")


def _qmd_check(
    qmd_tool: Path,
    hashtab: bytes,
    qmds: dict[str, bytes],
) -> None:
    orders = (tuple(qmds), tuple(reversed(qmds))) if len(qmds) > 1 else (tuple(qmds),)
    for order in orders:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hashtabs = root / "hashtabs"
            qmd_dir = root / "qmd"
            hashtabs.mkdir()
            qmd_dir.mkdir()
            (hashtabs / "hashtab-target").write_bytes(hashtab)
            for index, name in enumerate(order):
                (qmd_dir / f"{index:02d}-{name}.qmd").write_bytes(qmds[name])
            result = subprocess.run(
                [
                    str(qmd_tool),
                    "check",
                    "-hashtabs",
                    str(hashtabs),
                    "-qmd",
                    str(qmd_dir),
                ],
                capture_output=True,
                check=False,
            )
            output = result.stdout + result.stderr
            if result.returncode or b"ALL OK" not in output:
                raise RuntimeError(output.decode("utf-8", errors="replace"))


def build_target(
    package: tap.TapPageTurnPackage,
    base_archive: Path,
    qmd: bytes,
    catalog: bytes,
    qmd_tool: Path,
) -> tuple[dict, bytes]:
    with tempfile.TemporaryDirectory() as temporary:
        extracted = tap.extract_verified_package(base_archive, package, temporary)
        files = {
            path: (
                extracted.joinpath(*PurePosixPath(path).parts).read_bytes(),
                package.file(path).mode,
            )
            for path in sorted(RUNTIME_PATHS)
        }
        tap_qmd = extracted.joinpath(*PurePosixPath(TAP_QMD_PATH).parts).read_bytes()

    translator = (
        REPO_ROOT / "native-chinese/native-chinese-translator.so"
    ).read_bytes()
    if len(translator) != TRANSLATOR_SIZE or sha256(translator) != TRANSLATOR_SHA256:
        raise RuntimeError("Native-Chinese translator does not match the gate")
    files[native.QMD_PATH] = (qmd, 0o644)
    files[native.EXTENSION_PATH] = (translator, 0o644)
    files[native.CATALOG_PATH] = (catalog, 0o644)
    if set(files) != native.PAYLOAD_PATHS:
        raise RuntimeError("Native-Chinese payload whitelist mismatch")

    hashtab = files[HASHTAB_PATH][0]
    _qmd_check(qmd_tool, hashtab, {"native": qmd})
    _qmd_check(qmd_tool, hashtab, {"native": qmd, "tap": tap_qmd})

    archive = tap._gzip_member(
        tap._tar_member(files, apk_checksums=False, include_directories=False)
    )
    if archive != tap._gzip_member(
        tap._tar_member(files, apk_checksums=False, include_directories=False)
    ):
        raise RuntimeError("Native-Chinese package build is not deterministic")

    identity = (
        package.firmware,
        package.platform,
        package.architecture,
        package.xochitl_sha256,
    )
    release_version, channel, offline_verified, device_verified = (
        native.ALLOWED_TARGETS[identity]
    )
    asset = native.EXPECTED_ASSETS[identity]
    entry = {
        "firmware": package.firmware,
        "release_version": release_version,
        "channel": channel,
        "platform": package.platform,
        "architecture": package.architecture,
        "xochitl_sha256": package.xochitl_sha256,
        "offline_verified": offline_verified,
        "device_verified": device_verified,
        "asset": asset,
        "sha256": sha256(archive),
        "size": len(archive),
        "urls": [f"{native.COS_URL}/{asset}", f"{native.GITHUB_URL}/{asset}"],
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
    return entry, archive


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", action="append", type=Path, default=[])
    parser.add_argument("--qmd-tool", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "build/native-chinese",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=REPO_ROOT / "native-chinese/manifest.json",
    )
    parser.add_argument("--local-cache", type=Path)
    parser.add_argument("--target-firmware")
    parser.add_argument("--target-platform")
    args = parser.parse_args()
    if not args.qmd_tool.is_file():
        raise FileNotFoundError(args.qmd_tool)

    cache_roots = tuple(args.cache_root) or DEFAULT_CACHE_ROOTS
    qmds = _qmd_bytes()
    records = _translation_records()
    base_packages = _target_base_packages()
    if bool(args.target_firmware) != bool(args.target_platform):
        raise RuntimeError("Target firmware and platform must be provided together")
    if args.target_firmware:
        base_packages = tuple(
            package
            for package in base_packages
            if package.firmware == args.target_firmware
            and package.platform == args.target_platform
        )
        if len(base_packages) != 1:
            raise RuntimeError("Target must select exactly one native-Chinese package")

    built = []
    for package in base_packages:
        archive = _find_base_archive(package, cache_roots)
        built.append(
            build_target(
                package,
                archive,
                qmds[_variant(package.release_version)],
                _catalog_bytes(package, records),
                args.qmd_tool,
            )
        )

    entries = [entry for entry, _ in built]
    if args.target_firmware:
        previous = json.loads(args.manifest.read_text(encoding="utf-8"))
        replacements = {
            (
                entry["firmware"],
                entry["platform"],
                entry["architecture"],
                entry["xochitl_sha256"],
            ): entry
            for entry in entries
        }
        entries = [
            replacements.get(
                (
                    entry["firmware"],
                    entry["platform"],
                    entry["architecture"],
                    entry["xochitl_sha256"],
                ),
                entry,
            )
            for entry in previous["packages"]
        ]
    manifest = (
        json.dumps(
            {"schema_version": 1, "packages": entries},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    packages = native.parse_manifest(manifest)
    if len(packages) != len(native.ALLOWED_TARGETS):
        raise RuntimeError("Generated native-Chinese manifest is incomplete")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    built_by_identity = {
        (
            entry["firmware"],
            entry["platform"],
            entry["architecture"],
            entry["xochitl_sha256"],
        ): archive
        for entry, archive in built
    }
    for package in packages:
        identity = (
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        archive = built_by_identity.get(identity)
        if archive is None:
            continue
        output = args.output_dir / package.asset
        tap._write_atomic(output, archive)
        with tempfile.TemporaryDirectory() as temporary:
            extracted = tap.extract_verified_package(output, package, temporary)
            runtime, feature = native._shared_specs(package)
            _xovi_standalone.assert_feature_layout(runtime, (feature,))
            for item in package.files:
                path = extracted.joinpath(*PurePosixPath(item.path).parts)
                if sha256(path.read_bytes()) != item.sha256:
                    raise RuntimeError(f"Extracted file mismatch: {item.path}")
        if args.local_cache is not None:
            cached = args.local_cache / package.firmware / package.asset
            tap._write_atomic(cached, archive)
        print(f"archive={output}")
        print(f"archive_sha256={package.sha256}")
        print(f"archive_size={package.size}")

    tap._write_atomic(args.manifest, manifest)
    print(f"manifest={args.manifest}")
    print(f"targets={len(packages)}")
    print("verification=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
