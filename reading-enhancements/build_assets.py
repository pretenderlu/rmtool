"""Build the exact-firmware reading-enhancements asset matrix.

Trusted tap-page-turn archives provide the runtime carrier. Only the QMD is
replaced, and every target is compiled and replayed against its own cached
qrex tree before an archive or manifest entry is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import _reading_enhancements as reading
import _tap_page_turn as tap


BASE_QMD_PATH = "exthome/qt-resource-rebuilder/tap-page-turn.qmd"
QREX_FILES = (
    "qml/device/view/settings/Settings.qml",
    "qml/device/view/documentview/SceneViewGestures.qml",
    "qml/device/view/documentview/DocumentView.qml",
    "qt/qml/xofm/libs/toolbar/qml/SettingsMenu.qml",
    "qml/device/view/documentview/FormatFont.qml",
    "qml/device/view/documentview/Pages.qml",
)
EPUB_FONT_328_START = "; RMTOOL_EPUB_FONT_328_START"
EPUB_FONT_328_END = "; RMTOOL_EPUB_FONT_328_END"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _component_indent(source: str, component_id: str) -> str:
    matches = re.findall(
        rf"(?m)^([ \t]*)Component\s*\{{\s*\n[ \t]*id:\s*{re.escape(component_id)}\b",
        source,
    )
    if len(matches) != 1:
        raise RuntimeError(f"expected one Component#{component_id}, found {len(matches)}")
    return matches[0]


def _variant_for_release(release_version: str) -> str:
    return "3.27" if release_version.startswith("3.27.") else "3.28"


def _main_view_variant(source: str) -> str:
    navigation_fallback = """                } else {
                    rmtoolSettingsRoot._selectedIndex = page
                }
"""
    if source.count(navigation_fallback) != 1:
        raise RuntimeError("reading-enhancements source lacks the 3.27 navigation fallback")
    source = source.replace(
        navigation_fallback,
        """                } else {
                    rmtoolSettingsRoot._selectedIndex = page
                    rmtoolSettingsRoot.highlightedIndex = page
                }
""",
        1,
    )
    settings_root = "    TRAVERSE Item#root\n        LOCATE BEFORE Component#general"
    if settings_root not in source:
        raise RuntimeError("reading-enhancements source lacks the 3.28 Settings root boundary")
    source = source.replace(
        settings_root,
        "    TRAVERSE DeviceKeyboardNavigationHandler#settings\n"
        "        LOCATE BEFORE Component#general",
        1,
    )
    old = """    TRAVERSE ?[!mode][!visible]
        REPLACE visible WITH {
            visible: (rmtoolFastMonoReadingAvailable && rmtoolFastMonoReadingEnabled)
                || sceneView.globalScreenMode != undefined
        }
        REPLACE mode WITH {
            mode: rmtoolFastMonoReadingAvailable && rmtoolFastMonoReadingEnabled
                ? Epaper.ScreenModeItem.Mono
                : (visible ? sceneView.globalScreenMode : Epaper.ScreenModeItem.UI)
        }
    END TRAVERSE
END AFFECT
"""
    new = """END AFFECT

AFFECT /qml/device/view/main/MainView.qml
    TRAVERSE ?#globalScreenMode
        REPLACE visible WITH {
            visible: {
                if (!documentView.item) {
                    return false
                }
                if (documentView.visible
                    && documentView.item.rmtoolFastMonoReadingAvailable
                    && documentView.item.rmtoolFastMonoReadingEnabled) {
                    return true
                }
                const mode = documentView.item.globalScreenMode
                return mode !== undefined
            }
        }
        REPLACE mode WITH {
            mode: {
                if (documentView.visible
                    && documentView.item?.rmtoolFastMonoReadingAvailable
                    && documentView.item.rmtoolFastMonoReadingEnabled) {
                    return Epaper.ScreenModeItem.Mono
                }
                if (documentView.item && documentView.item.globalScreenMode) {
                    return documentView.item.globalScreenMode
                }
                return Epaper.ScreenModeItem.UI
            }
        }
    END TRAVERSE
