"""Build the exact Paper Pro 3.28.0.166 tap-to-turn package."""

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

import _tap_page_turn as tap


TARGET_IDENTITY = (
    "20260806095513",
    "ferrari",
    "aarch64",
    "8726b4fce55a9154a5014956e5204401ce881d752c1ff3813adb622a68aac2f9",
)
TARGET_RELEASE = "3.28.0.166"
TARGET_ASSET = (
    "rmtool-tap-page-turn-ferrari-20260806095513-3.28.0.166.tar.gz"
)
HASHTAB_SIZE = 624139
HASHTAB_SHA256 = (
    "014fdc004442c4f0e66fff5dd32e940e4c4e8e9a2f14f36a1f707f25c4032035"
)
HASHTAB_PATH = "exthome/qt-resource-rebuilder/hashtab"
QMD_PATH = "exthome/qt-resource-rebuilder/tap-page-turn.qmd"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _qmd_check(qmd_tool: Path, hashtab: bytes, qmd: bytes) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        hashtabs = root / "hashtabs"
        qmds = root / "qmd"
        hashtabs.mkdir()
        qmds.mkdir()
        (hashtabs / "hashtab-ferrari-20260806095513").write_bytes(hashtab)
        (qmds / "tap-page-turn.qmd").write_bytes(qmd)
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
        output = result.stdout + result.stderr
        if result.returncode or b"ALL OK" not in output:
            raise RuntimeError(output.decode("utf-8", errors="replace"))


def _build(
    base: tap.TapPageTurnPackage,
    base_archive: Path,
    hashtab_path: Path,
    qmd_tool: Path,
) -> tuple[dict, bytes]:
    hashtab = hashtab_path.read_bytes()
    if len(hashtab) != HASHTAB_SIZE or sha256(hashtab) != HASHTAB_SHA256:
        raise RuntimeError("Paper Pro 3.28.0.166 hashtab does not match the gate")
    with tempfile.TemporaryDirectory() as temporary:
        extracted = tap.extract_verified_package(base_archive, base, temporary)
        files = {
            item.path: (
                extracted.joinpath(*PurePosixPath(item.path).parts).read_bytes(),
                item.mode,
            )
            for item in base.files
        }
    files[HASHTAB_PATH] = (hashtab, 0o644)
    _qmd_check(qmd_tool, hashtab, files[QMD_PATH][0])
    archive = tap._gzip_member(
        tap._tar_member(files, apk_checksums=False, include_directories=False)
    )
    if archive != tap._gzip_member(
        tap._tar_member(files, apk_checksums=False, include_directories=False)
    ):
        raise RuntimeError("Tap-to-turn package build is not deterministic")
    firmware, platform, architecture, xochitl_sha256 = TARGET_IDENTITY
    entry = {
        "firmware": firmware,
        "release_version": TARGET_RELEASE,
        "channel": "beta",
        "platform": platform,
        "architecture": architecture,
        "xochitl_sha256": xochitl_sha256,
        "asset": TARGET_ASSET,
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
    parser.add_argument("--hashtab", type=Path, required=True)
    parser.add_argument("--qmd-tool", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=REPO_ROOT / "tap-page-turn/manifest.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "build/tap-page-turn-166",
    )
    args = parser.parse_args()

    catalog = tap.parse_manifest(args.manifest.read_bytes())
    base = next(
        package
        for package in catalog
        if package.platform == "ferrari"
        and package.release_version == "3.28.0.164"
    )
    if (
        args.base_archive.stat().st_size != base.size
        or sha256(args.base_archive.read_bytes()) != base.sha256
    ):
        raise RuntimeError("The 3.28.0.164 base archive does not match its manifest")
    entry, archive = _build(base, args.base_archive, args.hashtab, args.qmd_tool)

    document = json.loads(args.manifest.read_text(encoding="utf-8"))
    document["packages"] = [
        item
        for item in document["packages"]
        if not (
            item["firmware"] == TARGET_IDENTITY[0]
            and item["platform"] == TARGET_IDENTITY[1]
        )
    ]
    document["packages"].append(entry)
    manifest = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    tap.parse_manifest(manifest)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / TARGET_ASSET
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
