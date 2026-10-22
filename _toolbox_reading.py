"""Reading/note enhancement and WeRead sections plus their backend adapters."""

import logging

from PyQt5 import QtCore, QtWidgets, sip

import _package_download
import _rmkit_cn
import _note_enhancements
import _reading_enhancements
import _weread_app
import _weread_launcher
import _qr_upload
from _ssh import SSHClientWrapper, require_connection
# Dialog helpers are looked up on _toolbox_common at call time so a single
# patch point covers every toolbox section.
import _toolbox_common as _ui
import rmtool as _rmtool  # late-bound access to avoid circular import
from _toolbox_common import ToolboxStatusLabel


def _reading_enhancements_status(ssh_client, state_dir: str):
    catalog = _reading_enhancements.load_catalog(state_dir, refresh=True)
    return _reading_enhancements.get_status(ssh_client, catalog)


def _install_reading_enhancements(
    ssh_client,
    package,
    state_dir: str,
    migrate: bool,
):
    archive = _reading_enhancements.download_package(package, state_dir)
    operation = (
        _reading_enhancements.migrate
        if migrate
        else _reading_enhancements.install
    )
    return operation(ssh_client, package, archive)


def _set_epub_font_with_menu_support(
    ssh_client,
    remote_dir: str,
    filename: str,
    state_dir: str,
):
    """Prepare the trusted EPUB menu component before writing a font slot."""
    catalog = _reading_enhancements.load_catalog(state_dir, refresh=True)
    status = _reading_enhancements.get_status(ssh_client, catalog)
    states = _reading_enhancements.ReadingEnhancementsState
    reusable = (states.ENABLED, states.ENABLE_PENDING_REBOOT)
    deployable = (
        states.NOT_INSTALLED,
        states.INSTALLED_DISABLED,
        states.DISABLE_PENDING_REBOOT,
        states.MIGRATION_AVAILABLE,
        states.REPAIR_AVAILABLE,
    )
    if status.state in (states.INCOMPATIBLE, states.BROKEN):
        reason = (
            "当前固件没有可用的 EPUB 字体菜单支持。"
            if status.state is states.INCOMPATIBLE
            else "检测到插件状态异常，请先在“插件与工具”中完成修复。"
        )
        raise RuntimeError(f"{reason}\n{status.detail}\n未修改 EPUB 字体。")
    if status.state not in reusable:
        if status.state not in deployable or status.package is None:
            raise RuntimeError("无法安全准备 EPUB 字体菜单，未修改 EPUB 字体。")
        deployed = _install_reading_enhancements(
            ssh_client,
            status.package,
            state_dir,
            status.state is states.MIGRATION_AVAILABLE,
        )
        if deployed.state not in reusable:
            raise RuntimeError(f"EPUB 字体菜单支持未能通过验证：{deployed.detail}\n未修改 EPUB 字体。")
    try:
        return _rmkit_cn.set_epub_font_slot(ssh_client, remote_dir, filename)
    except Exception as exc:
        logging.error("EPUB font slot write failed after menu check: %s", exc)
        raise RuntimeError(f"EPUB 字体菜单已准备，但字体写入失败：{exc}\n请刷新状态后重试。") from exc


def _cleanup_reading_enhancements(ssh_client, state_dir: str):
    catalog = _reading_enhancements.load_catalog(state_dir, refresh=True)
    return _reading_enhancements.cleanup_legacy(ssh_client, catalog)


def _note_enhancements_status(ssh_client, state_dir: str):
    catalog = _note_enhancements.load_catalog(state_dir, refresh=True)
    return _note_enhancements.get_status(ssh_client, catalog)


def _install_note_enhancements(
    ssh_client,
    package,
    state_dir: str,
    migrate: bool,
):
    archive = _note_enhancements.download_package(package, state_dir)
    operation = _note_enhancements.migrate if migrate else _note_enhancements.install
    return operation(ssh_client, package, archive)


def _cleanup_note_enhancements(ssh_client, state_dir: str):
    catalog = _note_enhancements.load_catalog(state_dir, refresh=True)
    return _note_enhancements.cleanup_legacy(ssh_client, catalog)


def _weread_launcher_status(ssh_client, state_dir: str):
    catalog = _weread_launcher.load_catalog(state_dir, refresh=True)
    return _weread_launcher.get_status(ssh_client, catalog)


