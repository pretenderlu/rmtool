"""Build the exact Paper Pro 3.28.0.166 fast-monochrome package."""

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
import _tap_page_turn as tap


TARGET_IDENTITY = (
    "ferrari",
    "20260806095513",
    "aarch64",
    "8726b4fce55a9154a5014956e5204401ce881d752c1ff3813adb622a68aac2f9",
)
TARGET_RELEASE = "3.28.0.166"
BASE_QMD_PATH = "exthome/qt-resource-rebuilder/tap-page-turn.qmd"
HASHTAB_PATH = "exthome/qt-resource-rebuilder/hashtab"
QMD_SIZE = 11407
QMD_SHA256 = (
    "5ad0a13fff4a49716b2b2c31cf96a048d5f3cf23a6d6f615ea874c5043a3554f"
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
            (hashtabs / "hashtab-ferrari-20260806095513").write_bytes(hashtab)
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


def _build(
    base: tap.TapPageTurnPackage,
    base_archive: Path,
    qmd_path: Path,
    qmd_tool: Path,
) -> tuple[dict, bytes]:
    qmd = qmd_path.read_bytes()
    if len(qmd) != QMD_SIZE or sha256(qmd) != QMD_SHA256:
        raise RuntimeError("Fast-monochrome r4 QMD does not match the gate")
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
        tap_qmd = extracted.joinpath(*PurePosixPath(BASE_QMD_PATH).parts).read_bytes()
    files[fast.QMD_PAYLOAD_PATH] = (qmd, 0o644)
    if set(files) != fast._PAYLOAD_PATHS:
        raise RuntimeError("Fast-monochrome payload whitelist mismatch")

    hashtab = files[HASHTAB_PATH][0]
    _qmd_check(qmd_tool, hashtab, {"fast": qmd})
    _qmd_check(qmd_tool, hashtab, {"tap": tap_qmd, "fast": qmd})
    archive = tap._gzip_member(
        tap._tar_member(files, apk_checksums=False, include_directories=False)
    )
    if archive != tap._gzip_member(
        tap._tar_member(files, apk_checksums=False, include_directories=False)
    ):
        raise RuntimeError("Fast-monochrome package build is not deterministic")

    release_version, channel, offline_verified, device_verified = (
        fast.ALLOWED_TARGETS[TARGET_IDENTITY]
    )
    platform, firmware, architecture, xochitl_sha256 = TARGET_IDENTITY
    asset = fast._expected_asset_name(platform, firmware, release_version)
    entry = {
        "firmware": firmware,
        "release_version": release_version,
        "channel": channel,
        "platform": platform,
        "architecture": architecture,
        "xochitl_sha256": xochitl_sha256,
        "offline_verified": offline_verified,
        "device_verified": device_verified,
        "package_revision": 4,
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
    return entry, archive


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-archive", type=Path, required=True)
    parser.add_argument("--qmd", type=Path, required=True)
    parser.add_argument("--qmd-tool", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=REPO_ROOT / "fast-mono-reading/manifest.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "build/fast-mono-reading-166",
    )
    args = parser.parse_args()

    tap_catalog = tap.parse_manifest(
        (REPO_ROOT / "tap-page-turn/manifest.json").read_bytes()
    )
    base = next(
        package
        for package in tap_catalog
        if (
            package.platform,
            package.firmware,
            package.architecture,
            package.xochitl_sha256,
        )
        == TARGET_IDENTITY
    )
    if (
        args.base_archive.stat().st_size != base.size
        or sha256(args.base_archive.read_bytes()) != base.sha256
    ):
        raise RuntimeError("The Paper Pro 3.28.0.166 tap base is not exact")
    entry, archive = _build(base, args.base_archive, args.qmd, args.qmd_tool)

    document = json.loads(args.manifest.read_text(encoding="utf-8"))
    document["packages"] = [
        item
        for item in document["packages"]
        if not (
            item["firmware"] == TARGET_IDENTITY[1]
            and item["platform"] == TARGET_IDENTITY[0]
        )
    ]
    document["packages"].append(entry)
    manifest = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    fast.parse_manifest(manifest, require_local_match=False)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / entry["asset"]
    tap._write_atomic(output, archive)
    tap._write_atomic(args.manifest, manifest)
    print(f"archive={output}")
    print(f"archive_sha256={entry['sha256']}")
    print(f"archive_size={entry['size']}")
    print(f"manifest={args.manifest}")
    print("verification=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
