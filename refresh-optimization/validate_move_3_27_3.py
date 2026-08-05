"""Offline validation for the Move 3.27.3 fast-monochrome experiment."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
from pathlib import Path


INTERNAL_VERSION = "20260612085811"
TARGETS = {
    Path("qml/device/view/main/MainView.qml"): (
        23_925,
        "8440305637f5109fd484383faee32b2ce17834b89c853c3a6b037448b051670d",
    ),
    Path("qml/device/view/documentview/DocumentView.qml"): (
        103_778,
        "0ad4be8e386fb5bfa1f8cf6ff2813653c4458c59a2dad383bb10d15249863d2e",
    ),
    Path("qt/qml/xofm/libs/toolbar/qml/SettingsMenu.qml"): (
        8_040,
        "331c08f119a42ac7509344550deb13cf1cbeef67ae40eff74ac4b460f1ed08b1",
    ),
}
EXPECTED_HASHTAB_SHA256 = "82dd913163bbc0ccbe080d826a524e6d9539c54095f1d8027b3ae21b82dda3ba"
EXPECTED_HASHTAB_SIZE = 607_024
QMD_SOURCE = (
    Path(__file__).parent.parent
    / "fast-mono-reading"
    / "qmd-src"
    / "fast-mono-reading-3.27.qmd"
)


def run(*args: str | Path) -> subprocess.CompletedProcess[bytes]:
    command = [str(arg) for arg in args]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode:
        output = (result.stdout + result.stderr).decode(errors="replace")
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(command)}\n{output}")
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qml-root", type=Path, required=True)
    parser.add_argument("--hashtab", type=Path, required=True)
    parser.add_argument("--qmldiff", type=Path, required=True)
    parser.add_argument("--qmd-tool", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, default=Path("build/refresh-optimization"))
    args = parser.parse_args()

    source_qmls = {relative: args.qml_root / relative for relative in TARGETS}
    required_files = (*source_qmls.values(), args.hashtab, args.qmldiff, args.qmd_tool, QMD_SOURCE)
    missing = [str(path) for path in required_files if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing required file(s): " + ", ".join(missing))
    for relative, source_qml in source_qmls.items():
        expected_size, expected_hash = TARGETS[relative]
        if source_qml.stat().st_size != expected_size or sha256(source_qml) != expected_hash:
            raise RuntimeError(f"Recovered {relative} does not match Move 3.27.3.0")
    if args.hashtab.stat().st_size != EXPECTED_HASHTAB_SIZE or sha256(args.hashtab) != EXPECTED_HASHTAB_SHA256:
        raise RuntimeError("Hashtable does not match Move 3.27.3.0")

    work_dir = args.work_dir.resolve()
    shutil.rmtree(work_dir, ignore_errors=True)
    qmd_dir = work_dir / "qmd"
    hashtab_dir = work_dir / "hashtabs"
    patched_dir = work_dir / "patched"
    qmd_dir.mkdir(parents=True)
    hashtab_dir.mkdir()
    compiled_qmd = qmd_dir / QMD_SOURCE.name
    hashtab = hashtab_dir / f"hashtab-chiappa-{INTERNAL_VERSION}"

    shutil.copy2(args.hashtab, hashtab)
    compiled_qmd.write_bytes(
        run(args.qmd_tool, "hash", "-hashtab", hashtab, QMD_SOURCE).stdout
    )
    run(args.qmd_tool, "check", "-hashtabs", hashtab_dir, "-qmd", qmd_dir)
    compatibility = run(args.qmldiff, "check-compatibility", hashtab, compiled_qmd)
    compatibility_text = (compatibility.stdout + compatibility.stderr).decode(errors="replace")
    if "No compatibility errors found" not in compatibility_text:
        raise RuntimeError("qmldiff did not confirm compatibility:\n" + compatibility_text)
    run(
        args.qmldiff,
        "apply-diffs",
        "--hashtab",
        hashtab,
        "-c",
        args.qml_root,
        patched_dir,
        compiled_qmd,
    )

    patched_texts = {
        relative: (patched_dir / relative).read_text(encoding="utf-8")
        for relative in TARGETS
    }
    main_assertions = (
        "id: globalScreenMode",
        "if (!documentView.item)",
        "if (documentView.visible",
        "documentView.item?.rmtoolFastMonoReadingAvailable",
        "documentView.item.rmtoolFastMonoReadingEnabled",
        "const mode = documentView.item.globalScreenMode",
        "return mode !== undefined",
        "return Epaper.ScreenModeItem.Mono",
        "return documentView.item.globalScreenMode",
        "return Epaper.ScreenModeItem.UI",
        'objectName: "global"',
    )
    document_assertions = (
        "property  bool rmtoolFastMonoReadingEnabled: false",
        "property  int rmtoolFastMonoCleanupInterval: 10",
        "property  int rmtoolFastMonoCleanupCount: 0",
        "property  int rmtoolFastMonoLastPage: -1",
        "readonly property  bool rmtoolFastMonoReadingAvailable: !!document&&(document.fileType === Document.Pdf",
        "|| document.fileType === Document.Ebook)&&EPFramebuffer.hasCapability",
        "EPFramebuffer.hasCapability(EPFramebuffer.Capability.Color)",
        "function onCurrentPageChanged()",
        "function onDocumentChanged()",
        "function onRmtoolFastMonoReadingEnabledChanged()",
        "function onRmtoolFastMonoCleanupIntervalChanged()",
        "interval: 500",
        'root.ghostBuster.forceClearNow("rmtool fast mono periodic cleanup")',
        "property  var rmtoolDocumentView: root",
    )
    settings_assertions = (
        "ArkControls.FoldoutToggle",
        "id: rmtoolFastMonoReadingToggle",
        'objectName: "rmtoolFastMonoReadingToggle"',
        'label: "\\u5feb\\u901f\\u9ed1\\u767d"',
        "id: rmtoolFastMonoCleanupOptions",
        "ArkControls.FoldoutItem",
        'objectName: "rmtoolFastMonoCleanupSelector"',
        'label: "\\u5f3a\\u5236\\u5237\\u65b0"',
        "description: {",
        'label: "\\u6bcf 5 \\u6b21\\u7ffb\\u9875"',
        'label: "\\u6bcf 10 \\u6b21\\u7ffb\\u9875"',
        'label: "\\u6bcf 20 \\u6b21\\u7ffb\\u9875"',
        'label: "\\u6bcf 30 \\u6b21\\u7ffb\\u9875"',
        'label: "\\u4ece\\u4e0d"',
        "onClicked: stackView.push(rmtoolFastMonoCleanupOptions)",
        "stackView.popCurrentItem()",
        "rmtoolFastMonoReadingAvailable",
        "rmtoolFastMonoReadingEnabled = !view.rmtoolFastMonoReadingEnabled",
        "view.showNotification",
        '"\\u5feb\\u901f\\u9ed1\\u767d\\u5df2\\u5f00\\u542f"',
        '"\\u5feb\\u901f\\u9ed1\\u767d\\u5df2\\u5173\\u95ed"',
    )
    assertions = {
        Path("qml/device/view/main/MainView.qml"): main_assertions,
        Path("qml/device/view/documentview/DocumentView.qml"): document_assertions,
        Path("qt/qml/xofm/libs/toolbar/qml/SettingsMenu.qml"): settings_assertions,
    }
    missing_assertions = [
        f"{relative}:{value}"
        for relative, values in assertions.items()
        for value in values
        if value not in patched_texts[relative]
    ]
    if missing_assertions:
        raise RuntimeError("Patched QML assertions failed: " + ", ".join(missing_assertions))
    settings_text = patched_texts[Path("qt/qml/xofm/libs/toolbar/qml/SettingsMenu.qml")]
    forbidden = [
        value
        for value in ("ArkControls.Dropdown", "ArkControls.TextInput")
        if value in settings_text
    ]
    if forbidden:
        raise RuntimeError("Patched QML contains forbidden controls: " + ", ".join(forbidden))
    if settings_text.count("ArkControls.FoldoutItem {") < 6:
        raise RuntimeError("Patched QML does not contain six native FoldoutItem rows")
    unique_assertions = {
        Path("qml/device/view/main/MainView.qml"): ('id: globalScreenMode',),
        Path("qml/device/view/documentview/DocumentView.qml"): (
            "property  bool rmtoolFastMonoReadingEnabled: false",
            "readonly property  bool rmtoolFastMonoReadingAvailable",
            "property  var rmtoolDocumentView: root",
        ),
        Path("qt/qml/xofm/libs/toolbar/qml/SettingsMenu.qml"): (
            "id: rmtoolFastMonoReadingToggle",
        ),
    }
    duplicates = [
        f"{relative}:{value}"
        for relative, values in unique_assertions.items()
        for value in values
        if patched_texts[relative].count(value) != 1
    ]
    if duplicates:
        raise RuntimeError("Patched QML uniqueness checks failed: " + ", ".join(duplicates))
    print(f"firmware_internal={INTERNAL_VERSION}")
    for relative, source_qml in source_qmls.items():
        print(f"source_{relative.name}_sha256={sha256(source_qml)}")
    print(f"hashtab_sha256={sha256(hashtab)}")
    print(f"compiled_qmd_sha256={sha256(compiled_qmd)}")
    for relative in TARGETS:
        patched_qml = patched_dir / relative
        print(f"patched_{relative.name}_sha256={sha256(patched_qml)}")
    print("validation=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
