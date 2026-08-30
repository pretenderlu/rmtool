"""Exact-firmware backend for the consolidated reading-enhancements feature."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
from dataclasses import dataclass, replace
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

import _package_download
import _tap_page_turn as tap
import _xovi_standalone as shared


REPO_URL = "https://github.com/pretenderlu/rmtool"
ASSET_RELEASE_URL = f"{REPO_URL}/releases/download/reading-enhancements-assets"
COS_URL = (
    "https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/"
    "reading-enhancements"
)
REMOTE_BASE_URLS = (ASSET_RELEASE_URL, COS_URL)
MANIFEST_URLS = tuple(f"{base}/manifest.json" for base in REMOTE_BASE_URLS)
BUNDLED_MANIFEST = Path(__file__).with_name("reading-enhancements") / "manifest.json"

QMD_PAYLOAD_PATH = "exthome/qt-resource-rebuilder/reading-enhancements.qmd"
FEATURE_ID = "reading-enhancements"
PACKAGE_REVISION = 8
MAX_MANIFEST_BYTES = tap.MAX_MANIFEST_BYTES
MAX_PACKAGE_BYTES = tap.MAX_PACKAGE_BYTES
MAX_UNPACKED_BYTES = tap.MAX_UNPACKED_BYTES
_ASSET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.tar\.gz")
_REQUIRED_PATHS = {
    *shared._COMMON_ARCHIVE_PATHS,
    QMD_PAYLOAD_PATH,
}
_PAYLOAD_PATHS = _REQUIRED_PATHS | {
    "LICENSE.qmd-tool",
    "LICENSE.rm-xovi-extensions",
    "LICENSE.xovi",
}

# Only package revisions shipped by a tagged rmtool release are accepted as
# predecessors. Revisions 2 and 5, device canaries, and defect test builds were
# never public and deliberately remain untrusted.
_PUBLISHED_REVISION_QMDS = {
    1: {
        "3.27": (
            "7c3a384e1cd4f2be7b94aadce82b30c31ca81a49ed482a300e63bf83fce67fe7",
            28715,
        ),
        "3.28": (
            "e6d6ef9260c4bc6cfffc375d4485e3ec33eea36fd6725790c7e2483da16c74ec",
            27761,
        ),
    },
    3: {
        "3.27": (
            "622c17f90cb6f08552ac3ce412a37fc56c8f24fc4a52bb1fa0cdfb5057fb6532",
            47548,
        ),
        "3.28": (
            "10ef980eb3bc66cf94087ab096e660a5d3519fad1b383513ca7ed0db09f48a7a",
            46594,
        ),
    },
    4: {
        "3.27": (
            "d8b2a21d75eb4f1c26e67446a6519360aa2d690c7ae91f83c744c83152ba9e28",
            48148,
        ),
        "3.28": (
            "aadec3d2ec54c408a8f64c8f046bd5973ead1ba6e7e4a3c91cb38404d174b164",
            47194,
        ),
    },
    6: {
        "3.27": (
            "e526a2e8a7a3ac6199abc6cef591b6f77df0c52f2e6d73774b1a313e5b2b6ef4",
            48147,
        ),
        "3.28": (
            "1cecbf4e386f46d57ecf3ac9af1a7fd2ac208b7461cc730113dc744ef25d6f7f",
            54042,
        ),
    },
    7: {
        "3.27": (
            "59501fe8bacbf8ca0f9716262b43fecd154c33c7dc1982f1b56e9761562d3803",
            51289,
        ),
        "3.28.0.162": (
            "36b809ab3b29f64d76a976c3b6321324b36ccb036c453e7eecf4cf0c18efd566",
            57184,
        ),
        "3.28": (
            "65d36fa86f1db0378e2c729553089d71b7655cbda39608a612f366e170de3611",
            57224,
        ),
    },
}
_PUBLISHED_PREDECESSOR_REASONS = frozenset(
    f"package-revision-{revision}" for revision in _PUBLISHED_REVISION_QMDS
)

ALLOWED_TARGETS = {
    (
        "ferrari",
        "20260506100933",
        "aarch64",
        "29b9896b07f59636d910d8a740f6562c502f676a1a70f8814459229d25cc5288",
    ): ("3.27.1.0", "stable", True, False),
    (
        "chiappa",
        "20260506100933",
        "aarch64",
        "4646e0aef1cef2b3417889073ad5faba9259ae6b41f68326e75ef9a5c520c322",
    ): ("3.27.1.0", "stable", True, False),
    (
        "ferrari",
        "20260612085811",
        "aarch64",
        "9749880daa2f10844e77b560ec0ecddd1634d43eb328af637c7026edf3ef120e",
    ): ("3.27.3.0", "stable", True, False),
    (
        "chiappa",
        "20260612085811",
        "aarch64",
        "227a9bfe928ef5d164359e490d97648ffca40a5de13f07a9eb57a618a403f084",
    ): ("3.27.3.0", "stable", True, True),
    (
        "ferrari",
        "20260629074044",
        "aarch64",
        "10082aeb857c69c3f404ab189d7403318ba97d0c169e756ae9a5b3532b248a4a",
    ): ("3.28.0.162", "beta", True, False),
    (
        "chiappa",
        "20260629074044",
        "aarch64",
        "9e3e0372a15da25b148ac17667feb566014440e079c3e3ee504112d556ad2e10",
    ): ("3.28.0.162", "beta", True, False),
    (
        "ferrari",
        "20260702125656",
        "aarch64",
        "49f60572e830f6c4f20d800a56d644cdf53cd65a8e240b2b27106cce55040f89",
    ): ("3.28.0.163", "beta", True, False),
    (
        "chiappa",
        "20260702125656",
        "aarch64",
        "08171df6296b99d04b3694b337bd0ce911e6a93356955961a37de9dd93a0394d",
    ): ("3.28.0.163", "beta", True, False),
    (
        "ferrari",
        "20260702125656",
        "aarch64",
        "113bf7ea62ad171ea03c77c1f90e0666bcff163242a22ebca84372533b270c1c",
    ): ("3.28.0.164", "beta", True, False),
    (
        "chiappa",
        "20260702125656",
        "aarch64",
        "3a9e18483b73f43016fb25b451e3ece0efba7aa1cc92e080771e138ce6bbca98",
    ): ("3.28.0.164", "beta", True, False),
    (
        "ferrari",
        "20260806095513",
        "aarch64",
        "8726b4fce55a9154a5014956e5204401ce881d752c1ff3813adb622a68aac2f9",
    ): ("3.28.0.166", "beta", True, False),
    (
        "chiappa",
        "20260806095513",
        "aarch64",
        "5748eed3bb804c8d3000e833ba472750428b6a82bc09b2bc7b5cf01847336bc7",
    ): ("3.28.0.166", "beta", True, False),
    (
        "ferrari",
        "20260806095513",
        "aarch64",
        "43a9d5d0acc5b998264c16586e11b848f3b83d2d63b5fd322b09c0977d94d3d4",
    ): ("3.28.0.169", "beta", True, False),
    (
        "chiappa",
        "20260806095513",
        "aarch64",
        "6361610111c381ce730a8bfcc889bd933ef5fef173563a9156e435233714e7ee",
    ): ("3.28.0.169", "beta", True, False),
}

PayloadFile = tap.PayloadFile
DeviceIdentity = tap.DeviceIdentity


class ReadingEnhancementsState(Enum):
    INCOMPATIBLE = "incompatible"
    NOT_INSTALLED = "not_installed"
    MIGRATION_AVAILABLE = "migration_available"
    REPAIR_AVAILABLE = "repair_available"
    INSTALLED_DISABLED = "installed_disabled"
    ENABLE_PENDING_REBOOT = "enable_pending_reboot"
    ENABLED = "enabled"
    DISABLE_PENDING_REBOOT = "disable_pending_reboot"
    BROKEN = "broken"


@dataclass(frozen=True)
class ReadingEnhancementsPackage:
    firmware: str
    release_version: str
    channel: str
    platform: str
    architecture: str
    xochitl_sha256: str
    asset: str
    sha256: str
    size: int
    files: tuple[PayloadFile, ...]
    urls: tuple[str, str]
    package_revision: int
    offline_verified: bool
    device_verified: bool

    @property
    def package_id(self) -> str:
        return f"{self.platform}-{self.firmware}-{self.xochitl_sha256[:12]}"

    @property
    def download_urls(self) -> tuple[str, ...]:
        # GitHub first, Tencent COS fallback; the manifest field is a set.
        return (f"{ASSET_RELEASE_URL}/{self.asset}", f"{COS_URL}/{self.asset}")

    @property
    def download_url(self) -> str:
        return self.download_urls[0]

    def file(self, path: str) -> PayloadFile:
        for item in self.files:
            if item.path == path:
                return item
        raise KeyError(path)


@dataclass(frozen=True)
class ReadingEnhancementsStatus:
    state: ReadingEnhancementsState
    identity: DeviceIdentity
    package: Optional[ReadingEnhancementsPackage] = None
    available_packages: tuple[ReadingEnhancementsPackage, ...] = ()
    detail: str = ""
    recovery_available: bool = False
    cleanup_available: bool = False


def _required(entry: dict, key: str, pattern: re.Pattern[str]) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RuntimeError(f"阅读增强清单字段 {key} 无效。")
    return value


def _expected_asset_name(platform: str, firmware: str, release_version: str) -> str:
    return f"rmtool-reading-enhancements-{platform}-{firmware}-{release_version}.tar.gz"


def parse_manifest(
    data: bytes, *, require_local_match: bool = True
) -> tuple[ReadingEnhancementsPackage, ...]:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("阅读增强清单不是有效 JSON。") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise RuntimeError("阅读增强清单版本不受支持。")
    entries = document.get("packages")
    if not isinstance(entries, list):
        raise RuntimeError("阅读增强清单缺少 packages。")

    packages: list[ReadingEnhancementsPackage] = []
    identities: set[tuple[str, str, str]] = set()
    assets: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "firmware",
            "release_version",
            "channel",
            "platform",
            "architecture",
            "xochitl_sha256",
            "offline_verified",
            "device_verified",
            "package_revision",
            "asset",
            "sha256",
            "size",
            "urls",
            "files",
        }:
            raise RuntimeError("阅读增强清单包格式无效。")
        firmware = _required(entry, "firmware", tap._FIRMWARE_RE)
        release_version = _required(entry, "release_version", tap._VERSION_RE)
        channel = entry.get("channel")
        if channel not in ("stable", "beta"):
            raise RuntimeError("阅读增强清单发布类型无效。")
        platform = _required(entry, "platform", tap._PLATFORM_RE)
        architecture = _required(entry, "architecture", tap._ARCH_RE)
        xochitl_sha = _required(entry, "xochitl_sha256", tap._SHA256_RE)
        asset = _required(entry, "asset", _ASSET_RE)
        digest = _required(entry, "sha256", tap._SHA256_RE)
        size = entry.get("size")
        revision = entry.get("package_revision")
        offline_verified = entry.get("offline_verified")
        device_verified = entry.get("device_verified")
        if revision != PACKAGE_REVISION:
            raise RuntimeError("阅读增强包修订版本不受支持。")
        if type(offline_verified) is not bool or type(device_verified) is not bool:
            raise RuntimeError("阅读增强包验证级别必须是布尔值。")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_PACKAGE_BYTES:
            raise RuntimeError("阅读增强资源包大小无效。")
        raw_files = entry.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise RuntimeError("阅读增强资源包缺少文件清单。")
        files = tuple(tap._parse_payload_file(item) for item in raw_files)
        paths = {item.path for item in files}
        if len(paths) != len(files) or paths != _PAYLOAD_PATHS:
            raise RuntimeError("阅读增强资源包文件清单与固定白名单不匹配。")
        if sum(item.size for item in files) > MAX_UNPACKED_BYTES:
            raise RuntimeError("阅读增强资源包解压后过大。")

        identity = (platform, firmware, xochitl_sha)
        if identity in identities or asset in assets:
            raise RuntimeError("阅读增强清单包含重复包。")
        expected = ALLOWED_TARGETS.get((platform, firmware, architecture, xochitl_sha))
        if expected != (release_version, channel, offline_verified, device_verified):
            raise RuntimeError("阅读增强清单身份或验证级别不在本地信任清单中。")
        if asset != _expected_asset_name(platform, firmware, release_version):
            raise RuntimeError("阅读增强资源包文件名与本地信任清单不一致。")
        urls = entry.get("urls")
        expected_urls = {f"{base}/{asset}" for base in REMOTE_BASE_URLS}
        if not isinstance(urls, list) or len(urls) != 2 or set(urls) != expected_urls:
            raise RuntimeError("阅读增强资源包下载源无效。")
        identities.add(identity)
        assets.add(asset)
        packages.append(
            ReadingEnhancementsPackage(
                firmware,
                release_version,
                channel,
                platform,
                architecture,
                xochitl_sha,
                asset,
                digest,
                size,
                files,
                tuple(urls),
                revision,
                offline_verified,
                device_verified,
            )
        )
    result = tuple(packages)
    if require_local_match and result != _trusted_catalog():
        raise RuntimeError("阅读增强清单与本地完整目标信任清单不一致。")
    return result


@lru_cache(maxsize=1)
def _trusted_catalog() -> tuple[ReadingEnhancementsPackage, ...]:
    if not BUNDLED_MANIFEST.is_file():
        raise RuntimeError("缺少内置阅读增强信任清单。")
    return parse_manifest(BUNDLED_MANIFEST.read_bytes(), require_local_match=False)


def _cache_dir(state_dir: str) -> Path:
    return Path(state_dir) / "cache" / FEATURE_ID


def load_catalog(
    state_dir: str, *, refresh: bool = True
) -> tuple[ReadingEnhancementsPackage, ...]:
    manifest_path = _cache_dir(state_dir) / "manifest.json"
    if refresh:
        for url in MANIFEST_URLS:
            try:
                data = tap._download_limited(url, MAX_MANIFEST_BYTES)
                catalog = parse_manifest(data)
                tap._write_atomic(manifest_path, data)
                return catalog
            except Exception as exc:
                logging.warning("Could not load reading-enhancements manifest from %s: %s", url, exc)
    for candidate in (manifest_path, BUNDLED_MANIFEST):
        if candidate.is_file():
            try:
                return parse_manifest(candidate.read_bytes())
            except Exception as exc:
                logging.warning("Reading-enhancements manifest is invalid (%s): %s", candidate, exc)
    raise RuntimeError("无法获取阅读增强清单，且没有可用缓存或内置清单。")


def download_package(package: ReadingEnhancementsPackage, state_dir: str) -> Path:
    destination = _cache_dir(state_dir) / package.firmware / package.asset
    if destination.is_file():
        data = destination.read_bytes()
        if len(data) == package.size and hashlib.sha256(data).hexdigest() == package.sha256:
            return destination
    last_error: Optional[Exception] = None
    for url in package.download_urls:
        try:
            data = tap._download_limited(url, MAX_PACKAGE_BYTES)
            if len(data) != package.size or hashlib.sha256(data).hexdigest() != package.sha256:
                raise RuntimeError("阅读增强资源包与清单校验不匹配。")
            tap._write_atomic(destination, data)
            return destination
        except Exception as exc:
            last_error = exc
            logging.warning("Could not download reading-enhancements package from %s: %s", url, exc)
    raise _package_download.PackageDownloadError(
        "阅读增强",
        package.asset,
        package.download_urls,
        package.size,
        package.sha256,
        store=lambda source_path: load_local_package(
            package, source_path, state_dir
        ),
    ) from last_error


def load_local_package(
    package: ReadingEnhancementsPackage, source_path: str | Path, state_dir: str
) -> Path:
    """Verify a manually downloaded archive and store it in the package cache."""
    data = Path(source_path).read_bytes()
    _package_download.verify_local_package(
        data, package.size, package.sha256, "阅读增强"
    )
    destination = _cache_dir(state_dir) / package.firmware / package.asset
    tap._write_atomic(destination, data)
    return destination


def extract_verified_package(
    archive_path: str | Path,
    package: ReadingEnhancementsPackage,
    destination: str | Path,
) -> Path:
    return tap.extract_verified_package(archive_path, package, destination)


def select_package(
    catalog: Iterable[ReadingEnhancementsPackage], identity: DeviceIdentity
) -> Optional[ReadingEnhancementsPackage]:
    return next(
        (
            item
            for item in catalog
            if (
                item.firmware,
                item.platform,
                item.architecture,
                item.xochitl_sha256,
            )
            == (
                identity.firmware,
                identity.platform,
                identity.architecture,
                identity.xochitl_sha256,
            )
        ),
        None,
    )


def _shared_specs(package: ReadingEnhancementsPackage):
    return shared.specs_from_package(package, FEATURE_ID, QMD_PAYLOAD_PATH)


def _known_published_revision_feature(package, current, revision):
    fingerprints = _PUBLISHED_REVISION_QMDS.get(revision)
    if fingerprints is None:
        return None
    variant = "3.27" if package.release_version.startswith("3.27.") else "3.28"
    predecessor = fingerprints.get(package.release_version, fingerprints.get(variant))
    if predecessor is None:
        return None
    return replace(current, sha256=predecessor[0], size=predecessor[1])


def _known_shared_predecessor_specs(package, current):
    predecessors = []
    seen = {current}
    for revision in reversed(tuple(_PUBLISHED_REVISION_QMDS)):
        predecessor = _known_published_revision_feature(
            package, current, revision
        )
        if predecessor is not None and predecessor not in seen:
            predecessors.append((f"package-revision-{revision}", predecessor))
            seen.add(predecessor)
    return tuple(predecessors)


def _trusted_context(identity: DeviceIdentity, package: ReadingEnhancementsPackage):
    runtime, peers, legacies = tap._trusted_shared_context(identity)
    peer_runtime, feature = _shared_specs(package)
    if peer_runtime != runtime:
        raise RuntimeError("阅读增强与现有共享 Xovi 运行资源不一致。")
    trusted = dict(peers)
    trusted[FEATURE_ID] = feature
    shared.assert_feature_layout(runtime, trusted.values())
    return runtime, trusted, tuple(legacies), feature


def _validated_legacy_standalone(ssh_client, legacies):
    """Return every exact old standalone tree, refusing partial or modified ones."""
    grouped = {}
    for legacy in legacies:
        grouped.setdefault(legacy.layout.remote_base, []).append(legacy)
    present = []
    for candidates in grouped.values():
        legacy = candidates[0]
        paths = (
            legacy.layout.remote_base,
            legacy.marker_path,
            legacy.layout.dropin_path,
        )
        if not any(ssh_client.file_exists(path) for path in paths):
            continue
        last_error = None
        matched = None
        for candidate in candidates:
            try:
                if shared.validate_legacy(ssh_client, candidate):
                    matched = candidate
                    break
            except Exception as exc:
                last_error = exc
        if matched is not None:
            present.append(matched)
            continue
        if last_error is not None:
            raise RuntimeError(
                f"检测到无法验证的 {legacy.feature.feature_id} 旧版独立安装，已拒绝修改。"
            ) from last_error
        raise RuntimeError(
            f"检测到不完整的 {legacy.feature.feature_id} 旧版独立安装，已拒绝修改。"
        )
    return tuple(present)


def _legacy_specs_for_identity(identity, current=()):
    """Return current and every exact supported old standalone specification."""
    result = list(current)
    # A supported reading target always supplies the current tap/fast legacy
    # specs through _trusted_context. Keeping an empty injected context empty
    # also makes the helper useful for callers that deliberately model a
    # reading-only shared tree without standalone peers.
    if not result:
        return ()
    seen = {(item.feature.feature_id, item.layout.remote_base, str(item.marker)) for item in result}
    modules = (tap,)
    try:
        import _fast_mono_reading as fast

        modules += (fast,)
    except ImportError:
        pass
    for module in modules:
        legacy_builder = getattr(module, "_legacy_spec", None)
        catalog_builder = getattr(module, "_trusted_catalog", None)
        if legacy_builder is None or catalog_builder is None:
            continue
        for package in catalog_builder():
            if (
                package.platform != identity.platform
                or package.architecture != identity.architecture
            ):
                continue
            legacy = legacy_builder(package)
            key = (legacy.feature.feature_id, legacy.layout.remote_base, str(legacy.marker))
            if key not in seen:
                result.append(legacy)
                seen.add(key)
    return tuple(result)


def _inspection_for_migration(ssh_client, runtime, trusted, package):
    """Accept current trust plus narrowly recognized predecessor payloads."""
    predecessors = _known_shared_predecessor_specs(
        package, trusted[FEATURE_ID]
    )
    revision_map = {}
    try:
        import _fast_mono_reading as fast

        fast_package = fast.select_package(fast._trusted_catalog(), DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        ))
        if fast_package is not None and "fast-mono-reading" in trusted:
            predecessors_for_fast = fast._known_shared_predecessor_specs(fast_package)
            revisions = {
                f"fast-mono-reading-revision-{revision}": predecessor
                for revision, predecessor in predecessors_for_fast
            }
        else:
            revisions = {}
        if revisions:
            revision_map["fast-mono-reading"] = tuple(revisions.items())
    except Exception:
        # Fast-reading predecessor trust is optional for targets where its
        # catalog has no historical revision record; current reading trust
        # remains fail-closed below.
        pass
    try:
        inspection, installed, selected = shared.inspect_shared_revisions(
            ssh_client,
            runtime,
            trusted,
            {
                **({FEATURE_ID: tuple(predecessors)} if predecessors else {}),
                **revision_map,
            },
            check_lower=True,
        )
        return inspection, installed, selected
    except RuntimeError as current_error:
        try:
            import _fast_mono_reading as fast

            old = fast.select_package(fast._trusted_catalog(), DeviceIdentity(
                package.firmware, package.platform, package.architecture, package.xochitl_sha256
            ))
            if old is not None:
                inspection, installed, outdated = fast._inspect_shared_revision(
                    ssh_client, runtime, trusted, old, check_lower=True
                )
                return inspection, installed, (
                    {"fast-mono-reading": "known-predecessor"} if outdated else {}
                )
        except Exception:
            pass
        raise current_error


def _target_states(inspection: shared.SharedInspection, trusted, feature, ssh_client):
    current = tap._xochitl_process_token(ssh_client)
    states = {}
    for feature_id, state in inspection.states.items():
        if feature_id in {"tap-page-turn", "fast-mono-reading"}:
            continue
        replacement = trusted.get(feature_id)
        if replacement is None:
            raise RuntimeError(f"共享 Xovi 包含无法验证的 peer：{feature_id}。")
        states[feature_id] = shared.SharedFeatureState(
            replacement,
            state.enabled,
            current if state.enabled else state.process_token,
        )
    # No old settings are copied. The QML starts from its safe defaults.
    states[FEATURE_ID] = shared.SharedFeatureState(feature, True, current)
    return states


def get_status(
    ssh_client,
    catalog: Iterable[ReadingEnhancementsPackage],
) -> ReadingEnhancementsStatus:
    packages = tuple(catalog)
    identity = tap.get_device_identity(ssh_client)
    available = tuple(item for item in packages if item.platform == identity.platform)
    package = select_package(packages, identity)
    if package is None:
        return ReadingEnhancementsStatus(
            ReadingEnhancementsState.INCOMPATIBLE,
            identity,
            available_packages=available,
            detail="当前设备身份不在阅读增强的精确信任清单中。",
        )
    try:
        runtime, trusted, legacies, feature = _trusted_context(identity, package)
        present_legacies = _validated_legacy_standalone(
            ssh_client, _legacy_specs_for_identity(identity, legacies)
        )
        shared_exists = shared.has_shared_artifacts(ssh_client)
        if tap._vellum_runtime_present(ssh_client):
            return ReadingEnhancementsStatus(
                ReadingEnhancementsState.BROKEN,
                identity,
                package,
                available,
                "检测到 Vellum/AppLoader Xovi 运行环境，或它与 rmtool 历史布局混合；"
                "为避免所有权冲突，已阻止阅读增强的安装、迁移、修复和清理。"
                "请先按 Vellum 官方说明移除运行环境，再使用 rmtool 管理插件。",
                False,
                False,
            )
        if present_legacies and shared_exists:
            raise RuntimeError(
                "检测到共享 Xovi 与旧版独立安装混合布局，拒绝修改。"
            )
        if present_legacies:
            return ReadingEnhancementsStatus(
                ReadingEnhancementsState.MIGRATION_AVAILABLE,
                identity,
                package,
                available,
                "检测到已验证的旧版独立阅读功能；迁移只替换软件包，完成后请在设备设置的“阅读增强”中重新开启需要的开关。",
                True,
                True,
            )
        if not shared_exists:
            return ReadingEnhancementsStatus(
                ReadingEnhancementsState.NOT_INSTALLED,
                identity,
                package,
                available,
            )
        inspection, installed_trusted, selected_predecessors = _inspection_for_migration(
            ssh_client, runtime, trusted, package
        )
        record = inspection.states.get(FEATURE_ID)
        predecessor = selected_predecessors.get(FEATURE_ID)
        if predecessor in _PUBLISHED_PREDECESSOR_REASONS:
            revision = predecessor.rsplit("-", 1)[-1]
            return ReadingEnhancementsStatus(
                ReadingEnhancementsState.REPAIR_AVAILABLE,
                identity,
                package,
                available,
                f"检测到已公开的阅读增强旧版（revision {revision}），可安全更新；"
                "其他共享功能和阅读设置会保留。",
                True,
                True,
            )
        if FEATURE_ID in selected_predecessors:
            raise RuntimeError("阅读增强旧版识别结果无效。")
        if record is None:
            old = {"tap-page-turn", "fast-mono-reading"} & set(inspection.states)
            if old:
                return ReadingEnhancementsStatus(
                    ReadingEnhancementsState.MIGRATION_AVAILABLE,
                    identity,
                    package,
                    available,
                    "检测到已验证的旧版阅读功能；迁移只替换软件包，完成后请在设备设置的“阅读增强”中重新开启需要的开关。",
                    True,
                    True,
                )
            return ReadingEnhancementsStatus(
                ReadingEnhancementsState.NOT_INSTALLED,
                identity,
                package,
                available,
                "共享 Xovi 正由其他已验证功能使用。",
                True,
            )
        old_shared = {"tap-page-turn", "fast-mono-reading"} & set(inspection.states)
        if old_shared:
            return ReadingEnhancementsStatus(
                ReadingEnhancementsState.MIGRATION_AVAILABLE,
                identity,
                package,
                available,
                "检测到旧版点击翻页或快速黑白共享功能，可迁移或清理；其他已验证功能会保留。",
                True,
                True,
            )
        current = tap._xochitl_process_token(ssh_client)
        if record.enabled:
            if current == record.process_token:
                return ReadingEnhancementsStatus(
                    ReadingEnhancementsState.ENABLE_PENDING_REBOOT,
                    identity, package, available, "等待手动重启后载入阅读增强。", True
                )
            if not inspection.active:
                raise RuntimeError("阅读增强未在当前 xochitl 进程中载入。")
            return ReadingEnhancementsStatus(ReadingEnhancementsState.ENABLED, identity, package, available, recovery_available=True)
        if current == record.process_token:
            return ReadingEnhancementsStatus(
                ReadingEnhancementsState.DISABLE_PENDING_REBOOT,
                identity, package, available, "等待手动重启后停用阅读增强。", True
            )
        return ReadingEnhancementsStatus(ReadingEnhancementsState.INSTALLED_DISABLED, identity, package, available, recovery_available=True)
    except Exception as exc:
        return ReadingEnhancementsStatus(
            ReadingEnhancementsState.BROKEN, identity, package, available, str(exc), True
        )


def install(
    ssh_client,
    package: ReadingEnhancementsPackage,
    archive_path: str | Path,
) -> ReadingEnhancementsStatus:
    identity = tap.get_device_identity(ssh_client)
    if select_package((package,), identity) is None:
        raise RuntimeError("当前设备与阅读增强包不精确匹配，未执行修改。")
    trusted_package = select_package(_trusted_catalog(), identity)
    if trusted_package != package:
        raise RuntimeError("阅读增强包与内置信任清单不一致，拒绝部署。")
    tap._preflight_device(ssh_client)
    runtime, trusted, legacies, feature = _trusted_context(identity, package)
    present_legacies = _validated_legacy_standalone(
        ssh_client, _legacy_specs_for_identity(identity, legacies)
    )
    shared_exists = shared.has_shared_artifacts(ssh_client)
    if present_legacies and shared_exists:
        raise RuntimeError("检测到共享 Xovi 与旧版独立安装混合布局，拒绝修改。")
    with tempfile.TemporaryDirectory() as temporary:
        extracted = extract_verified_package(archive_path, package, temporary)
        inspection = shared.SharedInspection({}, False, False)
        current_trusted = dict(trusted)
        if shared_exists:
            inspection, current_trusted, _selected_predecessors = _inspection_for_migration(
                ssh_client, runtime, current_trusted, package
            )
            if (
                inspection.states.get(FEATURE_ID) is not None
                and inspection.states[FEATURE_ID].spec == feature
                and inspection.states[FEATURE_ID].enabled
                and not ({"tap-page-turn", "fast-mono-reading"} & set(inspection.states))
            ):
                return get_status(ssh_client, (package,))
        states = _target_states(inspection, trusted, feature, ssh_client) if inspection.states else {
            FEATURE_ID: shared.SharedFeatureState(feature, True, tap._xochitl_process_token(ssh_client))
        }
        # The old tap/fast enabled bit is deliberately not copied.
        shared.replace_shared_features(
            ssh_client,
            runtime,
            current_trusted,
            runtime,
            trusted,
            states,
            {FEATURE_ID: extracted},
            legacy_layouts=tuple(item.layout for item in present_legacies),
        )
    return get_status(ssh_client, (package,))


def disable(ssh_client, catalog: Iterable[ReadingEnhancementsPackage]) -> ReadingEnhancementsStatus:
    status = get_status(ssh_client, catalog)
    if status.state in (ReadingEnhancementsState.NOT_INSTALLED, ReadingEnhancementsState.INCOMPATIBLE):
        return status
    if status.state == ReadingEnhancementsState.MIGRATION_AVAILABLE:
        raise RuntimeError("请先迁移到阅读增强，再停用。")
    if status.state == ReadingEnhancementsState.BROKEN:
        raise RuntimeError(status.detail or "阅读增强状态无法验证。")
    identity = status.identity
    package = status.package
    if package is None:
        raise RuntimeError("当前设备没有精确匹配的阅读增强包。")
    runtime, trusted, _legacies, feature = _trusted_context(identity, package)
    inspection, installed_trusted, _selected_predecessors = _inspection_for_migration(
        ssh_client, runtime, trusted, package
    )
    # Use the existing atomic disable path so the common runtime is copied
    # from the verified live tree without inventing a second staging format.
    installed = inspection.states[FEATURE_ID].spec
    shared.disable_shared(
        ssh_client,
        runtime,
        FEATURE_ID,
        installed_trusted,
        replacement_spec=feature if installed != feature else None,
    )
    return get_status(ssh_client, (package,))


def cleanup_legacy(
    ssh_client,
    catalog: Iterable[ReadingEnhancementsPackage],
) -> ReadingEnhancementsStatus:
    """Remove only exact legacy reading states, then report fresh-install state."""
    packages = tuple(catalog)
    status = get_status(ssh_client, packages)
    if status.state in (
        ReadingEnhancementsState.INCOMPATIBLE,
        ReadingEnhancementsState.BROKEN,
    ):
        raise RuntimeError(status.detail or "当前阅读增强状态无法验证，拒绝清理。")
    if status.state not in (
        ReadingEnhancementsState.MIGRATION_AVAILABLE,
        ReadingEnhancementsState.REPAIR_AVAILABLE,
    ) or not status.cleanup_available:
        raise RuntimeError("当前没有可验证的旧版阅读增强可清理。")
    package = status.package
    if package is None:
        raise RuntimeError("当前设备没有精确匹配的阅读增强包。")
    identity = status.identity
    runtime, trusted, legacies, _feature = _trusted_context(identity, package)
    present_legacies = _validated_legacy_standalone(
        ssh_client, _legacy_specs_for_identity(identity, legacies)
    )
    shared_exists = shared.has_shared_artifacts(ssh_client)
    if present_legacies and shared_exists:
        raise RuntimeError("检测到共享 Xovi 与旧版独立安装混合布局，拒绝清理。")
    if present_legacies:
        shared.remove_verified_legacy_batch(ssh_client, present_legacies)
        return get_status(ssh_client, packages)
    if not shared_exists:
        raise RuntimeError("旧版阅读增强状态已消失，未执行清理。")

    inspection, installed_trusted, selected = _inspection_for_migration(
        ssh_client, runtime, trusted, package
    )
    remove_ids = {
        "tap-page-turn", "fast-mono-reading"
    } & set(inspection.states)
    if selected.get(FEATURE_ID) is not None:
        remove_ids.add(FEATURE_ID)
    if not remove_ids:
        raise RuntimeError("未找到可验证的旧版阅读增强共享功能，拒绝清理。")
    shared.remove_shared_features(
        ssh_client,
        runtime,
        installed_trusted,
        remove_ids,
    )
    return get_status(ssh_client, packages)


def migrate(
    ssh_client,
    package: ReadingEnhancementsPackage,
    archive_path: str | Path,
) -> ReadingEnhancementsStatus:
    """Replace verified tap/fast states; never copy their UI preferences."""
    return install(ssh_client, package, archive_path)
