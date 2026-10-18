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
    "qr-upload": "_qr_upload",
    "native-import": "_native_import_control",
}
FIRMWARE_RE = re.compile(r"^[0-9]{14}$")
COLOR_PLATFORMS = frozenset(("ferrari", "chiappa"))
MONO_PLATFORMS = frozenset(("rm1", "rm2", "tatsu"))
COLOR_RELEASES = frozenset((
    "3.27.1.0", "3.27.3.0", "3.28.0.162", "3.28.0.163",
    "3.28.0.164", "3.28.0.166", "3.28.0.169", "3.28.0.172",
))

# Reviewed coverage, independent of each backend's allowlist: deleting a target
# from both code and its manifest must not silently narrow a published feature.
COLOR_TARGETS = {(platform, release) for platform in COLOR_PLATFORMS for release in COLOR_RELEASES}
MONO_TARGETS = {(platform, release) for platform in MONO_PLATFORMS for release in ("3.27.3.0", "3.28.0.172")}
BETA_329_TARGETS = {(platform, "3.29.0.152") for platform in COLOR_PLATFORMS | MONO_PLATFORMS} | {("chiappa", "3.29.0.153")}
BETA_329_COLOR_TARGETS = {(platform, "3.29.0.152") for platform in COLOR_PLATFORMS} | {("chiappa", "3.29.0.153")}
EXPECTED_COVERAGE = {
    "tap-page-turn": COLOR_TARGETS | MONO_TARGETS | BETA_329_TARGETS,
    "fast-mono-reading": COLOR_TARGETS,
    "native-chinese": COLOR_TARGETS | {(platform, "3.28.0.172") for platform in MONO_PLATFORMS} | BETA_329_TARGETS,
    "pinyin-input": COLOR_TARGETS | {(platform, "3.28.0.172") for platform in MONO_PLATFORMS} | BETA_329_TARGETS,
    "reading-enhancements": COLOR_TARGETS | BETA_329_COLOR_TARGETS,
    "note-enhancements": COLOR_TARGETS | BETA_329_COLOR_TARGETS,
    "weread-launcher": {(platform, "3.28.0.172") for platform in COLOR_PLATFORMS},
    "qr-upload": {("chiappa", "3.28.0.172"), ("chiappa", "3.29.0.153")},
    "native-import": {(platform, release) for platform in COLOR_PLATFORMS | MONO_PLATFORMS
                      for release in ("3.28.0.172", "3.29.0.153")},
}

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


OFFICIAL_329 = {
    "ferrari": {
        "architecture": "aarch64",
        "xochitl_sha256": "7aa5d67da6850d7021ade5a2232edeb5f34550236eca6f7a102f37f962dd4bd2",
        "stock_french_sha256": "8ac6be8693319e5f2f9c29f91dec87e30854d3e6ffb4a5eb5ffb71d01a6df47c",
        "localized_qm": ("reMarkable_zh_CN-3.29.0.152-ferrari.qm", 205597, "d51a54b693e233cfe654185fa6f65abcaf6616e10c4216dc6fecb6a550687c68"),
    },
    "chiappa": {
        "architecture": "aarch64",
        "xochitl_sha256": "09ba8581a50d96c19b360606ee0288b8863faec620036c8eda3b2a0b5cc2d2e2",
        "stock_french_sha256": "a693ba91b9e3123840b09d6dcc1bf1a29e55672258abcf664ab06eba6a53d35b",
        "localized_qm": ("reMarkable_zh_CN-3.29.0.152-chiappa.qm", 201371, "71423229aa311950bb640b71ff903ce78206c068f2dc36ffae042c22d2b4c3b6"),
    },
    "tatsu": {
        "architecture": "aarch64",
        "xochitl_sha256": "1bf16b6e7d5355e48479fa832871e98834307da652a06245b5fb86d0bbd7a26c",
        "stock_french_sha256": "d4cac61e20cadeb88bcf2cc3d88b5fb7f709cb1e547529034f1a3135ac2597db",
        "localized_qm": ("reMarkable_zh_CN-3.29.0.152-tatsu.qm", 201371, "71423229aa311950bb640b71ff903ce78206c068f2dc36ffae042c22d2b4c3b6"),
    },
    "rm1": {
        "architecture": "armv7l",
        "xochitl_sha256": "2eb775c3ced943c20ccf6271bb107e745db10aece2dc21e0e4bd8450d98fd23c",
        "stock_french_sha256": "9cc0ef23b734e7d5d5be0cd18efaf366824ec86c32db8e46024bf63fae468fd7",
        "localized_qm": ("reMarkable_zh_CN-3.29.0.152-rm1.qm", 214592, "f545de827901f7fc0fa91c31a33392429aea3ec79d4c644ae546856fa6e5d6e6"),
    },
    "rm2": {
        "architecture": "armv7l",
        "xochitl_sha256": "50a5ed69ab9c60d9f3b1d04b19311562194f6cfee769b17a6ea069d255e47760",
        "stock_french_sha256": "25661b271e867a60327c97ff6518097b50b14c9e8a64b179d6d401f4799c6f1c",
        "localized_qm": ("reMarkable_zh_CN-3.29.0.152-rm2.qm", 214592, "f545de827901f7fc0fa91c31a33392429aea3ec79d4c644ae546856fa6e5d6e6"),
    },
}


