"""Fail the application build when a supported device/version matrix drifts."""

from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FEATURE_MODULES = {
    "tap-page-turn": "_tap_page_turn",
    "fast-mono-reading": "_fast_mono_reading",
    "native-chinese": "_native_chinese",
    "pinyin-input": "_pinyin_input",
    "reading-enhancements": "_reading_enhancements",
    "note-enhancements": "_note_enhancements",
    "weread-launcher": "_weread_launcher",
}
FIRMWARE_RE = re.compile(r"^[0-9]{14}$")

# These are the official 3.28.0.172 identities extracted from the five cached
# official SWUs. Keep this table close to the build gate so a new firmware
# cannot be added to one plugin while its stock carrier is still unverified.
OFFICIAL_172 = {
    "ferrari": {
        "architecture": "aarch64",
        "xochitl_sha256": "b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021",
        "stock_french_sha256": "2b03e8bdf26566d06189604f4678b1929af60b8bef65b662fafc9f04eebed9cc",
        "localized_qm": ("reMarkable_zh_CN-3.28.0.166-ferrari.qm", 196626, "49cf09fc23ef3fcacb956d426915e3f80b85a02fa7e597a8b5fc8013a2bdb931"),
    },
    "chiappa": {
        "architecture": "aarch64",
        "xochitl_sha256": "5ba79d1b5656df1a771217d29a8d3938c40256be53361b10a0d17cd4752807f4",
        "stock_french_sha256": "e0ec3db5e71798db0e9543e826b9770ae13c837e438cdffe7268ad45c58da1a0",
        "localized_qm": ("reMarkable_zh_CN-3.28.0.166-chiappa.qm", 192400, "2e501a66c30addbecada68b6af262ea506440547b478b4e02e7d2a56889446a1"),
    },
    "tatsu": {
        "architecture": "aarch64",
        "xochitl_sha256": "fa674d2ca3d8002602ce4b1b92280b96bcdf2cf32b6a42ec62ae178a1fad1fe3",
        "stock_french_sha256": "aeb154bedc9235df280354790522a15508858af55ddeab17c85ce1876d35a6a2",
        "localized_qm": ("reMarkable_zh_CN-3.28.0.166-chiappa.qm", 192400, "2e501a66c30addbecada68b6af262ea506440547b478b4e02e7d2a56889446a1"),
    },
    "rm1": {
        "architecture": "armv7l",
        "xochitl_sha256": "1f4fbb6e14650704b5b036e482da9948e73553178f6116ad464ab072f7b90117",
        "stock_french_sha256": "bcd3310eee9ecb647287957b4e07ff28fbe6fa1aa09e3efb40312bcc2d2942e3",
        "localized_qm": ("reMarkable_zh_CN-3.28.0.172-rm1-rm2.qm", 205621, "0f1de519ab4ac1998f432dab014d40fb0cdae2fe528ab30ca47c7a507df82485"),
    },
    "rm2": {
        "architecture": "armv7l",
        "xochitl_sha256": "071d85beef3ef2d4cc0e11002140b27b82a2cc04a2ed740a5669f591069b77df",
        "stock_french_sha256": "6b946dcbe013d66a4ac270a34f79f57c327c8bfbcc73ffe5c44a5a3efd35d6fb",
        "localized_qm": ("reMarkable_zh_CN-3.28.0.172-rm1-rm2.qm", 205621, "0f1de519ab4ac1998f432dab014d40fb0cdae2fe528ab30ca47c7a507df82485"),
    },
}


def _manifest(feature: str) -> dict:
    path = ROOT / feature / "manifest.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path.relative_to(ROOT)}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("packages"), list):
        raise RuntimeError(f"Invalid package manifest: {path.relative_to(ROOT)}")
    return document


def _package_key(package: dict) -> tuple[str, str, str, str, str]:
    return (
        package.get("firmware", ""),
        package.get("platform", ""),
        package.get("architecture", ""),
        package.get("xochitl_sha256", ""),
        package.get("release_version", ""),
    )


def _allowed_key(key, release: str) -> tuple[str, str, str, str, str]:
    if len(key) != 4 or not all(isinstance(item, str) for item in key):
        raise RuntimeError(f"Invalid target identity: {key!r}")
    if FIRMWARE_RE.fullmatch(key[0]):
        firmware, platform, architecture, xochitl_sha256 = key
    elif FIRMWARE_RE.fullmatch(key[1]):
        platform, firmware, architecture, xochitl_sha256 = key
    else:
        raise RuntimeError(f"Target identity has no firmware field: {key!r}")
    return firmware, platform, architecture, xochitl_sha256, release


def _validate_feature_matrix(feature: str) -> None:
    module = importlib.import_module(FEATURE_MODULES[feature])
    document = _manifest(feature)
    # The parser performs the detailed schema, hash, URL and payload checks.
    module.parse_manifest(json.dumps(document).encode("utf-8"))
    allowed = getattr(module, "ALLOWED_TARGETS", None)
    if not isinstance(allowed, dict):
        return
    expected = {
        _allowed_key(identity, policy[0])
        for identity, policy in allowed.items()
    }
    actual = {_package_key(package) for package in document["packages"]}
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise RuntimeError(
            f"{feature} matrix mismatch; missing={missing!r}, extra={extra!r}"
        )


def _translation_candidates() -> dict[tuple[str, str], list]:
    import _rmkit_cn

    manifest = _rmkit_cn.parse_translation_manifest(
        _rmkit_cn.BUNDLED_TRANSLATION_MANIFEST_PATH.read_bytes()
    )
    result = {}
    for firmware, root in manifest.items():
        for package in (root, *root.variants):
            result.setdefault((firmware, package.platform), []).append(package)
    return result


def _validate_172_localization() -> None:
    import _native_chinese as native

    candidates = _translation_candidates()
    for platform, expected in OFFICIAL_172.items():
        key = ("20260827113527", platform)
        matches = [
            package
            for package in candidates.get(key, ())
            if package.release_version == "3.28.0.172"
            and package.xochitl_sha256 == expected["xochitl_sha256"]
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"localization matrix missing exact 3.28.0.172 {platform} carrier"
            )
        package = matches[0]
        if package.stock_french_sha256 != expected["stock_french_sha256"]:
            raise RuntimeError(f"3.28.0.172 {platform} stock French hash mismatch")
        if (package.asset, package.size, package.localized_qm_sha256) != expected["localized_qm"]:
            raise RuntimeError(f"3.28.0.172 {platform} Chinese catalog mismatch")

        identity = next(
            raw_identity
            for raw_identity, policy in native.ALLOWED_TARGETS.items()
            if _allowed_key(raw_identity, policy[0])
            == (
                "20260827113527",
                platform,
                expected["architecture"],
                expected["xochitl_sha256"],
                "3.28.0.172",
            )
        )
        selected = native._bundled_french_slot_package(
            native.tap.DeviceIdentity(*identity)
        )
        if selected.stock_french_sha256 != expected["stock_french_sha256"]:
            raise RuntimeError(f"French-slot guard selected the wrong {platform} catalog")


def validate() -> None:
    for feature in FEATURE_MODULES:
        _validate_feature_matrix(feature)
    _validate_172_localization()


def main() -> int:
    validate()
    print(
        "PASS: device/version matrix covers all package manifests and "
        "the five official 3.28.0.172 localization carriers."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
