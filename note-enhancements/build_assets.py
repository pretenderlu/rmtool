"""Build and offline-validate exact note-enhancements packages."""

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
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import _note_enhancements as note
import _tap_page_turn as tap


BASE_QMD_PATH = "exthome/qt-resource-rebuilder/tap-page-turn.qmd"
SETTINGS_QML = "qml/device/view/settings/Settings.qml"
SETTINGS_MENU_QML = "qt/qml/xofm/libs/toolbar/qml/SettingsMenu.qml"
DOCUMENT_QML = "qml/device/view/documentview/DocumentView.qml"
SCENE_QML = "qml/device/view/documentview/DeviceSceneView.qml"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _variant_for_release(release_version: str) -> str:
    return "3.27" if release_version.startswith("3.27.") else "3.28"


def _source_for_release(source: Path, release_version: str, temporary: Path) -> Path:
    text = source.read_text(encoding="utf-8")
    variant = _variant_for_release(release_version)
    if variant == "3.27":
        fallback = """                } else {
                    rmtoolNoteSettingsRoot._selectedIndex = page
                }
"""
        replacement = """                } else {
                    rmtoolNoteSettingsRoot._selectedIndex = page
                    rmtoolNoteSettingsRoot.highlightedIndex = page
                }
"""
        if text.count(fallback) != 1:
            raise RuntimeError("note-enhancements source lacks the 3.27 navigation fallback")
        text = text.replace(fallback, replacement, 1)
        boundary = "    TRAVERSE Item#root\n        LOCATE BEFORE Component#general"
        if text.count(boundary) != 1:
            raise RuntimeError("note-enhancements source lacks the Settings root boundary")
        text = text.replace(
            boundary,
            "    TRAVERSE DeviceKeyboardNavigationHandler#settings\n"
            "        LOCATE BEFORE Component#general",
            1,
        )
    temporary.mkdir(parents=True, exist_ok=True)
    destination = temporary / f"note-enhancements-{variant}.qmd"
    destination.write_text(text, encoding="utf-8", newline="\n")
    return destination


def _run(command: list[str], *, label: str) -> bytes:
    result = subprocess.run(command, capture_output=True, check=False)
    output = result.stdout + result.stderr
    if result.returncode:
        raise RuntimeError(f"{label} failed: {output.decode(errors='replace')}")
    return result.stdout