OFFICIAL_329_153 = {
    "chiappa": {
        "architecture": "aarch64",
        "xochitl_sha256": "0d4bfba6daf8f61138996af95b494d70da11654d4861a98bffe894a425b8caba",
        "stock_french_sha256": "5f0fbd874230e2e7256f682656b6d3b7e854c2513e44258a4143b365d9c42997",
        "localized_qm": ("reMarkable_zh_CN-3.29.0.153-chiappa.qm", 210177, "f90054fc45147901e26f3260feb1e729985f435396586f3ab05c02736aacbf60"),
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
    packages = module.parse_manifest(json.dumps(document).encode("utf-8"))
    if feature == "native-chinese":
        for package in packages:
            if package.architecture == "armv7l":
                extension = package.file(module.EXTENSION_PATH)
                if (extension.size, extension.sha256) != module.ARM_TRANSLATOR:
                    raise RuntimeError(f"Native Chinese ARM Qt ABI mismatch: {package.asset}")
    pairs = [(package["platform"], package["release_version"]) for package in document["packages"]]
    expected_pairs = EXPECTED_COVERAGE[feature]
    if len(pairs) != len(set(pairs)) or set(pairs) != expected_pairs:
        raise RuntimeError(
            f"{feature} device/release coverage mismatch; "
            f"missing={sorted(expected_pairs - set(pairs))!r}, "
            f"extra={sorted(set(pairs) - expected_pairs)!r}, duplicate={len(pairs) != len(set(pairs))}"
        )
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


def _validate_cross_feature_support() -> None:
    import _appload as appload
    import _screen_preview as preview
    import _tap_page_turn as tap
    import _weread_app as weread

    catalogs = {
        feature: importlib.import_module(module).parse_manifest(
            json.dumps(_manifest(feature)).encode("utf-8")
        )
        for feature, module in FEATURE_MODULES.items()
    }
    baselines = {
        (package.platform, package.release_version): (tap._shared_specs(package)[0], package)
        for package in catalogs["tap-page-turn"]
    }
    native_import = importlib.import_module(FEATURE_MODULES["native-import"])
    for package in catalogs["native-import"]:
        baselines.setdefault((package.platform, package.release_version),
                             (native_import._shared_specs(package)[0], package))
    for feature, catalog in catalogs.items():
        for package in catalog:
            baseline_runtime, baseline = baselines[(package.platform, package.release_version)]
            fields = ("firmware", "architecture", "xochitl_sha256", "channel")
            if any(getattr(package, field) != getattr(baseline, field) for field in fields):
                raise RuntimeError(
                    f"{feature} identity disagrees with runtime: "
                    f"{package.platform} {package.release_version}"
                )
            runtime, _feature = importlib.import_module(FEATURE_MODULES[feature])._shared_specs(package)
            if runtime != baseline_runtime:
                raise RuntimeError(f"{feature} shared runtime mismatch: {package.package_id}")

    app = weread.trusted_package()
    for _runtime, package in baselines.values():
        identity = tap.DeviceIdentity(
            package.firmware, package.platform, package.architecture, package.xochitl_sha256
        )
        # Includes path conflicts and trusted historical peers. Historical trust
        # is NOT installation support (notably AppLoad on 3.28).
        tap._trusted_shared_context(identity)
        expected_appload = package.release_version in ("3.27.1.0", "3.27.3.0")
        if ((appload.app_asset(identity) is not None) != expected_appload
                or (appload.koreader_asset(identity) is not None) != expected_appload):
            raise RuntimeError(f"AppLoad/KOReader support drift: {package.package_id}")
        device = weread.WeReadAppDevice(package.platform, package.architecture, package.release_version)
        expected_weread = package.platform in COLOR_PLATFORMS and package.release_version.startswith("3.28.")
        if weread._supported(device, app) != expected_weread:
            raise RuntimeError(f"WeRead app support drift: {package.package_id}")
    if set(preview.PROFILES) != COLOR_PLATFORMS | MONO_PLATFORMS:
        raise RuntimeError("Screen preview device coverage mismatch")


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


def _validate_localization(firmware: str, release: str, targets: dict) -> None:
    import _native_chinese as native

    candidates = _translation_candidates()
    for platform, expected in targets.items():
        key = (firmware, platform)
        matches = [
            package
            for package in candidates.get(key, ())
            if package.release_version == release
            and package.xochitl_sha256 == expected["xochitl_sha256"]
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"localization matrix missing exact {release} {platform} carrier"
            )
        package = matches[0]
        if package.stock_french_sha256 != expected["stock_french_sha256"]:
            raise RuntimeError(f"{release} {platform} stock French hash mismatch")
        if (package.asset, package.size, package.localized_qm_sha256) != expected["localized_qm"]:
            raise RuntimeError(f"{release} {platform} Chinese catalog mismatch")

        identity = next(
            raw_identity
            for raw_identity, policy in native.ALLOWED_TARGETS.items()
            if _allowed_key(raw_identity, policy[0])
            == (
                firmware,
                platform,
                expected["architecture"],
                expected["xochitl_sha256"],
                release,
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
    _validate_cross_feature_support()
    _validate_localization("20260827113527", "3.28.0.172", OFFICIAL_172)
    _validate_localization("20261009073202", "3.29.0.152", OFFICIAL_329)
    _validate_localization("20261009073202", "3.29.0.153", OFFICIAL_329_153)


def main() -> int:
    validate()
    print(
        f"PASS: 32 device/firmware targets, {len(FEATURE_MODULES)} plugin matrices, shared runtimes, "
        "AppLoad/KOReader and WeRead boundaries, five preview profiles, and "
        "eleven official 3.28.0.172 / 3.29.0.152 / 3.29.0.153 localization carriers."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
