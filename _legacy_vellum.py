"""Strict cleanup for rmtool's two historical Vellum feature packages."""

import _fast_mono_reading as fast
import _tap_page_turn as tap
import _xovi_standalone


def remove_legacy_plugins(ssh_client) -> tuple[str, ...]:
    if not ssh_client.file_exists(tap.VELLUM_BIN):
        return ()

    installed = tap._vellum_installed_packages(ssh_client)
    detected = tuple(
        name for name in tap.RMTOOL_VELLUM_PACKAGE_NAMES if name in installed
    )
    if not detected:
        return ()
    if _xovi_standalone.has_shared_artifacts(ssh_client):
        raise RuntimeError(
            "检测到当前 rmtool 共享 Xovi，拒绝卸载路径可能重叠的旧版 Vellum 插件。"
        )
    validators = {
        tap.VELLUM_PACKAGE_NAME: tap._validate_vellum_removal,
        fast.VELLUM_PACKAGE_NAME: fast._validate_vellum_removal,
    }
    removers = {
        tap.VELLUM_PACKAGE_NAME: tap._remove_validated_vellum,
        fast.VELLUM_PACKAGE_NAME: fast._remove_validated_vellum,
    }

    for name in detected:
        validators[name](ssh_client)
    removed = []
    for name in detected:
        try:
            removers[name](ssh_client)
        except Exception as exc:
            completed = "、".join(removed) or "无"
            raise RuntimeError(
                f"旧版插件清理未全部完成；已确认卸载：{completed}；"
                f"{name} 的卸载结果无法确认：{exc}"
            ) from exc
        removed.append(name)
    return detected