END AFFECT
"""
    if old not in source:
        raise RuntimeError("reading-enhancements source lacks the 3.28 screen-mode boundary")
    return source.replace(old, new, 1)


def _strip_epub_font_328(source: str) -> str:
    if source.count(EPUB_FONT_328_START) != 1 or source.count(EPUB_FONT_328_END) != 1:
        raise RuntimeError("reading-enhancements source lacks one 3.28 EPUB font block")
    before, remainder = source.split(EPUB_FONT_328_START, 1)
    _block, after = remainder.split(EPUB_FONT_328_END, 1)
    return before.rstrip() + "\n" + after.lstrip("\n")


def _source_for_release(source: Path, release_version: str, temporary: Path) -> Path:
    text = source.read_text(encoding="utf-8")
    if _variant_for_release(release_version) == "3.27":
        text = _strip_epub_font_328(_main_view_variant(text))
    temporary.mkdir(parents=True, exist_ok=True)
    destination = temporary / f"reading-enhancements-{_variant_for_release(release_version)}.qmd"
    destination.write_text(text, encoding="utf-8", newline="\n")
    return destination


def _run(command: list[str], *, label: str) -> bytes:
    result = subprocess.run(command, capture_output=True, check=False)
    output = result.stdout + result.stderr
    if result.returncode:
        raise RuntimeError(f"{label} failed: {output.decode(errors='replace')}")
    return result.stdout


def _compile_and_validate(*, qmd_tool: Path, qmldiff: Path, source: Path, target: dict, work: Path) -> bytes:
    target_root = Path(target["qrex_root"])
    hashtab = target_root / target["hashtab"]
    qrex = target_root / "qrex-out"
    if not hashtab.is_file() or not qrex.is_dir():
        raise RuntimeError(f"missing qrex validation tree for {target['id']}")
    compiled = _run(
        [str(qmd_tool), "hash", "-hashtab", str(hashtab), str(source)],
        label=f"compile {target['id']}",
    )
    if not compiled:
        raise RuntimeError(f"compile {target['id']} produced an empty QMD")

    check_root = work / target["id"]
    hashtabs = check_root / "hashtabs"
    qmds = check_root / "qmd"
    replay = check_root / "replay"
    hashtabs.mkdir(parents=True)
    qmds.mkdir()
    shutil.copy2(hashtab, hashtabs / f"hashtab-{target['platform']}-{target['firmware']}")
    (qmds / "reading-enhancements.qmd").write_bytes(compiled)
    checked = _run(
        [str(qmd_tool), "check", "-hashtabs", str(hashtabs), "-qmd", str(qmds)],
        label=f"qmd check {target['id']}",
    )
    if b"ALL OK" not in checked:
        raise RuntimeError(f"qmd check {target['id']} did not report ALL OK")
    compatible = _run(
        [str(qmldiff), "check-compatibility", str(hashtab), str(qmds / "reading-enhancements.qmd")],
        label=f"qmldiff compatibility {target['id']}",
    )
    if b"No compatibility errors found" not in compatible:
        raise RuntimeError(f"qmldiff compatibility {target['id']} was incomplete")
    _run(
        [str(qmldiff), "apply-diffs", "--hashtab", str(hashtab), "-c", str(qrex), str(replay), str(qmds / "reading-enhancements.qmd")],
        label=f"qmldiff replay {target['id']}",
    )
    settings = (replay / QREX_FILES[0]).read_text(encoding="utf-8")
    gestures = (replay / QREX_FILES[1]).read_text(encoding="utf-8")
    document = (replay / QREX_FILES[2]).read_text(encoding="utf-8")
    menu = (replay / QREX_FILES[3]).read_text(encoding="utf-8")
    pages = (replay / QREX_FILES[5]).read_text(encoding="utf-8")
    for text, markers in (
        (settings, ("rmtoolReadingEnhancementsPage", "RmtoolReadingEnhancements")),
        (gestures, ("rmtoolTapPageDirection",)),
        (
            document,
            (
                "rmtoolHasUsableToc",
                "forceClearNow",
                "onRmtoolReadingDocumentIdChanged",
                "rmtoolTableOfContentsAvailable",
                "root.requestTableOfContents(true)",
                "toolbar.selectLastTool()",
                "pages.rmtoolOpenTableOfContents()",
            ),
        ),
        (
            menu,
            (
                "rmtoolTableOfContentsItem",
                "rmtoolTapPageTurnToggle",
                "rmtoolFastMonoToggle",
                "rmtoolCleanupSelector",
            ),
        ),
        (
            pages,
            (
                "function rmtoolOpenTableOfContents()",
                'reportPageAction("Table of Content")',
                "documentView.rmtoolRequestTableOfContents()",
                "tableOfContentsLoader.active = true",
            ),
        ),
    ):
        for marker in markers:
            if marker not in text:
                raise RuntimeError(f"structure assertion {target['id']} missing {marker}")
    if not re.search(
        r"function\s+rmtoolOpenTableOfContents\(\)\s*\{\s*"
        r"openOverview\(\)\s*toolbar\.selectLastTool\(\)\s*"
        r"pages\.rmtoolOpenTableOfContents\(\)\s*\}",
        document,
    ):
        raise RuntimeError(
            f"structure assertion {target['id']} changed the TOC wrapper call order"
        )
    if pages.count("TableOfContent {") != 1:
        raise RuntimeError(
            f"structure assertion {target['id']} did not preserve the stock TOC loader"
        )
    if not re.search(
        r"readonly property\s+bool\s+rmtoolReadingAvailable:\s*!!document\s*&&\s*!notePage",
        document,
    ):
        raise RuntimeError(
            f"structure assertion {target['id']} did not exclude note pages"
        )
    if re.search(r"Component\s*\{\s*Component\s*\{", settings):
        raise RuntimeError(f"structure assertion {target['id']} nested a Component in Component")
    if _component_indent(settings, "general") != _component_indent(
        settings, "rmtoolReadingEnhancements"
    ):
        raise RuntimeError(
            f"structure assertion {target['id']} nested the reading page component"
        )
    is_327 = target["firmware"] in {"20260506100933", "20260612085811"}
    if is_327:
        for marker in (
            "rmtoolSettingsRoot._selectedIndex = page",
            "rmtoolSettingsRoot.highlightedIndex = page",
            "settings.sideBarItemClicked(settings.highlightedIndex);",
        ):
            if marker not in settings:
                raise RuntimeError(
                    f"structure assertion {target['id']} missing 3.27 navigation marker {marker}"
                )
        main = (replay / "qml/device/view/main/MainView.qml").read_text(encoding="utf-8")
        if "rmtoolFastMonoReadingEnabled" not in main:
            raise RuntimeError(f"structure assertion {target['id']} missing MainView fast-mono hook")
    else:
        font_menu = (replay / QREX_FILES[4]).read_text(encoding="utf-8")
        for marker in (
            "rmtoolEpubFont1",
            "rmtoolEpubFont3",
            "slot-1.label",
            "slot-3.ttf",
            'fontModel.setProperty(existing, "value", label)',
            "key: loader.name",
            "value: label",
        ):
            if marker not in font_menu:
                raise RuntimeError(
                    f"structure assertion {target['id']} missing EPUB font marker {marker}"
                )
        for marker in (
            "rmtoolSettingsRoot._selectedPage = page",
            "root.sideBarItemClicked(root._selectedPage);",
        ):
            if marker not in settings:
                raise RuntimeError(
                    f"structure assertion {target['id']} missing 3.28 navigation marker {marker}"
                )
        if "rmtoolSettingsRoot.highlightedIndex = page" in settings:
            raise RuntimeError(
                f"structure assertion {target['id']} unexpectedly changed 3.28 navigation"
            )
    return compiled


def _load_matrix_config(path: Path) -> dict[tuple[str, str, str], dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    raw_targets = document.get("targets") if isinstance(document, dict) else None
    if not isinstance(raw_targets, list):
        raise RuntimeError("matrix config must contain a targets list")
    result = {}
    for item in raw_targets:
        if not isinstance(item, dict):
            raise RuntimeError("matrix config target is invalid")
        key = (item.get("platform"), item.get("firmware"), item.get("xochitl_sha256"))
        if any(not isinstance(value, str) for value in key):
            raise RuntimeError("matrix config target identity is incomplete")
        if key in result:
            raise RuntimeError(f"duplicate matrix target: {key}")
        item = dict(item)
        item["id"] = f"{key[0]}-{key[1]}-{key[2][:12]}"
        result[key] = item
    return result


def _find_base_archive(package, cache_roots: tuple[Path, ...], download_root: Path) -> Path:
    for root in cache_roots:
        if not root.is_dir():
            continue
        for candidate in dict.fromkeys((root / package.asset, *root.glob(f"**/{package.asset}"))):
            if candidate.is_file() and candidate.stat().st_size == package.size and sha256(candidate.read_bytes()) == package.sha256:
                return candidate
    data = tap._download_limited(package.download_url, tap.MAX_PACKAGE_BYTES)
    if len(data) != package.size or sha256(data) != package.sha256:
        raise RuntimeError(f"downloaded tap carrier failed verification: {package.asset}")
    destination = download_root / package.asset
    tap._write_atomic(destination, data)
    return destination


def _build_archive(base, base_archive: Path, qmd: bytes) -> tuple[bytes, dict]:
    if base_archive.stat().st_size != base.size or sha256(base_archive.read_bytes()) != base.sha256:
        raise RuntimeError(f"tap carrier failed verification: {base.asset}")
    with tempfile.TemporaryDirectory() as temporary:
        extracted = tap.extract_verified_package(base_archive, base, temporary)
        files = {
            item.path: (extracted.joinpath(*PurePosixPath(item.path).parts).read_bytes(), item.mode)
            for item in base.files
            if item.path != BASE_QMD_PATH
        }
    files[reading.QMD_PAYLOAD_PATH] = (qmd, 0o644)
    if set(files) != reading._PAYLOAD_PATHS:
        raise RuntimeError("reading-enhancements payload path set drifted from carrier")
    archive = tap._gzip_member(tap._tar_member(files, apk_checksums=False, include_directories=False))
    if archive != tap._gzip_member(tap._tar_member(files, apk_checksums=False, include_directories=False)):
        raise RuntimeError("reading-enhancements build is not deterministic")
    identity = (base.platform, base.firmware, base.architecture, base.xochitl_sha256)
    release, channel, offline, device = reading.ALLOWED_TARGETS[identity]
    asset = reading._expected_asset_name(base.platform, base.firmware, release)
    entry = {
        "firmware": base.firmware,
        "release_version": release,
        "channel": channel,
        "platform": base.platform,
        "architecture": base.architecture,
        "xochitl_sha256": base.xochitl_sha256,
        "offline_verified": offline,
        "device_verified": device,
        "package_revision": reading.PACKAGE_REVISION,
        "asset": asset,
        "sha256": sha256(archive),
        "size": len(archive),
        "urls": [
            f"{origin}/{asset}" for origin in reading.REMOTE_BASE_URLS
        ],
        "files": [
            {"path": path, "sha256": sha256(data), "size": len(data), "mode": mode}
            for path, (data, mode) in sorted(files.items())
        ],
    }
    return archive, entry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qmd-tool", type=Path, required=True)
    parser.add_argument("--qmldiff", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=REPO_ROOT / "reading-enhancements/qmd-src/reading-enhancements-3.28.qmd")
    parser.add_argument("--matrix-config", type=Path, required=True)
    parser.add_argument("--base-manifest", type=Path, default=REPO_ROOT / "tap-page-turn/manifest.json")
    parser.add_argument("--cache-root", action="append", type=Path, default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--write-manifest", type=Path, required=True)
    args = parser.parse_args()

    tap_catalog = tap.parse_manifest(args.base_manifest.read_bytes())
    matrix = _load_matrix_config(args.matrix_config)
    expected_keys = set(reading.ALLOWED_TARGETS)
    expected_config = {(key[0], key[1], key[3]) for key in expected_keys}
    if set(matrix) != expected_config:
        raise RuntimeError("matrix config does not cover the exact 14 reading targets")
    cache_roots = tuple(args.cache_root) or (REPO_ROOT / ".rmtool/cache/tap-page-turn",)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    download_root = args.output_dir / "tap-base-cache"
    entries = []
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        for key in sorted(reading.ALLOWED_TARGETS, key=lambda item: (item[1], item[0], item[3])):
            platform, firmware, architecture, xochitl_sha = key
            release, channel, _offline, _device = reading.ALLOWED_TARGETS[key]
            target = matrix[(platform, firmware, xochitl_sha)]
            base_matches = [
                item for item in tap_catalog
                if (item.platform, item.firmware, item.architecture, item.xochitl_sha256) == key
                and item.release_version == release and item.channel == channel
            ]
            if len(base_matches) != 1:
                raise RuntimeError(f"tap manifest has no unique carrier for {platform}/{release}")
            base = base_matches[0]
            source = _source_for_release(args.source, release, work)
            qmd = _compile_and_validate(qmd_tool=args.qmd_tool, qmldiff=args.qmldiff, source=source, target=target, work=work / "validation")
            archive, entry = _build_archive(base, _find_base_archive(base, cache_roots, download_root), qmd)
            output = args.output_dir / entry["asset"]
            tap._write_atomic(output, archive)
            entries.append(entry)
            print(f"{entry['platform']} {entry['release_version']}: {output} {entry['sha256']} {entry['size']}")

    manifest_data = (json.dumps({"schema_version": 1, "packages": entries}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    reading.parse_manifest(manifest_data, require_local_match=False)
    tap._write_atomic(args.write_manifest, manifest_data)
    print(f"manifest: {args.write_manifest}")
    print(f"targets: {len(entries)}")
    print("verification: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