def _weread_combined_status(ssh_client, state_dir: str):
    app_status = _weread_app.get_status(ssh_client)
    return app_status, _weread_launcher_status(ssh_client, state_dir)


def _install_weread_app_and_status(ssh_client, state_dir: str):
    app_status = _weread_app.install_online(ssh_client, state_dir)
    return app_status, _weread_launcher_status(ssh_client, state_dir)


def _install_weread_launcher(ssh_client, package, state_dir: str, migrate: bool):
    del migrate
    archive = _weread_launcher.download_package(package, state_dir)
    return _weread_launcher.install(ssh_client, package, archive)


def _cleanup_weread_launcher(ssh_client, state_dir: str):
    catalog = _weread_launcher.load_catalog(state_dir, refresh=True)
    return _weread_launcher.cleanup_legacy(ssh_client, catalog)


class ReadingEnhancementsSection(QtWidgets.QWidget):
    """Combined browser entry for the native reading-enhancement package."""

    def __init__(
        self,
        ssh_client: SSHClientWrapper,
        parent=None,
        *,
        backend=_reading_enhancements,
        state_type=_reading_enhancements.ReadingEnhancementsState,
        status_loader=_reading_enhancements_status,
        installer=_install_reading_enhancements,
        cleaner=_cleanup_reading_enhancements,
        feature_name="阅读增强",
        device_page_name=None,
        description=(
            "为 PDF 和 EPUB 阅读提供点击翻页、快刷模式和翻页清残影；"
            "每本书可独立选择普通、彩色快刷或黑白快刷。"
            "Paper Pro 与 Paper Pro Move 3.28.0.172、3.29.0.152 和 3.29.0.153 另提供中文划词精确选取。"
            "日常开关由设备的“设置 > 阅读增强”页面控制。"
        ),
        explanation=(
            "阅读增强只作用于 PDF 和 EPUB 阅读页，包含点击翻页、快刷模式和"
            "翻页清残影。全局设置只负责授权快刷，每本书可独立选择普通、"
            "彩色快刷或黑白快刷；精确匹配的 Paper Pro 与 Paper Pro Move 3.28.0.172、3.29.0.152 和 3.29.0.153 "
            "另提供中文划词精确选取。"
            "安装或迁移完成后请手动重启设备，再到“设置 > 阅读增强”开启需要的开关。"
        ),
    ):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.backend = backend
        self.state_type = state_type
        self.status_loader = status_loader
        self.installer = installer
        self.cleaner = cleaner
        self.feature_name = feature_name
        self.device_page_name = device_page_name or feature_name
        self.explanation = explanation
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._status = None
        self._busy = False

        self.title_label = QtWidgets.QLabel(feature_name)
        self.title_label.setObjectName("toolboxFeatureTitle")
        self.detail_label = QtWidgets.QLabel(description)
        self.detail_label.setWordWrap(True)

        self.catalog_label = QtWidgets.QLabel(f"当前固件{feature_name}包：检测后显示")
        self.catalog_label.setObjectName("readingEnhancementsCatalog")
        self.catalog_label.setWordWrap(True)
        self.catalog_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        self.status_label = ToolboxStatusLabel("设备已连接，尚未检测")
        self.status_label.setObjectName("readingEnhancementsDeviceStatus")
        self.status_label.setWordWrap(True)

        self.detect_button = QtWidgets.QPushButton("检测状态")
        self.install_button = QtWidgets.QPushButton(f"安装{feature_name}")
        self.install_button.setProperty("btnRole", "primary")
        self.disable_button = QtWidgets.QPushButton("停用")
        self.uninstall_button = QtWidgets.QPushButton("卸载")
        self.uninstall_button.setProperty("btnRole", "danger")
        self.cleanup_legacy_button = QtWidgets.QPushButton("清理旧版")
        self.cleanup_legacy_button.setProperty("btnRole", "danger")
        self.explain_button = QtWidgets.QPushButton("查看说明")

        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        for button in (
            self.detect_button,
            self.install_button,
            self.disable_button,
            self.uninstall_button,
            self.cleanup_legacy_button,
            self.explain_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(self.title_label)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.catalog_label)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self.detect_button.clicked.connect(self._detect_status)
        self.install_button.clicked.connect(self._install)
        self.disable_button.clicked.connect(self._disable)
        self.uninstall_button.clicked.connect(self._uninstall)
        self.cleanup_legacy_button.clicked.connect(self._cleanup_legacy)
        self.explain_button.clicked.connect(self._show_explanation)
        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(self.ssh_client.is_connected())

    @staticmethod
    def _package_display_text(package):
        channel_names = {"stable": "正式版", "beta": "测试版"}
        verification = "已实机验证" if package.device_verified else "离线验证，尚待实机"
        return (
            f"{package.release_version} | {channel_names[package.channel]} | "
            f"硬件 {package.platform.title()} | 内部版本 {package.firmware} | "
            f"{verification}"
        )

    def _on_connection_changed(self, connected: bool):
        if not connected:
            self._status = None
            self.catalog_label.setText(
                f"当前固件{self.feature_name}包：检测后显示"
            )
            self.status_label.setText("设备未连接")
        elif self._status is None:
            self.status_label.setText("设备已连接，尚未检测")
        self._update_buttons()

    def _update_buttons(self):
        connected = self.ssh_client.is_connected() and not self._busy
        state = self._status.state if self._status else None
        install_states = (
            self.state_type.NOT_INSTALLED,
            self.state_type.INSTALLED_DISABLED,
            self.state_type.MIGRATION_AVAILABLE,
            self.state_type.REPAIR_AVAILABLE,
        )
        states = self.state_type
        if state is states.MIGRATION_AVAILABLE:
            self.install_button.setText(f"迁移到{self.feature_name}")
        elif state is states.REPAIR_AVAILABLE:
            self.install_button.setText("修复并更新")
        elif state is states.INSTALLED_DISABLED:
            self.install_button.setText("重新启用")
        else:
            self.install_button.setText(f"安装{self.feature_name}")
        self.detect_button.setEnabled(connected)
        self.install_button.setEnabled(
            connected
            and self._status is not None
            and self._status.package is not None
            and getattr(self._status, "prerequisite_available", True)
            and state in install_states
        )
        self.disable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.recovery_available
            and state
            not in (
                states.NOT_INSTALLED,
                states.INCOMPATIBLE,
                states.MIGRATION_AVAILABLE,
                states.INSTALLED_DISABLED,
                states.BROKEN,
            )
        )
        self.cleanup_legacy_button.setEnabled(
            connected
            and self._status is not None
            and self._status.cleanup_available
            and state
            in (
                states.MIGRATION_AVAILABLE,
                states.REPAIR_AVAILABLE,
            )
        )
        uninstall_states = (
            states.REPAIR_AVAILABLE,
            states.INSTALLED_DISABLED,
            states.ENABLE_PENDING_REBOOT,
            states.ENABLED,
            states.DISABLE_PENDING_REBOOT,
        )
        if self.backend is _weread_launcher:
            uninstall_states += (states.MIGRATION_AVAILABLE,)
        self.uninstall_button.setEnabled(
            connected
            and self._status is not None
            and state in uninstall_states
        )
        self.explain_button.setEnabled(True)

    def _apply_status(self, status):
        self._status = status
        if status.package is None:
            self.catalog_label.setText(
                f"当前固件{self.feature_name}包：没有精确匹配版本"
            )
        else:
            self.catalog_label.setText(
                f"当前固件{self.feature_name}包：\n"
                + self._package_display_text(status.package)
            )

        states = type(status.state)
        messages = {
            states.INCOMPATIBLE: f"当前设备没有精确匹配的{self.feature_name}包",
            states.NOT_INSTALLED: f"尚未安装{self.feature_name}",
            states.MIGRATION_AVAILABLE: f"检测到旧版{self.feature_name}，可迁移",
            states.REPAIR_AVAILABLE: "检测到已知缺陷版本，可安全修复",
            states.INSTALLED_DISABLED: f"{self.feature_name}已安装，当前未启用",
            states.ENABLE_PENDING_REBOOT: f"{self.feature_name}已部署，等待手动重启后生效",
            states.ENABLED: f"{self.feature_name}已启用",
            states.DISABLE_PENDING_REBOOT: f"{self.feature_name}已停用，等待手动重启后完成",
            states.BROKEN: f"检测到不完整或不可验证的{self.feature_name}状态",
        }
        message = messages[status.state]
        if status.detail:
            message += f"：{status.detail}"
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
        if self._busy:
            if on_done is not None:
                on_done()
            return
        ssh_client = self.ssh_client
        feature_id = self.backend.FEATURE_ID
        self._set_busy(True, pending)
        worker = _rmtool.Worker(fn, *args)

        def on_finished(status):
            if sip.isdeleted(self):
                if close_connection:
                    ssh_client.close()
                return
            self._set_busy(False)
            self._apply_status(status)
            if close_connection:
                self.ssh_client.close()
            if success:
                _ui.show_info(self, _rmtool.APP_NAME, success)
            if on_done is not None:
                on_done()

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                if close_connection:
                    ssh_client.close()
                logging.error(
                    "%s operation failed after tab close: %s",
                    feature_id,
                    exc,
                )
                return
            if isinstance(exc, _package_download.PackageDownloadError):
                # Download failed before any device change; keep the session.
                self._set_busy(False, "资源包下载失败，可手动加载后重试")
                logging.error(
                    "%s package download failed: %s", feature_id, exc
                )
                if show_errors:
                    _ui._show_package_download_error(self, exc, retry=self._install)
                if on_done is not None:
                    on_done()
                return
            if close_connection:
                self.ssh_client.close()
            self._set_busy(False, "操作失败，设备不会被自动重启；请检查日志后重试")
            logging.error("%s operation failed: %s", feature_id, exc)
            if show_errors:
                _ui.show_error(
                    self,
                    _rmtool.APP_NAME,
                    f"操作失败：{exc}\n设备不会被自动重启。",
                )
            if on_done is not None:
                on_done()

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _detect_status(self):
        self._start_status_detection()

    def _start_status_detection(self, *, on_done=None, show_errors: bool = True):
        self._start_worker(
            self.status_loader,
            self.ssh_client,
            str(_rmtool.app_state_dir()),
            pending=f"正在获取清单并核对{self.feature_name}包与设备状态…",
            on_done=on_done,
            show_errors=show_errors,
        )

    @require_connection
    def _install(self):
        if not self._status or not self._status.package:
            return
        migration = self._status.state is self.state_type.MIGRATION_AVAILABLE
        repair = self._status.state is self.state_type.REPAIR_AVAILABLE
        action = (
            f"迁移已验证的旧版{self.feature_name}"
            if migration
            else f"修复并更新已验证的{self.feature_name}"
            if repair
            else f"安装当前固件对应的{self.feature_name}"
        )
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"将{action}，完成后 SSH 会话会关闭。"
            f"本次不会自动重启设备；请手动重启后前往“设置 > {self.device_page_name}”。"
            + ("迁移不会沿用旧版开关状态。" if migration else "")
            + "是否继续？",
            confirm_text=(
                f"迁移到{self.feature_name}"
                if migration
                else "修复并更新"
                if repair
                else f"安装{self.feature_name}"
            ),
            cancel_text="取消",
        ):
            return
        self._start_worker(
            self.installer,
            self.ssh_client,
            self._status.package,
            str(_rmtool.app_state_dir()),
            migration,
            pending=(
                f"正在验证旧版并迁移到{self.feature_name}…"
                if migration
                else "正在校验并原子替换缺陷包…"
                if repair
                else f"正在下载、校验并部署{self.feature_name}…"
            ),
            success=(
                f"{self.feature_name}已迁移并通过校验，SSH 会话已关闭。\n"
                if migration
                else f"{self.feature_name}已修复并通过校验，SSH 会话已关闭。\n"
                if repair
                else f"{self.feature_name}已部署并通过校验，SSH 会话已关闭。\n"
            )
            + f"请手动重启设备，然后前往“设置 > {self.device_page_name}”。",
            close_connection=True,
        )

    @require_connection
    def _disable(self):
        if not self._status or not self._status.available_packages:
            return
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"将停用{self.feature_name}并保留其他已验证的 rmtool 功能。"
            "本次不会自动重启设备，完成后请手动重启使修改生效。是否继续？",
            confirm_text=f"停用{self.feature_name}",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            self.backend.disable,
            self.ssh_client,
            self._status.available_packages,
            pending=f"正在安全停用{self.feature_name}并保留其他功能…",
            success=f"{self.feature_name}已停用，SSH 会话已关闭。\n请手动重启设备使修改生效。",
            close_connection=True,
        )

    @require_connection
    def _cleanup_legacy(self):
        if not self._status or not self._status.cleanup_available:
            return
        state = self._status.state
        if state not in (
            self.state_type.MIGRATION_AVAILABLE,
            self.state_type.REPAIR_AVAILABLE,
        ):
            return
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"将清理已逐文件验证的旧版{self.feature_name}，并保留其他已验证的 rmtool 功能。"
            "清理完成后 SSH 会话会关闭；请重新连接设备，再进行全新安装。"
            "本次不会自动重启设备。是否继续？",
            confirm_text="清理旧版",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            self.cleaner,
            self.ssh_client,
            str(_rmtool.app_state_dir()),
            pending=f"正在再次验证并原子清理旧版{self.feature_name}…",
            success=(
                f"已清理旧版{self.feature_name}，SSH 会话已关闭。\n"
                f"请重新连接设备后，再进行全新安装当前固件对应的{self.feature_name}。"
            ),
            close_connection=True,
        )

    @require_connection
    def _uninstall(self):
        if not self._status or self._status.package is None:
            return
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"将卸载{self.feature_name}及其 rmtool 管理的文件；其他插件和设备设置会保留。"
            "本次不会自动重启设备，完成后请手动重启。是否继续？",
            confirm_text=f"卸载{self.feature_name}",
            cancel_text="取消",
            danger=True,
        ):
            return
        self._start_worker(
            self.backend.uninstall,
            self.ssh_client,
            self._status.available_packages,
            pending=f"正在验证并卸载{self.feature_name}…",
            success=f"{self.feature_name}已卸载，SSH 会话已关闭。\n请手动重启设备。",
            close_connection=True,
        )

    def _show_explanation(self):
        _ui.show_info(
            self,
            self.feature_name,
            self.explanation,
        )


