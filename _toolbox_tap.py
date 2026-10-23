"""Classic tap-to-turn section for RM1/RM2/Paper Pure."""

import logging
from typing import Optional

from PyQt5 import QtCore, QtGui, QtWidgets

import _tap_page_turn
from _ssh import SSHClientWrapper, require_connection
# Dialog helpers are looked up on _toolbox_common at call time so a single
# patch point covers every toolbox section.
import _toolbox_common as _ui
import rmtool as _rmtool  # late-bound access to avoid circular import
from _toolbox_common import ToolboxStatusLabel


def _tap_page_turn_status(ssh_client, state_dir: str):
    catalog = _tap_page_turn.load_catalog(state_dir, refresh=True)
    return _tap_page_turn.get_status(ssh_client, catalog)


class TapPageTurnSection(QtWidgets.QWidget):
    """Tap-to-turn management for RM1/RM2/Paper Pure (reading-enhancements
    has no packages for these devices). All these targets are offline
    verified only; the UI must keep saying so."""

    OFFLINE_NOTICE = (
        "注意：该设备系列的点击翻页包仅通过官方固件离线验证，"
        "尚未进行实机验证。"
    )

    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._status: Optional[_tap_page_turn.TapPageTurnStatus] = None
        self._busy = False
        self._other_packages_count = 0

        title = QtWidgets.QLabel("点击翻页")
        title.setObjectName("toolboxFeatureTitle")
        detail = QtWidgets.QLabel(
            "在 PDF 和 EPUB 阅读页使用屏幕分区点击上一页或下一页，滑动翻页保持可用。"
            "功能按硬件、内部固件版本和 xochitl 哈希精确匹配，并在冷启动后持续生效。"
            + self.OFFLINE_NOTICE
        )
        detail.setWordWrap(True)

        self.catalog_label = QtWidgets.QLabel("当前固件点击翻页包：检测后显示")
        self.catalog_label.setObjectName("tapPageTurnCatalog")
        self.catalog_label.setWordWrap(True)
        self.catalog_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        self.other_packages_button = QtWidgets.QPushButton("其他固件版本")
        self.other_packages_button.setCheckable(True)
        self.other_packages_button.setSizePolicy(
            QtWidgets.QSizePolicy.Maximum,
            QtWidgets.QSizePolicy.Preferred,
        )
        self.other_packages_button.hide()

        self.other_packages_label = QtWidgets.QLabel()
        self.other_packages_label.setObjectName("tapPageTurnOtherCatalog")
        self.other_packages_label.setWordWrap(True)
        self.other_packages_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse
        )
        self.other_packages_label.hide()

        self.status_label = ToolboxStatusLabel("设备已连接，尚未检测")
        self.status_label.setObjectName("tapPageTurnDeviceStatus")
        self.status_label.setWordWrap(True)

        self.detect_button = QtWidgets.QPushButton("检测状态")
        self.enable_button = QtWidgets.QPushButton("启用点击翻页")
        self.enable_button.setProperty("btnRole", "primary")
        self.disable_button = QtWidgets.QPushButton("停用")
        self.uninstall_button = QtWidgets.QPushButton("卸载")
        self.uninstall_button.setProperty("btnRole", "danger")
        self.load_package_button = QtWidgets.QPushButton("加载本地资源包…")
        self.vellum_help_button = QtWidgets.QPushButton("Vellum 官方卸载说明")
        self.vellum_help_button.hide()
        self.project_button = QtWidgets.QPushButton("查看说明")

        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        for button in (
            self.detect_button,
            self.enable_button,
            self.disable_button,
            self.uninstall_button,
            self.load_package_button,
            self.vellum_help_button,
            self.project_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(title)
        layout.addWidget(detail)
        layout.addWidget(self.catalog_label)
        layout.addWidget(self.other_packages_button, alignment=QtCore.Qt.AlignLeft)
        layout.addWidget(self.other_packages_label)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self.other_packages_button.toggled.connect(self._toggle_other_packages)
        self.detect_button.clicked.connect(self._detect_status)
        self.enable_button.clicked.connect(self._enable)
        self.disable_button.clicked.connect(self._disable)
        self.uninstall_button.clicked.connect(self._uninstall)
        self.load_package_button.clicked.connect(self._load_local_package)
        self.vellum_help_button.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl(_tap_page_turn.VELLUM_UNINSTALL_URL)
            )
        )
        self.project_button.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl(f"{_tap_page_turn.REPO_URL}/tree/main/tap-page-turn")
            )
        )
        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(self.ssh_client.is_connected())

    @staticmethod
    def _package_display_text(package: _tap_page_turn.TapPageTurnPackage) -> str:
        # The tap manifest predates verification flags, but this section only
        # appears for RM1/RM2/Paper Pure, which are offline verified only.
        channel_names = {"stable": "正式版", "beta": "测试版"}
        return (
            f"{package.release_version} | {channel_names[package.channel]} | "
            f"硬件 {package.platform.title()} | 内部版本 {package.firmware} | "
            f"离线验证（未实机）"
        )

    def _on_connection_changed(self, connected: bool):
        if not connected:
            self._status = None
            self.catalog_label.setText("当前固件点击翻页包：检测后显示")
            self.other_packages_button.setChecked(False)
            self._other_packages_count = 0
            self.other_packages_button.setText("其他固件版本")
            self.other_packages_label.clear()
            self.other_packages_button.hide()
            self.other_packages_label.hide()
            self.status_label.setText("设备未连接")
        elif self._status is None:
            self.status_label.setText("设备已连接，尚未检测")
        self.detect_button.setEnabled(connected and not self._busy)
        self._update_buttons()

    def _toggle_other_packages(self, expanded: bool):
        self.other_packages_button.setText(
            f"其他固件版本（{self._other_packages_count}） "
            + ("⌄" if expanded else "›")
        )
        self.other_packages_label.setVisible(
            expanded and not self.other_packages_button.isHidden()
        )

    def _update_buttons(self):
        connected = self.ssh_client.is_connected() and not self._busy
        state = self._status.state if self._status else None
        if state == _tap_page_turn.TapPageTurnState.FIRMWARE_RESIDUE:
            self.disable_button.setText("清理残留")
        elif state in (
            _tap_page_turn.TapPageTurnState.OUTDATED,
            _tap_page_turn.TapPageTurnState.LEGACY_VELLUM,
        ):
            self.disable_button.setText("卸载旧版")
        else:
            self.disable_button.setText("停用")
        self.detect_button.setEnabled(connected)
        self.enable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.package is not None
            and state
            in (
                _tap_page_turn.TapPageTurnState.NOT_INSTALLED,
                _tap_page_turn.TapPageTurnState.INSTALLED_DISABLED,
            )
        )
        self.disable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.dropin_present
            and state
            in (
                _tap_page_turn.TapPageTurnState.ENABLE_PENDING_REBOOT,
                _tap_page_turn.TapPageTurnState.ENABLED,
                _tap_page_turn.TapPageTurnState.DISABLE_PENDING_REBOOT,
                _tap_page_turn.TapPageTurnState.OUTDATED,
                _tap_page_turn.TapPageTurnState.LEGACY_VELLUM,
                _tap_page_turn.TapPageTurnState.FIRMWARE_RESIDUE,
                _tap_page_turn.TapPageTurnState.BROKEN,
            )
        )
        self.uninstall_button.setEnabled(
            connected
            and self._status is not None
            and state in (
                _tap_page_turn.TapPageTurnState.INSTALLED_DISABLED,
                _tap_page_turn.TapPageTurnState.ENABLE_PENDING_REBOOT,
                _tap_page_turn.TapPageTurnState.ENABLED,
                _tap_page_turn.TapPageTurnState.DISABLE_PENDING_REBOOT,
            )
        )
        self.load_package_button.setEnabled(
            connected and self._status is not None and self._status.package is not None
        )
        self.vellum_help_button.setVisible(
            state in (
                _tap_page_turn.TapPageTurnState.LEGACY_VELLUM,
                _tap_page_turn.TapPageTurnState.VELLUM_RUNTIME,
            )
        )
        self.project_button.setEnabled(True)

    def _apply_status(self, status: _tap_page_turn.TapPageTurnStatus):
        self._status = status
        if status.package is not None:
            self.catalog_label.setText(
                "当前固件点击翻页包：\n" + self._package_display_text(status.package)
            )
        else:
            self.catalog_label.setText("当前固件点击翻页包：没有精确匹配版本")

        other_packages = tuple(
            package
            for package in status.available_packages
            if package != status.package
            and package.platform == status.identity.platform
        )
        self.other_packages_button.setChecked(False)
        if other_packages:
            self._other_packages_count = len(other_packages)
            self.other_packages_button.setText(
                f"其他固件版本（{self._other_packages_count}） ›"
            )
            self.other_packages_label.setText(
                "\n".join(
                    self._package_display_text(package) for package in other_packages
                )
            )
            self.other_packages_button.show()
        else:
            self._other_packages_count = 0
            self.other_packages_button.setText("其他固件版本")
            self.other_packages_label.clear()
            self.other_packages_button.hide()
        self.other_packages_label.hide()

        messages = {
            _tap_page_turn.TapPageTurnState.INCOMPATIBLE: (
                "没有与当前设备、固件和 xochitl 哈希精确匹配的点击翻页包"
            ),
            _tap_page_turn.TapPageTurnState.NOT_INSTALLED: "尚未安装点击翻页",
            _tap_page_turn.TapPageTurnState.INSTALLED_DISABLED: (
                "点击翻页资源已缓存，持久化当前未启用"
            ),
            _tap_page_turn.TapPageTurnState.ENABLE_PENDING_REBOOT: (
                "持久化已部署，等待冷启动生效"
            ),
            _tap_page_turn.TapPageTurnState.ENABLED: "点击翻页已启用并正在运行",
            _tap_page_turn.TapPageTurnState.DISABLE_PENDING_REBOOT: (
                "持久化已停用，当前进程将在冷启动后恢复原生"
            ),
            _tap_page_turn.TapPageTurnState.OUTDATED: (
                "检测到旧固件的 rmtool 点击翻页，请先卸载旧版"
            ),
            _tap_page_turn.TapPageTurnState.LEGACY_VELLUM: (
                "检测到 rmtool 安装的旧版 Vellum 点击翻页包，请先卸载"
            ),
            _tap_page_turn.TapPageTurnState.VELLUM_RUNTIME: (
                "Vellum/AppLoader Xovi 仍在设备中，rmtool 插件安装已暂停"
            ),
            _tap_page_turn.TapPageTurnState.FIRMWARE_RESIDUE: (
                "检测到固件升级后遗留的旧共享 Xovi 状态，可安全清理"
            ),
            _tap_page_turn.TapPageTurnState.BROKEN: (
                "检测到不完整或被修改的点击翻页安装，请先停用"
            ),
        }
        message = messages[status.state]
        if status.detail:
            message = f"{message}：{status.detail}"
        identity = status.identity
        message += (
            f"\n设备：{identity.platform or '未知'} | "
            f"内部版本 {identity.firmware or '未知'}"
        )
        message += "\n" + self.OFFLINE_NOTICE
        self.status_label.setText(message)
        self._update_buttons()

    def _set_busy(self, busy: bool, message: str = ""):
        self._busy = busy
        if message:
            self.status_label.setText(message)
        self._update_buttons()

    def _start_worker(
        self,
        fn,
        *args,
        pending: str,
        success: str = "",
        close_connection: bool = False,
        on_done=None,
        show_errors: bool = True,
    ):
        return _ui.start_plugin_worker(
            self, fn, *args, pending=pending, success=success,
            close_connection=close_connection, on_done=on_done,
            show_errors=show_errors, retry=self._enable,
        )

    @require_connection
    def _detect_status(self):
        self._start_status_detection()

    def _start_status_detection(self, *, on_done=None, show_errors: bool = True):
        self._start_worker(
            _tap_page_turn_status,
            self.ssh_client,
            str(_rmtool.app_state_dir()),
            pending="正在获取清单并核对点击翻页包与设备状态…",
            on_done=on_done,
            show_errors=show_errors,
        )

    @require_connection
    def _enable(self):
        if not self._status or not self._status.package:
            return
        verification_notice = (
            "该精确包已完成对应真机验证。"
            if self._status.package.device_verified
            else "该精确包已完成官方固件离线验证，尚未进行实机验证。"
        )
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"{verification_notice}"
            "将下载并校验固件专用资源，并安装到 rmtool 管理的共享 Xovi。"
            "若检测到 Vellum/AppLoader Xovi，安装会在上传前停止。"
            "本次操作不会重启界面或设备；完成后 SSH 会话会关闭，"
            "请从设备菜单手动冷启动。是否继续？",
            confirm_text="部署持久化",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _tap_page_turn.enable_cloud,
            self.ssh_client,
            self._status.package,
            str(_rmtool.app_state_dir()),
            pending="正在下载、逐文件校验并部署点击翻页资源…",
            success=(
                "点击翻页持久化已部署并通过校验，SSH 会话已关闭。\n"
                "请从设备菜单手动重新启动；不要通过 rmtool 立即重启。"
            ),
            close_connection=True,
        )

    @require_connection
    def _disable(self):
        if not self._status:
            return
        state = self._status.state
        if state is _tap_page_turn.TapPageTurnState.LEGACY_VELLUM:
            title = "卸载旧版 Vellum 点击翻页"
            confirmation = (
                "将卸载经精确验证的 rmtool Vellum 点击翻页包；"
                "不会卸载 Vellum/AppLoader 运行环境或其他插件。"
                "完成后请按 Vellum 官方说明处理其运行环境。是否继续？"
            )
            confirm_text = "卸载旧版"
            pending = "正在验证并卸载 rmtool 的旧版 Vellum 点击翻页包…"
            success = (
                "旧版 Vellum 点击翻页包已卸载，SSH 会话已关闭。\n"
                "请重新连接，按 Vellum 官方说明卸载其运行环境后，再安装 rmtool 版本。"
            )
        elif state is _tap_page_turn.TapPageTurnState.OUTDATED:
            title = "卸载旧版点击翻页"
            confirmation = (
                "将卸载经精确验证的旧固件点击翻页。"
                "操作不会重启设备；完成后请手动重启，"
                "再重新检测并安装当前版本。是否继续？"
            )
            confirm_text = "卸载旧版"
            pending = "正在卸载旧固件点击翻页并保留可验证的同伴功能…"
            success = (
                "点击翻页持久化已移除，SSH 会话已关闭。\n"
                "请从设备菜单手动重新启动。"
            )
        else:
            title = _rmtool.APP_NAME
            confirmation = (
                "将停用 rmtool 共享 Xovi 中的点击翻页配置；"
                "其他 rmtool 功能及其共享运行时会按需保留。"
                "资源缓存会保留，本次操作不会重启界面或设备；完成后请手动冷启动。"
                "是否继续？"
            )
            confirm_text = "停用点击翻页"
            pending = "正在移除点击翻页持久化配置…"
            success = (
                "点击翻页持久化已移除，SSH 会话已关闭。\n"
                "请从设备菜单手动重新启动。"
            )
        if not _ui.ask_confirmation(
            self,
            title,
            confirmation,
            confirm_text=confirm_text,
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _tap_page_turn.disable,
            self.ssh_client,
            self._status.available_packages,
            pending=pending,
            success=success,
            close_connection=True,
        )

    @require_connection
    def _uninstall(self):
        if not self._status or self._status.package is None:
            return
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将卸载点击翻页及其 rmtool 管理的共享文件；其他插件和共享运行时会保留。"
            "本次不会自动重启设备，完成后请手动重启。是否继续？",
            confirm_text="卸载点击翻页",
            cancel_text="取消",
            danger=True,
        ):
            return
        self._start_worker(
            _tap_page_turn.uninstall,
            self.ssh_client,
            self._status.available_packages,
            pending="正在验证并卸载点击翻页…",
            success="点击翻页已卸载，SSH 会话已关闭。\n请手动重启设备。",
            close_connection=True,
        )

    def _load_local_package(self):
        if not self._status or not self._status.package:
            return
        package = self._status.package
        source_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "加载本地点击翻页资源包",
            "",
            "资源包 (*.tar.gz);;所有文件 (*)",
        )
        if not source_path:
            return
        try:
            stored = _tap_page_turn.load_local_package(
                package, source_path, str(_rmtool.app_state_dir())
            )
        except Exception as exc:
            logging.error("Manual tap-to-turn package load failed: %s", exc)
            _ui.show_error(self, _rmtool.APP_NAME, f"手动加载失败：{exc}")
            return
        _ui.show_info(
            self,
            _rmtool.APP_NAME,
            "本地资源包已通过大小与 SHA-256 校验，并写入缓存：\n"
            f"{stored}\n\n点击“启用点击翻页”时会优先使用这份缓存。",
        )