def _compile_and_validate(
    *, qmd_tool: Path, qmldiff: Path, source: Path, target: dict, work: Path
) -> bytes:
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
    shutil.copy2(
        hashtab,
        hashtabs / f"hashtab-{target['platform']}-{target['firmware']}",
    )
    qmd_path = qmds / "note-enhancements.qmd"
    qmd_path.write_bytes(compiled)
    checked = _run(
        [str(qmd_tool), "check", "-hashtabs", str(hashtabs), "-qmd", str(qmds)],
        label=f"qmd check {target['id']}",
    )
    if b"ALL OK" not in checked:
        raise RuntimeError(f"qmd check {target['id']} did not report ALL OK")
    compatible = _run(
        [str(qmldiff), "check-compatibility", str(hashtab), str(qmd_path)],
        label=f"qmldiff compatibility {target['id']}",
    )
    if b"No compatibility errors found" not in compatible:
        raise RuntimeError(f"qmldiff compatibility {target['id']} was incomplete")
    _run(
        [
            str(qmldiff),
            "apply-diffs",
            "--hashtab",
            str(hashtab),
            "-c",
            str(qrex),
            str(replay),
            str(qmd_path),
        ],
        label=f"qmldiff replay {target['id']}",
    )
    settings = (replay / SETTINGS_QML).read_text(encoding="utf-8")
    settings_menu = (replay / SETTINGS_MENU_QML).read_text(encoding="utf-8")
    document = (replay / DOCUMENT_QML).read_text(encoding="utf-8")
    scene = (replay / SCENE_QML).read_text(encoding="utf-8")
    for marker in (
        "rmtoolNoteEnhancementsSidebarItem",
        "rmtoolNoteEnhancementsPage",
        "RmtoolNoteEnhancements",
        "delayedColorRefreshEnabled",
        "delayChoices",
        "settlementPolicy",
        'label: "提笔后延迟刷新"',
        'label: "仅翻页刷新"',
        'label: "空闲等待时间"',
        'iconSource: "qrc:/ark/icons/notebook"',
    ):
        if marker not in settings:
            raise RuntimeError(f"structure assertion {target['id']} missing {marker}")
    for marker in (
        "rmtoolNoteDelayToggle",
        "rmtoolNoteDelaySelector",
        "rmtoolNoteDelayOptions",
        "rmtoolNoteIdleToggle",
        "rmtoolNotePageOnlyToggle",
        "rmtoolNoteEnhancementsAvailable",
        "rmtoolSetNoteDocumentDelayEnabled",
        "rmtoolSetNoteDocumentDelaySeconds",
        "rmtoolSetNoteDocumentSettlementPolicy",
        'label: "空闲等待时间"',
    ):
        if marker not in settings_menu:
            raise RuntimeError(f"notebook menu assertion {target['id']} missing {marker}")
    for marker in (
        "Document.Notebook",
        'return "documents/" + rmtoolNoteDocumentId + "/" + key',
        "rmtoolNoteReadScopedBool",
        "rmtoolNoteReadScopedDelay",
        "rmtoolNoteReadScopedPolicy",
        "rmtoolNoteDocumentSettlementPolicy",
        "sceneView?.rmtoolNoteSettingsChanged()",
        "sceneView?.rmtoolSettlePendingColor()",
        "rmtoolNoteDocumentView: root",
    ):
        if marker not in document:
            raise RuntimeError(f"document assertion {target['id']} missing {marker}")
    for marker in (
        "function rmtoolNoteRefreshInterval()",
        "function rmtoolNoteFeatureEnabled()",
        "function rmtoolNoteCanDeferStroke()",
        "function rmtoolNoteSettlementPolicy()",
        "function rmtoolQueueDirty(dirtyRect)",
        "function rmtoolSettlePending()",
        "function rmtoolSubmitPending()",
        "function rmtoolNoteSettingsChanged()",
        "rmtoolAwaitingStockFinalize",
        'const group = "RmtoolNoteEnhancements"',
        "root.document.fileType !== Document.Notebook",
        'return "documents/" + root.document.id.toString() + "/" + key',
        "if (!featureKey)",
        "const documentFeature = featureKey",
        "const documentSeconds = delayKey",
        "return 1000",
        "return seconds * 1000 - 1000",
        'rmtoolNoteSettlementPolicy() === "page"',
        "viewportUpdateTimer.rmtoolNoteCanDeferStroke()",
        "viewportUpdateTimer.rmtoolQueueDirty(dirtyRect)",
        "root.viewport?.markDirty(rmtoolPendingDirtyRect)",
        "inputSurface.clearFramebuffer()",
        "viewportUpdateTimer.rmtoolSettlePending()",
        "strokeHandler.timeSincePenUp() < 100",
    ):
        if marker not in scene:
            raise RuntimeError(f"timer assertion {target['id']} missing {marker}")
    if not re.search(r"property\s+bool\s+rmtoolAwaitingStockFinalize\s*:\s*false", scene):
        raise RuntimeError(
            f"timer assertion {target['id']} lacks the stock-finalize state property"
        )
    if scene.count("id: viewportUpdateTimer") != 1:
        raise RuntimeError(f"timer assertion {target['id']} changed timer identity")
    if scene.count("strokeHandler.timeSincePenUp() < 100") != 2:
        raise RuntimeError(
            f"pen-up assertion {target['id']} did not preserve both safety guards"
        )
    for forbidden in (
        "settleOnPenUp",
        "settleOnToolChange",
        "rmtoolNotePenUpToggle",
        "rmtoolNoteDocumentSettleOnPenUp",
        "rmtoolSettlePendingForToolChange",
        "rmtoolSchedulePenUpSettlement",
        "onCompletedStroke: {",
        "RMTOOL-NOTE",
        "└",
    ):
        if any(forbidden in qml for qml in (settings, settings_menu, document, scene)):
            raise RuntimeError(
                f"policy assertion {target['id']} retained obsolete {forbidden}"
            )
    timer = scene[scene.index("id: viewportUpdateTimer") :]
    if "interval: 1000" not in timer[:2000]:
        raise RuntimeError(f"timer assertion {target['id']} changed stock fallback")
    queue = "viewportUpdateTimer.rmtoolQueueDirty(dirtyRect)"
    stock_dirty = "root.viewport.markDirty(dirtyRect)"
    if queue not in timer or stock_dirty not in timer or timer.index(queue) > timer.index(stock_dirty):
        raise RuntimeError(f"timer assertion {target['id']} does not queue before stock fallback")
    page_policy = 'rmtoolNoteSettlementPolicy() === "page"'
    stop = "viewportUpdateTimer.stop()"
    restart = "viewportUpdateTimer.restart()"
    if page_policy not in timer or stop not in timer or restart not in timer:
        raise RuntimeError(f"timer assertion {target['id']} lacks both settlement policies")
    short_intervals = [
        int(value)
        for value in re.findall(
            r"viewportUpdateTimer\.interval\s*=\s*(\d+)", scene
        )
        if int(value) < 1000
    ]
    if short_intervals:
        raise RuntimeError(
            f"pen-up assertion {target['id']} has a sub-stock timer path: "
            f"{short_intervals}"
        )
    dirty_start = timer.index("function markDirtyAndRestart(dirtyRect)")
    dirty = timer[dirty_start : timer.index("onTriggered:", dirty_start)]
    dirty_order = (
        "viewportUpdateTimer.rmtoolNoteCanDeferStroke()",
        "viewportUpdateTimer.rmtoolQueueDirty(dirtyRect)",
        'viewportUpdateTimer.rmtoolNoteSettlementPolicy() === "page"',
        "viewportUpdateTimer.stop()",
        ".rmtoolNoteRefreshInterval()",
        "viewportUpdateTimer.restart()",
        "return",
        "viewportUpdateTimer.rmtoolSubmitPending()",
        "root.viewport.markDirty(dirtyRect)",
    )
    positions = [dirty.index(marker) for marker in dirty_order]
    if positions != sorted(positions):
        raise RuntimeError(
            f"policy assertion {target['id']} does not preserve revision-3 queue ordering"
        )
    if dirty.count("viewportUpdateTimer.rmtoolQueueDirty(dirtyRect)") != 1:
        raise RuntimeError(f"pen-up assertion {target['id']} duplicated private queueing")
    stock_dirty_position = dirty.index("root.viewport.markDirty(dirtyRect)")
    if dirty.find("viewportUpdateTimer.restart()", stock_dirty_position) < stock_dirty_position:
        raise RuntimeError(
            f"pen-up assertion {target['id']} does not retain the stock timer restart"
        )
    settings_start = scene.index(
        "function rmtoolNoteSettingsChanged()",
        scene.index("function rmtoolSettlePending()"),
    )
    timer_settings = scene[settings_start : scene.index("id: viewportUpdateTimer")]
    settings_order = (
        "!rmtoolNoteFeatureEnabled()",
        "viewportUpdateTimer.interval = 1000",
        "rmtoolSubmitPending()",
        "viewportUpdateTimer.restart()",
        "return",
        "if (!rmtoolHasPendingDirty)",
        'rmtoolNoteSettlementPolicy() === "page"',
    )
    positions = [timer_settings.index(marker) for marker in settings_order]
    if positions != sorted(positions):
        raise RuntimeError(
            f"pen-up assertion {target['id']} does not flush pending state before stock mode"
        )
    triggered = timer[timer.index("onTriggered:") :]
    awaiting = triggered.index("if (viewportUpdateTimer.rmtoolAwaitingStockFinalize")
    clear_awaiting = triggered.index(
        "viewportUpdateTimer.rmtoolAwaitingStockFinalize = false"
    )
    pending = triggered.index("else if (viewportUpdateTimer.rmtoolHasPendingDirty)")
    guard = triggered.index("if (strokeHandler.timeSincePenUp() < 100)")
    guard_restart = triggered.index("viewportUpdateTimer.restart()", guard)
    submit = triggered.index("viewportUpdateTimer.rmtoolSubmitPending()")
    stock_interval = triggered.index("viewportUpdateTimer.interval = 1000", submit)
    submit_restart = triggered.index("viewportUpdateTimer.restart()", stock_interval)
    positions = [
        awaiting,
        clear_awaiting,
        pending,
        guard,
        guard_restart,
        submit,
        stock_interval,
        submit_restart,
    ]
    if positions != sorted(positions):
        raise RuntimeError(
            f"pen-up assertion {target['id']} does not preserve two-stage ordering"
        )
    settings_page = settings[settings.index('label: "延迟彩色刷新"') :]
    settings_positions = tuple(
        settings_page.index(marker)
        for marker in (
            'label: "仅翻页刷新"',
            'label: "提笔后延迟刷新"',
            'label: "空闲等待时间"',
        )
    )
    menu_positions = tuple(
        settings_menu.index(marker)
        for marker in (
            'objectName: "rmtoolNotePageOnlyToggle"',
            'objectName: "rmtoolNoteIdleToggle"',
            'label: "空闲等待时间"',
        )
    )
    if settings_positions != tuple(sorted(settings_positions)) or menu_positions != tuple(
        sorted(menu_positions)
    ):
        raise RuntimeError(
            f"menu assertion {target['id']} does not preserve policy hierarchy"
        )
    if variant := _variant_for_release(target["release_version"]):
        if variant == "3.27":
            for marker in (
                "rmtoolNoteSettingsRoot._selectedIndex = page",
                "rmtoolNoteSettingsRoot.highlightedIndex = page",
            ):
                if marker not in settings:
                    raise RuntimeError(
                        f"navigation assertion {target['id']} missing {marker}"
                    )
        elif "rmtoolNoteSettingsRoot.highlightedIndex = page" in settings:
            raise RuntimeError(
                f"navigation assertion {target['id']} changed 3.28 navigation"
            )
    return compiled