def _qr_upload_status(ssh_client, state_dir):
    return _qr_upload.get_status(ssh_client, _qr_upload.load_catalog(state_dir, refresh=False))


def _install_qr_upload(ssh_client, package, state_dir, migrate):
    archive = _qr_upload.download_package(package, state_dir)
    return _qr_upload.install(ssh_client, package, archive)


class QrUploadSection(ReadingEnhancementsSection):
    def __init__(self, ssh_client, parent=None):
        super().__init__(ssh_client, parent, backend=_qr_upload,
            state_type=_qr_upload.QrUploadState, status_loader=_qr_upload_status,
            installer=_install_qr_upload, cleaner=_qr_upload.cleanup_legacy,
            feature_name="扫码传输",
            description="同一可信 Wi-Fi 下接收手机发送的 PDF / EPUB，单个文件最多 200 MB。"
                "传输未加密，更大文件推荐使用 USB。支持五款设备的 3.29.0.153，以及 Paper Pro Move 3.28.0.172。",
            explanation="安装后手动重启设备，在“设置 > 扫码传输”开启接收。手机与设备须连接同一可信 Wi-Fi；"
                "接收时不需要电脑、rmtool 或 USB 连接。停止接收或退出页面后链接失效。"
                "RM1 / RM2 未安装中文字体时，请先通过“原生简体中文”准备内置兜底字体，或设置自定义中文系统字体。"
                "停用、更新或卸载前，请先在设备上停止接收并退出；已导入的书籍不会被删除。")
        self.cleanup_legacy_button.hide()


