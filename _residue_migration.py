"""One-click migration of verified shared-Xovi firmware-upgrade residue."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

import _fast_mono_reading as fast
import _native_chinese as native
import _pinyin_input as pinyin
import _tap_page_turn as tap
import _xovi_standalone


FEATURE_LABELS = {
    "tap-page-turn": "点击翻页",
    "fast-mono-reading": "快速黑白阅读",
    "native-chinese": "独立简体中文",
    "pinyin-input": "拼音输入",
    "appload": "AppLoad/KOReader",
}


@dataclass(frozen=True)
class ResidueFeatureReport:
    feature_id: str
    label: str
    enabled: bool
    target_available: bool


@dataclass(frozen=True)
class ResidueReport:
    old_identity: tap.DeviceIdentity
    new_identity: tap.DeviceIdentity
    features: tuple[ResidueFeatureReport, ...]
    migratable: bool
    blockers: tuple[str, ...]
    detail: str = ""
    # The residue carries an unreleased launcher/drop-in generation; payload
    # anchors still verified, so cleanup/migration rebuild those two files.
    legacy_templates: bool = False


def _providers() -> Dict[str, object]:
    return {
        "tap-page-turn": tap,
        "fast-mono-reading": fast,
        "native-chinese": native,
        "pinyin-input": pinyin,
    }


def inspect_residue(ssh_client) -> ResidueReport | None:
    """Return the firmware-residue report, or None when there is no residue."""
    new_identity = tap.get_device_identity(ssh_client)
    if not _xovi_standalone.has_shared_artifacts(ssh_client):
        return None
    old_identity = tap.DeviceIdentity(
        *_xovi_standalone.read_shared_identity(ssh_client)
    )
    if old_identity == new_identity:
        return None
    try:
        old_runtime, old_trusted, _legacy = tap._trusted_shared_context(old_identity)
    except RuntimeError as exc:
        return ResidueReport(
            old_identity,
            new_identity,
            (),
            False,
            (str(exc),),
            "旧固件没有内置信任清单，残留只能手动清理，不能迁移。",
        )
    try:
        residue = _xovi_standalone.inspect_shared_firmware_residue(
            ssh_client,
            old_runtime,
            old_trusted,
            (
                new_identity.firmware,
                new_identity.platform,
                new_identity.architecture,
                new_identity.xochitl_sha256,
            ),
        )
        legacy_templates = False
    except RuntimeError:
        # Development-era deployments (pre-release launcher/drop-in design)
        # cannot reproduce their launcher hashes. Fall back to tolerant
        # verification: identity, runtime files, feature states, and
        # runtime_present must still match the trusted manifests, and the
        # on-disk launcher/systemd copy must hash to the marker's own values.
        try:
            residue = _xovi_standalone.inspect_shared_firmware_residue(
                ssh_client,
                old_runtime,
                old_trusted,
                (
                    new_identity.firmware,
                    new_identity.platform,
                    new_identity.architecture,
                    new_identity.xochitl_sha256,
                ),
                tolerate_legacy_templates=True,
            )
            legacy_templates = residue.legacy_templates
        except RuntimeError as exc:
            return ResidueReport(
                old_identity,
                new_identity,
                (),
                False,
                (str(exc),),
                "残留无法通过旧固件受信清单验证（含宽容校验），既不能自动迁移，"
                "也不能自动清理；请截图反馈以便分析。",
            )
    new_trusted: Dict[str, object] = {}
    blockers = []
    try:
        _new_runtime, new_trusted, _legacy = tap._trusted_shared_context(new_identity)
    except RuntimeError as exc:
        blockers.append(f"当前固件没有精确匹配的插件包（{exc}）")
    features = []
    for feature_id, state in sorted(residue.states.items()):
        label = FEATURE_LABELS.get(feature_id, feature_id)
        target_available = (
            feature_id in _xovi_standalone.MIGRATABLE_FEATURE_IDS
            and feature_id in new_trusted
        )
        if state.enabled and not target_available:
            blockers.append(
                f"{label}处于启用状态，但当前固件没有可迁移的精确包；请先停用该功能"
            )
        features.append(
            ResidueFeatureReport(feature_id, label, state.enabled, target_available)
        )
    detail = (
        "固件升级后检测到已验证的共享 Xovi 残留，可一键迁移到当前固件的精确包。"
        if not blockers
        else "残留已验证，但存在阻断项，暂不能一键迁移。"
    )
    if legacy_templates and not blockers:
        detail = (
            "固件升级后检测到共享 Xovi 残留：内部文件与已发布包逐字节一致，"
            "仅启动脚本为未发布的开发期变体；迁移会按当前固件模板整体重建，可安全继续。"
        )
    elif legacy_templates:
        detail = (
            "残留内部文件与已发布包逐字节一致（启动脚本为开发期变体），"
            "但存在阻断项，暂不能一键迁移。"
        )
    return ResidueReport(
        old_identity,
        new_identity,
        tuple(features),
        not blockers,
        tuple(blockers),
        detail,
        legacy_templates,
    )


def migrate(ssh_client, state_dir: str) -> ResidueReport:
    """Migrate a verified residue onto current-identity packages in one transaction."""
    report = inspect_residue(ssh_client)
    if report is None:
        raise RuntimeError("未检测到固件升级残留，无需迁移。")
    if not report.migratable:
        raise RuntimeError("；".join(report.blockers))
    old_runtime, old_trusted, _legacy = tap._trusted_shared_context(report.old_identity)
    new_runtime, new_trusted, _legacy = tap._trusted_shared_context(report.new_identity)
    providers = _providers()
    enabled_ids = [
        item.feature_id for item in report.features if item.enabled
    ]
    with tempfile.TemporaryDirectory() as temporary:
        roots: Dict[str, Path] = {}
        for feature_id in enabled_ids:
            module = providers[feature_id]
            package = module.select_package(
                module._trusted_catalog(), report.new_identity
            )
            if package is None:
                raise RuntimeError(f"{FEATURE_LABELS[feature_id]}没有当前固件的精确包。")
            archive = module.download_package(package, state_dir)
            extractor = getattr(
                module, "extract_verified_package", tap.extract_verified_package
            )
            destination = Path(temporary) / feature_id
            destination.mkdir()
            roots[feature_id] = extractor(archive, package, destination)
        _xovi_standalone.migrate_shared(
            ssh_client,
            old_runtime,
            old_trusted,
            new_runtime,
            {feature_id: new_trusted[feature_id] for feature_id in enabled_ids},
            roots,
            (),
            tolerate_legacy_templates=report.legacy_templates,
        )
    return report
