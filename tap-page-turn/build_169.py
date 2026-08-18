"""Build the exact Paper Pro and Paper Pro Move 3.28.0.169 tap-to-turn packages."""

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


TARGETS = {
    "ferrari": {
        "identity": (
            "20260806095513",
            "ferrari",
            "aarch64",
            "43a9d5d0acc5b998264c16586e11b848f3b83d2d63b5fd322b09c0977d94d3d4",
        ),
        "release": "3.28.0.169",
        "base_release": "3.28.0.166",
        "asset": "rmtool-tap-page-turn-ferrari-20260806095513-3.28.0.169.tar.gz",
        "hashtab_size": 624183,
        "hashtab_sha256": (
            "d2af090d3da8f88f883119ca62b1e3313c0bb0cc1e7584bc1dd6a9b5a10f894d"
        ),
    },
    "chiappa": {
        "identity": (
            "20260806095513",
            "chiappa",
            "aarch64",
            "6361610111c381ce730a8bfcc889bd933ef5fef173563a9156e435233714e7ee",
        ),
        "release": "3.28.0.169",
        "base_release": "3.28.0.166",
        "asset": "rmtool-tap-page-turn-chiappa-20260806095513-3.28.0.169.tar.gz",
        "hashtab_size": 618942,
        "hashtab_sha256": (
            "1cb01571664742f3d7d5767cd23498b50effc5e1d077ee3e1058b5f1259301b1"
        ),
    },
}
HASHTAB_PATH = "exthome/qt-resource-rebuilder/hashtab"
QMD_PATH = "exthome/qt-resource-rebuilder/tap-page-turn.qmd"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _qmd_check(qmd_tool: Path, hashtab: bytes, qmd: bytes, platform: str) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        hashtabs = root / "hashtabs"
        qmds = root / "qmd"
        hashtabs.mkdir()
        qmds.mkdir()
        (hashtabs / f"hashtab-{platform}-20260806095513").write_bytes(hashtab)
        (qmds / "tap-page-turn.qmd").write_bytes(qmd)
        result = subprocess.run(
            [str(qmd_tool), "check", "-hashtabs", str(hashtabs), "-qmd", str(qmds)],
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
    spec: dict,
) -> tuple[dict, bytes]:
    hashtab = hashtab_path.read_bytes()
    if len(hashtab) != spec["hashtab_size"] or sha256(hashtab) != spec["hashtab_sha256"]:
        raise RuntimeError(f"{spec['identity'][1]} 3.28.0.169 hashtab does not match the gate")
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
    _qmd_check(qmd_tool, hashtab, files[QMD_PATH][0], spec["identity"][1])
    archive = tap._gzip_member(
        tap._tar_member(files, apk_checksums=False, include_directories=False)
    )
    if archive != tap._gzip_member(
        tap._tar_member(files, apk_checksums=False, include_directories=False)
    ):
        raise RuntimeError("Tap-to-turn package build is not deterministic")
    firmware, platform, architecture, xochitl_sha256 = spec["identity"]
    entry = {
        "firmware": firmware,
        "release_version": spec["release"],
        "channel": "beta",
        "platform": platform,
        "architecture": architecture,
        "xochitl_sha256": xochitl_sha256,
        "asset": spec["asset"],
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
    parser.add_argument("--qmd-tool", type=Path, required=True)
    parser.add_argument(
        "--hashtab-root",
        type=Path,
        default=Path(
            r"E:\remarkable\firmware-cache\work\tap-matrix\matrix-169"
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=REPO_ROOT / "tap-page-turn/manifest.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "build/tap-page-turn-169",
    )
    args = parser.parse_args()

    catalog = tap.parse_manifest(args.manifest.read_bytes())
    document = json.loads(args.manifest.read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for platform, spec in TARGETS.items():
        base = next(
            package
            for package in catalog
            if package.platform == platform
            and package.release_version == spec["base_release"]
        )
        base_archive = args.output_dir.parent / "tap-page-turn-166" / base.asset
        if (
            base_archive.stat().st_size != base.size
            or sha256(base_archive.read_bytes()) != base.sha256
        ):
            raise RuntimeError(
                f"The {spec['base_release']} base archive for {platform} does not match its manifest"
            )
        hashtab = (
            args.hashtab_root / f"{platform}-20260806095513-169" / "hashtab-canonical"
        )
        entry, archive = _build(base, base_archive, hashtab, args.qmd_tool, spec)
        document["packages"] = [
            item
            for item in document["packages"]
            if not (
                item["firmware"] == spec["identity"][0]
                and item["platform"] == platform
                and item["xochitl_sha256"] == spec["identity"][3]
            )
        ]
        document["packages"].append(entry)
        output = args.output_dir / spec["asset"]
        tap._write_atomic(output, archive)
        print(f"archive={output}")
        print(f"archive_sha256={entry['sha256']}")
        print(f"archive_size={entry['size']}")

    manifest = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    tap.parse_manifest(manifest)
    tap._write_atomic(args.manifest, manifest)
    print(f"manifest={args.manifest}")
    print("verification=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