class NoteEnhancementsSection(ReadingEnhancementsSection):
    """Independent browser entry for color-device note enhancements."""

    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(
            ssh_client,
            parent,
            backend=_note_enhancements,
            state_type=_note_enhancements.NoteEnhancementsState,
            status_loader=_note_enhancements_status,
            installer=_install_note_enhancements,
            cleaner=_cleanup_note_enhancements,
            feature_name="笔记增强",
            description=(
                "为 Paper Pro 和 Move 的彩色笔记提供延迟刷新控制，减少提笔后的频繁闪烁。"
                "设备设置提供全局默认，每本笔记也可单独调整。"
            ),
            explanation=(
                "笔记增强提供延迟彩色刷新：可选择停止书写 5、10 或 30 秒后完成"
                "彩色沉淀刷新。“设置 > 笔记增强”保存全局授权和默认值；"
                "每本笔记的设置菜单可单独覆盖开关和等待时间。关闭总开关或功能开关"
                "会恢复系统原生 1 秒刷新；安装完成后需手动重启设备。"
            ),
        )


class WeReadLauncherSection(ReadingEnhancementsSection):
    """Official WeRead application plus the exact-firmware device entry."""

    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(
            ssh_client,
            parent,
            backend=_weread_launcher,
            state_type=_weread_launcher.WeReadLauncherState,
            status_loader=_weread_launcher_status,
            installer=_install_weread_launcher,
            cleaner=_cleanup_weread_launcher,
            feature_name="设备启动入口",
            device_page_name="微信读书",
            description=(
                "微信读书 App 支持 Paper Pro 和 Move 的 3.28 系列固件；"
                "设备启动入口目前仅适配 3.28.0.172。"
                "先安装官方 App，再安装精确匹配的设备启动入口。"
            ),
            explanation=(
                "微信读书 App 直接从腾讯微信读书官方 CDN 下载 v1.0.0；"
                "rmtool 校验外层 ZIP 和内层安装包，重装或修复会保留登录、下载和阅读数据。"
                "设备启动入口提供普通、彩色快刷和黑白快刷。安装入口后请手动重启设备，"
                "再从“设置 > 微信读书”启动官方应用。"
                "两种快刷模式均保持手写笔最低延迟；彩色快刷保留低质量彩色并提升响应，"
                "黑白快刷减少彩色残影。"
            ),
        )
        self.title_label.setText("微信读书")
        self.app_status = None

        app_title = QtWidgets.QLabel("微信读书 App")
        app_title.setObjectName("toolboxFeatureTitle")
        self.app_status_label = ToolboxStatusLabel("设备已连接，尚未检测")
        self.app_status_label.setObjectName("wereadAppStatus")
        self.app_status_label.setWordWrap(True)
        self.app_detect_button = QtWidgets.QPushButton("检测 App")
        self.app_install_button = QtWidgets.QPushButton("安装微信读书 App")
        self.app_install_button.setProperty("btnRole", "primary")
        app_buttons = QtWidgets.QHBoxLayout()
        app_buttons.setContentsMargins(0, 0, 0, 0)
        app_buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        app_buttons.addWidget(self.app_detect_button)
        app_buttons.addWidget(self.app_install_button)
        app_buttons.addStretch()
        app_buttons_widget = QtWidgets.QWidget()
        app_buttons_widget.setLayout(app_buttons)

        launcher_title = QtWidgets.QLabel("设备启动入口")
        launcher_title.setObjectName("toolboxFeatureTitle")
        layout = self.layout()
        layout.insertWidget(2, app_title)
        layout.insertWidget(3, self.app_status_label)
        layout.insertWidget(4, app_buttons_widget)
        layout.insertWidget(5, launcher_title)
        self.detect_button.setText("检测启动入口")

        self.app_detect_button.clicked.connect(self._detect_status)
        self.app_install_button.clicked.connect(self._install_app)
        self._update_buttons()

    def _on_connection_changed(self, connected: bool):
        super()._on_connection_changed(connected)
        if hasattr(self, "app_status_label"):
            if not connected:
                self.app_status = None
                self.app_status_label.setText("设备未连接")
            elif self.app_status is None:
                self.app_status_label.setText("设备已连接，尚未检测")
            self._update_buttons()

    def _update_buttons(self):
        super()._update_buttons()
        if not hasattr(self, "app_install_button"):
            return
        connected = self.ssh_client.is_connected() and not self._busy
        state = self.app_status.state if self.app_status else None
        installable = state in (
            _weread_app.WeReadAppState.NOT_INSTALLED,
            _weread_app.WeReadAppState.REPAIR_AVAILABLE,
        )
        self.app_detect_button.setEnabled(connected)
        self.app_install_button.setEnabled(connected and installable)
        self.install_button.setEnabled(
            self.install_button.isEnabled()
            and state is _weread_app.WeReadAppState.INSTALLED
        )
        self.app_install_button.setText(
            "修复微信读书 App"
            if state is _weread_app.WeReadAppState.REPAIR_AVAILABLE
            else "安装微信读书 App"
        )

    def _apply_app_status(self, status):
        self.app_status = status
        messages = {
            _weread_app.WeReadAppState.INCOMPATIBLE: "当前设备不支持安装",
            _weread_app.WeReadAppState.NOT_INSTALLED: "尚未安装官方微信读书 App",
            _weread_app.WeReadAppState.REPAIR_AVAILABLE: "官方 App 可修复",
            _weread_app.WeReadAppState.INSTALLED: "官方微信读书 App 已安装并通过校验",
            _weread_app.WeReadAppState.BROKEN: "无法验证官方微信读书 App 状态",
        }
        message = messages[status.state]
        if status.detail:
            message += f"：{status.detail}"
        self.app_status_label.setText(message)

    def _start_status_detection(self, *, on_done=None, show_errors: bool = True):
        self._start_combined_worker(
            _weread_combined_status,
            self.ssh_client,
            str(_rmtool.app_state_dir()),
            pending="正在检测微信读书 App 与设备启动入口…",
            on_done=on_done,
            show_errors=show_errors,
        )

    def _start_combined_worker(
        self, fn, *args, pending: str, success: str = "", on_done=None,
        show_errors: bool = True,
    ):
        if self._busy:
            if on_done is not None:
                on_done()
            return
        self._set_busy(True, pending)
        self.app_status_label.setText(pending)
        worker = _rmtool.Worker(fn, *args)

        def on_finished(result):
            if sip.isdeleted(self):
                return
            app_status, launcher_status = result
            self._set_busy(False)
            self._apply_app_status(app_status)
            self._apply_status(launcher_status)
            if success:
                _ui.show_info(self, _rmtool.APP_NAME, success)
            if on_done is not None:
                on_done()

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error("WeRead operation failed after tab close: %s", exc)
                return
            self._set_busy(False, "操作失败，请检查日志后重试")
            self.app_status_label.setText("操作失败，请检查日志后重试")
            if isinstance(exc, _package_download.PackageDownloadError):
                if show_errors:
                    _ui._show_package_download_error(self, exc, retry=self._install_app)
            elif show_errors:
                _ui.show_error(self, _rmtool.APP_NAME, f"操作失败：{exc}\n设备不会被自动重启。")
            if on_done is not None:
                on_done()

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _install_app(self):
        if not self.app_status or self.app_status.state not in (
            _weread_app.WeReadAppState.NOT_INSTALLED,
            _weread_app.WeReadAppState.REPAIR_AVAILABLE,
        ):
            return
        repairing = self.app_status.state is _weread_app.WeReadAppState.REPAIR_AVAILABLE
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将从腾讯微信读书官方 CDN 下载并严格校验 v1.0.0 安装包，"
            + ("保留用户数据并修复程序文件。" if repairing else "安装到设备。")
            + "仅支持 Paper Pro/Move 的 3.28 系列固件；不会重启 xochitl 或设备。是否继续？",
            confirm_text="修复微信读书 App" if repairing else "安装微信读书 App",
            cancel_text="取消",
        ):
            return
        self._start_combined_worker(
            _install_weread_app_and_status,
            self.ssh_client,
            str(_rmtool.app_state_dir()),
            pending="正在下载、校验并安装官方微信读书 App…",
            success=(
                "微信读书 App 已修复，用户数据已保留。"
                if repairing else "微信读书 App 已安装。"
            ) + "\n若当前固件有匹配的设备启动入口，可继续安装；入口安装后需手动重启设备。",
        )

    @require_connection
    def _uninstall(self):
        if not self._status or self._status.package is None:
            return
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将只卸载 rmtool 管理的微信读书设备启动入口，保留官方微信读书 App、"
            "登录信息、下载内容和阅读数据。其他插件及共享运行时会按需保留。"
            "本次不会自动重启设备，完成后请手动重启。是否继续？",
            confirm_text="卸载启动入口",
            cancel_text="取消",
            danger=True,
        ):
            return
        self._start_worker(
            self.backend.uninstall,
            self.ssh_client,
            self._status.available_packages,
            pending="正在验证并卸载微信读书设备启动入口…",
            success="微信读书设备启动入口已卸载，官方 App 和用户数据已保留。\n"
            "SSH 会话已关闭，请手动重启设备。",
            close_connection=True,
        )
