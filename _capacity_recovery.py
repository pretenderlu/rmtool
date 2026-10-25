"""Bounded home-only font recovery and original-install continuation."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, is_dataclass
import hashlib
import json
from typing import Optional

import _device_storage as storage
import _rmkit_cn
import _tap_page_turn as tap


_FEATURE_LABELS = {
    "pinyin-input": "拼音输入法",
    "qr-upload": "扫码传输",
}


def _identity_tuple(identity) -> tuple[str, str, str, str]:
    return (
        identity.firmware,
        identity.platform,
        identity.architecture,
        identity.xochitl_sha256,
    )


@dataclass(frozen=True)
class OriginalIntent:
    ssh_client: object
    client: object
    device: tuple[str, str, str, str]
    feature_id: str
    package: object
    state_dir: str

    def __post_init__(self) -> None:
        if self.feature_id not in _FEATURE_LABELS or not self.state_dir:
            raise ValueError("unsupported original install intent")

    @property
    def feature_label(self) -> str:
        return _FEATURE_LABELS[self.feature_id]


def make_original_intent(
    ssh_client, feature_id: str, package, state_dir: str, identity
) -> OriginalIntent:
    return OriginalIntent(
        ssh_client,
        getattr(ssh_client, "_client", None),
        _identity_tuple(identity),
        feature_id,
        package,
        state_dir,
    )


def fresh_intent(ssh_client, feature_id: str, state_dir: str) -> OriginalIntent:
    if feature_id == "pinyin-input":
        import _pinyin_input as module
        status = module.get_status(ssh_client, module._trusted_catalog())
    elif feature_id == "qr-upload":
        import _qr_upload as module
        status = module.get_status(
            ssh_client, module.load_catalog(state_dir, refresh=False)
        )
    else:
        raise RuntimeError("不支持从该功能重新检测容量。")
    if status.package is None:
        raise RuntimeError("当前设备没有精确匹配的资源包，无法继续容量恢复。")
    return make_original_intent(
        ssh_client, feature_id, status.package, state_dir, status.identity
    )


@dataclass(frozen=True)
class CapacityContext:
    kind: str
    partition: str
    required: int
    available: int
    shortfall: int


def capacity_context(error: BaseException) -> Optional[CapacityContext]:
    value = vars(error).get("recovery_context")
    if not isinstance(value, dict) or set(value) != {
        "kind", "partition", "required", "available", "shortfall"
    }:
        return None
    if (
        not isinstance(value["kind"], str)
        or not isinstance(value["partition"], str)
        or any(
            type(value[key]) is not int or value[key] < 0
            for key in ("required", "available", "shortfall")
        )
        or value["required"] - value["available"] != value["shortfall"]
    ):
        return None
    return CapacityContext(**value)


@dataclass(frozen=True)
class HomeOnlyFontPlan:
    snapshot: _rmkit_cn.HomeOnlyFontState

    @property
    def estimated_reclaim_bytes(self) -> int:
        return self.snapshot.size


def _json_default(value):
    if isinstance(value, frozenset):
        return sorted(value)
    raise TypeError(type(value).__name__)


def _fingerprint(intent, capacity, home_only, enable_plan) -> str:
    package_id = getattr(intent.package, "package_id", "")
    snapshot = home_only.snapshot if home_only is not None else None
    value = {
        "device": intent.device,
        "feature_id": intent.feature_id,
        "package_id": package_id,
        "partition": capacity.partition,
        "required": capacity.required,
        "enable_plan": asdict(enable_plan) if is_dataclass(enable_plan) else enable_plan,
        "home_only": None if snapshot is None else {
            "remote_path": snapshot.remote_path,
            "mirror_path": snapshot.mirror_path,
            "font_sha256": snapshot.font_sha256,
            "user_config_sha256": snapshot.user_config_sha256,
            "system_config_sha256": snapshot.system_config_sha256,
            "metadata": [
                (item.path, item.mode, item.uid, item.gid, item.nlink)
                for item in snapshot.metadata
            ],
            "mount_fingerprint": snapshot.mount_fingerprint,
            "size": snapshot.size,
        },
    }
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=_json_default
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CapacityPlan:
    intent: OriginalIntent
    capacity: CapacityContext
    home_only: Optional[HomeOnlyFontPlan]
    enable_plan: object
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "fingerprint",
            _fingerprint(self.intent, self.capacity, self.home_only, self.enable_plan),
        )


@dataclass(frozen=True)
class CapacityResult:
    state: str
    detail: str
    capacity: Optional[CapacityContext] = None
    cleaned: bool = False
    before_capacity: Optional[CapacityContext] = None
    after_capacity: Optional[CapacityContext] = None


def _validate_intent(ssh_client, intent: OriginalIntent) -> None:
    if ssh_client is not intent.ssh_client or getattr(ssh_client, "_client", None) is not intent.client:
        raise RuntimeError("设备连接已变化，请从原功能重新发起安装。")
    if _identity_tuple(tap.get_device_identity(ssh_client)) != intent.device:
        raise RuntimeError("设备或固件已变化，请重新检测。")


def _run_preflight(intent: OriginalIntent):
    if intent.feature_id == "pinyin-input":
        import _pinyin_input as module
        return module.preflight(intent.ssh_client, intent.package)
    import _qr_upload as module
    return module.preflight(intent.ssh_client, intent.package, with_plan=True)[3]


def _run_original(intent: OriginalIntent):
    if intent.feature_id == "pinyin-input":
        import _pinyin_input as module
        return module.enable_cloud(intent.ssh_client, intent.package, intent.state_dir)
    import _qr_upload as module
    return module.install_cloud(intent.ssh_client, intent.package, intent.state_dir)


def _preflight_result(intent: OriginalIntent):
    try:
        enable_plan = _run_preflight(intent)
    except Exception as exc:
        return capacity_context(exc), exc, getattr(exc, "shared_enable_plan", None)
    return None, None, enable_plan


def _matches_plan(plan: CapacityPlan, context: CapacityContext, enable_plan) -> bool:
    return plan.fingerprint == _fingerprint(
        plan.intent, context, plan.home_only, enable_plan
    )


def _measure_after(ssh_client, before: CapacityContext) -> CapacityContext:
    if before.partition == "/home":
        partition, available = "/home", storage.df_free_bytes(ssh_client, "/home")
    else:
        partition, available = storage.system_free_bytes(ssh_client)
    if partition != before.partition:
        raise RuntimeError("容量目标分区已变化，请重新检测。")
    return CapacityContext(
        before.kind,
        partition,
        before.required,
        available,
        max(0, before.required - available),
    )


def _post_cleanup_result(
    state: str,
    detail: str,
    plan: CapacityPlan,
    after: Optional[CapacityContext],
) -> CapacityResult:
    return CapacityResult(
        state,
        detail,
        after or plan.capacity,
        True,
        plan.capacity,
        after,
    )


def scan(ssh_client, intent: OriginalIntent) -> CapacityPlan | CapacityResult:
    try:
        _validate_intent(ssh_client, intent)
    except Exception as exc:
        return CapacityResult("preflight_failed", str(exc))
    context, error, enable_plan = _preflight_result(intent)
    if error is None:
        return CapacityResult("not_needed", "当前空间已满足要求，未自动续装；请从原功能继续安装。")
    if context is None or context.kind != "shared-capacity":
        return CapacityResult("preflight_failed", f"原安装预检未通过：{error}")
    if enable_plan is None:
        return CapacityResult("preflight_failed", "无法确认完整共享安装目标，已停止。", context)
    try:
        snapshot = _rmkit_cn.inspect_home_only_font(ssh_client)
    except Exception as exc:
        return CapacityResult(
            "no_candidate",
            f"没有找到可安全处理的 home-only 字体候选：{exc}",
            context,
        )
    return CapacityPlan(intent, context, HomeOnlyFontPlan(snapshot), enable_plan)


def execute(ssh_client, plan: CapacityPlan) -> CapacityResult:
    session = getattr(ssh_client, "operation_session", nullcontext)
    with session():
        try:
            _validate_intent(ssh_client, plan.intent)
        except Exception as exc:
            return CapacityResult("state_drift", str(exc))
        context, error, enable_plan = _preflight_result(plan.intent)
        if error is None:
            return CapacityResult("not_needed", "空间已满足要求，未执行字体修改或自动续装。")
        if context is None or context.kind != "shared-capacity":
            return CapacityResult("preflight_failed", f"原安装预检未通过：{error}")
        if plan.home_only is None or not _matches_plan(plan, context, enable_plan):
            return CapacityResult("state_drift", "共享安装目标已变化，请重新检测。", context)
        try:
            snapshot = _rmkit_cn.inspect_home_only_font(ssh_client)
            if snapshot != plan.home_only.snapshot:
                raise RuntimeError("字体状态已变化，请重新检测。")
        except Exception as exc:
            return CapacityResult("state_drift", str(exc), context)
        try:
            _rmkit_cn.set_active_user_font(
                ssh_client,
                plan.home_only.snapshot.remote_dir,
                plan.home_only.snapshot.filename,
                False,
                expected_home_only=plan.home_only.snapshot,
            )
        except Exception as exc:
            return CapacityResult(
                "cleanup_failed",
                f"home-only 字体事务失败，未继续原安装：{exc}",
                context,
            )
        context, error, enable_plan = _preflight_result(plan.intent)
        if error is not None:
            if context is not None and context.kind == "shared-capacity":
                if not _matches_plan(plan, context, enable_plan):
                    return _post_cleanup_result(
                        "state_drift", "清理后共享安装目标已变化，未继续原安装。", plan, context
                    )
                return _post_cleanup_result(
                    "insufficient",
                    "字体布局已调整，但空间仍不足；没有更多候选，未继续原安装。",
                    plan,
                    context,
                )
            return CapacityResult(
                "preflight_failed",
                f"字体布局已调整，但原安装预检未通过：{error}",
                context,
                True,
                plan.capacity,
                context,
            )
        try:
            after = _measure_after(ssh_client, plan.capacity)
        except Exception as exc:
            return _post_cleanup_result(
                "state_drift", f"清理后无法重新确认实际容量：{exc}", plan, None
            )
        if not _matches_plan(plan, plan.capacity, enable_plan):
            return _post_cleanup_result(
                "state_drift", "清理后共享安装目标已变化，未继续原安装。", plan, after
            )
        try:
            _run_original(plan.intent)
        except Exception as exc:
            return _post_cleanup_result(
                "install_failed", f"字体布局已调整，但原安装失败：{exc}", plan, after
            )
        return _post_cleanup_result(
            "deployed",
            f"{plan.intent.feature_label}已部署，请手动重启设备。",
            plan,
            after,
        )