def _load_matrix_config(path: Path) -> dict[tuple[str, str, str], dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    targets = document.get("targets") if isinstance(document, dict) else None
    if not isinstance(targets, list):
        raise RuntimeError("matrix config must contain a targets list")
    result = {}
    for raw in targets:
        if not isinstance(raw, dict):
            raise RuntimeError("matrix config target is invalid")
        item = dict(raw)
        key = (item.get("platform"), item.get("firmware"), item.get("xochitl_sha256"))
        if any(not isinstance(value, str) for value in key) or key in result:
            raise RuntimeError(f"matrix config target identity is invalid: {key}")
        item["id"] = f"{key[0]}-{key[1]}-{key[2][:12]}"
        result[key] = item
    return result


def _find_base_archive(
    package, cache_roots: tuple[Path, ...], download_root: Path
) -> Path:
    for root in cache_roots:
        if not root.is_dir():
            continue
        candidates = dict.fromkeys((root / package.asset, *root.glob(f"**/{package.asset}")))
        for candidate in candidates:
            if (
                candidate.is_file()
                and candidate.stat().st_size == package.size
                and sha256(candidate.read_bytes()) == package.sha256
            ):
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
            item.path: (
                extracted.joinpath(*PurePosixPath(item.path).parts).read_bytes(),
                item.mode,
            )
            for item in base.files
            if item.path != BASE_QMD_PATH
        }
    files[note.QMD_PAYLOAD_PATH] = (qmd, 0o644)
    if set(files) != note._PAYLOAD_PATHS:
        raise RuntimeError("note-enhancements payload path set drifted from carrier")
    archive = tap._gzip_member(
        tap._tar_member(files, apk_checksums=False, include_directories=False)
    )
    identity = (base.platform, base.firmware, base.architecture, base.xochitl_sha256)
    release, channel, offline, device = note.ALLOWED_TARGETS[identity]
    asset = note._expected_asset_name(base.platform, base.firmware, release)
    entry = {
        "firmware": base.firmware,
        "release_version": release,
        "channel": channel,
        "platform": base.platform,
        "architecture": base.architecture,
        "xochitl_sha256": base.xochitl_sha256,
        "offline_verified": offline,
        "device_verified": device,
        "package_revision": note.PACKAGE_REVISION,
        "asset": asset,
        "sha256": sha256(archive),
        "size": len(archive),
        "urls": [f"{origin}/{asset}" for origin in note.REMOTE_BASE_URLS],
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
    parser.add_argument(
        "--source",
        type=Path,
        default=REPO_ROOT / "note-enhancements/qmd-src/note-enhancements-3.28.qmd",
    )
    parser.add_argument("--matrix-config", type=Path, required=True)
    parser.add_argument(
        "--base-manifest",
        type=Path,
        default=REPO_ROOT / "tap-page-turn/manifest.json",
    )
    parser.add_argument("--cache-root", action="append", type=Path, default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--write-manifest", type=Path, required=True)
    args = parser.parse_args()

    tap_catalog = tap.parse_manifest(args.base_manifest.read_bytes())
    matrix = _load_matrix_config(args.matrix_config)
    expected_config = {(key[0], key[1], key[3]) for key in note.ALLOWED_TARGETS}
    if set(matrix) != expected_config:
        raise RuntimeError("matrix config does not cover the exact note targets")
    cache_roots = tuple(args.cache_root) or (
        REPO_ROOT / ".rmtool/cache/tap-page-turn",
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    download_root = args.output_dir / "tap-base-cache"
    entries = []
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        for key in sorted(note.ALLOWED_TARGETS, key=lambda item: (item[1], item[0], item[3])):
            platform, firmware, architecture, xochitl_sha = key
            release, channel, _offline, _device = note.ALLOWED_TARGETS[key]
            target = dict(matrix[(platform, firmware, xochitl_sha)])
            target["release_version"] = release
            matches = [
                item
                for item in tap_catalog
                if (item.platform, item.firmware, item.architecture, item.xochitl_sha256)
                == key
                and item.release_version == release
                and item.channel == channel
            ]
            if len(matches) != 1:
                raise RuntimeError(f"tap manifest has no unique carrier for {platform}/{release}")
            base = matches[0]
            source = _source_for_release(args.source, release, work / "source")
            qmd = _compile_and_validate(
                qmd_tool=args.qmd_tool,
                qmldiff=args.qmldiff,
                source=source,
                target=target,
                work=work / "validation",
            )
            archive, entry = _build_archive(
                base,
                _find_base_archive(base, cache_roots, download_root),
                qmd,
            )
            output = args.output_dir / entry["asset"]
            tap._write_atomic(output, archive)
            entries.append(entry)
            print(
                f"{entry['platform']} {entry['release_version']}: "
                f"{output} {entry['sha256']} {entry['size']}"
            )

    manifest_data = (
        json.dumps({"schema_version": 1, "packages": entries}, ensure_ascii=False, indent=2)
        + "\n"
    ).encode("utf-8")
    note.parse_manifest(manifest_data, require_local_match=False)
    tap._write_atomic(args.write_manifest, manifest_data)
    print(f"manifest: {args.write_manifest}")
    print(f"targets: {len(entries)}")
    print("verification: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
