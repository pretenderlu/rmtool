"""Legacy plugin migration and shared-runtime recovery section."""

import logging
from contextlib import nullcontext

from PyQt5 import QtCore, QtGui, QtWidgets, sip

import _package_download
import _legacy_vellum
import _plugin_recovery
import _residue_migration
import _xovi_standalone
from _ssh import SSHClientWrapper, require_connection
# Dialog helpers are looked up on _toolbox_common at call time so a single
# patch point covers every toolbox section.
import _toolbox_common as _ui
import rmtool as _rmtool  # late-bound access to avoid circular import
from _toolbox_common import ToolboxStatusLabel


_LEGACY_PLATFORM_LABELS = {
    "ferrari": "Paper Pro",
    "chiappa": "Paper Pro Move",
    "tatsu": "Paper Pure",
    "rm1": "reMarkable 1",
    "rm2": "reMarkable 2",
}


class LegacyPluginMigrationSection(QtWidgets.QWidget):
    """Firmware-residue migration/cleanup plus historical Vellum cleanup."""

    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._busy = False
        self._report = None
        self._report_session = None
        self._recovery_report = None
        self._recovery_session = None
        self._incomplete_report = None
        self._incomplete_session = None
        self._connection_generation = 0
        self._connected = None

        migration_title = QtWidgets.QLabel("固件升级插件迁移")
        migration_title.setObjectName("toolboxFeatureTitle")
        self.status_label = ToolboxStatusLabel(
            "固件升级后，已安装的 rmtool 插件会因固件身份变化而停用。"
            "在这里可以先用旧固件清单逐文件验证残留，再把全部已启用功能"
            "一次性迁移到当前固件的精确包，或只清理已验证残留；"
            "迁移完成后需手动重启设备。"
        )
        self.status_label.setWordWrap(True)
        self.detect_button = QtWidgets.QPushButton("检测迁移状态")
        self.detect_button.clicked.connect(self._detect)
        self.recovery_detect_button = QtWidgets.QPushButton("检测恢复状态")
        self.recovery_detect_button.clicked.connect(
            lambda: self._start_status_detection()
        )
        self.repair_button = QtWidgets.QPushButton("修复并重装")
        self.repair_button.clicked.connect(self._repair)
        self.cleanup_incomplete_button = QtWidgets.QPushButton("清理不完整状态")
        self.cleanup_incomplete_button.setProperty("btnRole", "danger")
        self.cleanup_incomplete_button.clicked.connect(self._cleanup_incomplete)
        self.migrate_button = QtWidgets.QPushButton("迁移到当前固件")
        self.migrate_button.clicked.connect(self._migrate)
        self.cleanup_residue_button = QtWidgets.QPushButton("清理固件残留")
        self.cleanup_residue_button.setProperty("btnRole", "danger")
        self.cleanup_residue_button.clicked.connect(self._cleanup_residue)

        vellum_title = QtWidgets.QLabel("旧版 Vellum 插件清理")
        vellum_title.setObjectName("toolboxFeatureTitle")
        self.cleanup_status_label = ToolboxStatusLabel(
            "仅清理 rmtool 历史安装的点击翻页和快速黑白 Vellum 包；"
            "不会卸载 Vellum、AppLoader、Xovi 或其他插件。"
        )
        self.cleanup_status_label.setWordWrap(True)
        self.cleanup_button = QtWidgets.QPushButton("一键卸载旧版插件")
        self.cleanup_button.clicked.connect(self._cleanup)
        self.vellum_help_button = QtWidgets.QPushButton("查看 Vellum 官方卸载说明")
        self.vellum_help_button.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl(_legacy_vellum.VELLUM_UNINSTALL_URL)
            )
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(migration_title)
        layout.addWidget(self.status_label)
        recovery_row = QtWidgets.QHBoxLayout()
        recovery_row.setSpacing(_rmtool.SUBSECTION_GAP)
        recovery_row.addWidget(self.recovery_detect_button)
        recovery_row.addWidget(self.repair_button)
        recovery_row.addWidget(self.cleanup_incomplete_button)
        recovery_row.addStretch(1)
        layout.addLayout(recovery_row)
        detect_row = QtWidgets.QHBoxLayout()
        detect_row.setSpacing(_rmtool.SUBSECTION_GAP)
        detect_row.addWidget(self.detect_button)
        detect_row.addWidget(self.migrate_button)
        detect_row.addWidget(self.cleanup_residue_button)
        detect_row.addStretch(1)
        layout.addLayout(detect_row)
        layout.addWidget(vellum_title)
        layout.addWidget(self.cleanup_status_label)
        cleanup_row = QtWidgets.QHBoxLayout()
        cleanup_row.setSpacing(_rmtool.SUBSECTION_GAP)
        cleanup_row.addWidget(self.cleanup_button)
        cleanup_row.addWidget(self.vellum_help_button)
        cleanup_row.addStretch(1)
        layout.addLayout(cleanup_row)

        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(self.ssh_client.is_connected())

    def _on_connection_changed(self, connected: bool):
        connected = bool(connected)
        if connected != self._connected:
            self._connected = connected
            self._connection_generation += 1
            self._report = None
            self._report_session = None
            self._recovery_report = None
            self._recovery_session = None
            self._incomplete_report = None
            self._incomplete_session = None
            self._busy = False
            self.status_label.setText(
                "设备已连接，尚未检测恢复或迁移状态" if connected else "设备未连接"
            )
        active = connected and not self._busy
        self.detect_button.setEnabled(active)
        self.recovery_detect_button.setEnabled(active)
        self.repair_button.setEnabled(
            active and self._recovery_report is not None
            and self._recovery_report.can_repair
        )
        self.cleanup_incomplete_button.setEnabled(
            active and self._incomplete_report is not None
            and not (
                self._recovery_report is not None
                and getattr(self._recovery_report, "can_repair", False)
            )
            and getattr(self._incomplete_report, "can_cleanup", False)
        )
        self.cleanup_button.setEnabled(active)
        self.migrate_button.setEnabled(
            active and self._report is not None and self._report.migratable
        )
        self.cleanup_residue_button.setEnabled(
            active and self._report is not None and bool(self._report.features)
        )

    def _session_token(self):
        return self._connection_generation, getattr(self.ssh_client, "_client", None)

    def _session_current(self, token):
        generation, client = token
        return (
            generation == self._connection_generation
            and client is getattr(self.ssh_client, "_client", None)
            and self.ssh_client.is_connected()
        )

    def _run_in_session(self, session, fn, *args):
        with self.ssh_client.operation_session():
            if not self._session_current(session):
                raise RuntimeError("设备连接已变化，请重新检测后操作")
            return fn(*args)

    def _close_session(self, session):
        with getattr(self.ssh_client, "_state_lock", nullcontext()):
            if self._session_current(session):
                self.ssh_client.close()

    def _apply_status(self, report, incomplete_report=None):
        self._recovery_report = report
        self._recovery_session = self._session_token()
        allow_incomplete_cleanup = not getattr(report, "can_repair", False)
        self._incomplete_report = incomplete_report if allow_incomplete_cleanup else None
        self._incomplete_session = (
            self._session_token() if self._incomplete_report is not None else None
        )
        labels = {
            _plugin_recovery.RecoveryState.NOT_NEEDED: "无需修复",
            _plugin_recovery.RecoveryState.REPAIR_AVAILABLE: "可修复并重装",
            _plugin_recovery.RecoveryState.CLEANUP_AVAILABLE: "可清理不完整状态",
            _plugin_recovery.RecoveryState.UNSUPPORTED: "当前固件不支持自动修复，请等待精确匹配的资源包",
            _plugin_recovery.RecoveryState.BLOCKED: (
                "无法安全自动修复，请导出诊断日志交由维护者核对；不要手动跳过校验"
            ),
        }
        lines = [labels[report.state]]
        if report.detail:
            lines.append(report.detail)
        if report.features:
            lines.append("涉及共享插件：" + "、".join(report.features))
        for issue in report.issues:
            if issue and issue not in lines:
                lines.append(issue)
        if self._incomplete_report is not None:
            if getattr(self._incomplete_report, "can_cleanup", False):
                lines.append("残缺共享 Xovi：可清理不完整状态")
            elif self._incomplete_report.state == _plugin_recovery.RecoveryState.BLOCKED:
                lines.append(
                    "残缺共享 Xovi 清理已阻止：" + self._incomplete_report.detail
                )
        if report.backup_path:
            lines.append("原安装备份：" + report.backup_path)
        self.status_label.setText("\n".join(line for line in lines if line))
        self._on_connection_changed(self.ssh_client.is_connected())

    def _start_worker(
        self, fn, *args, pending: str, on_done=None, show_errors=True,
        session=None, repairing=False, close_on_success=False, success="",
    ):
        if self._busy or not self.ssh_client.is_connected():
            if on_done is not None:
                on_done()
            return
        session = self._session_token() if session is None else session
        if not self._session_current(session):
            if on_done is not None:
                on_done()
            return
        if repairing:
            self._report = None
            self._incomplete_report = None
            self._incomplete_session = None
        self._busy = True
        self._on_connection_changed(True)
        self.status_label.setText(pending)

        def execute():
            # Check identity after acquiring the transport lock, including queued work.
            return self._run_in_session(session, fn, *args)

        worker = _rmtool.Worker(execute)
        completed = False

        def finish(report=None, exc=None):
            nonlocal completed
            if completed:
                return
            completed = True
            try:
                if sip.isdeleted(self) or not self._session_current(session):
                    return
                self._busy = False
                self._on_connection_changed(True)
                if exc is None:
                    incomplete_report = None
                    if (
                        isinstance(report, tuple)
                        and len(report) == 2
                        and hasattr(report[0], "state")
                        and (report[1] is None or hasattr(report[1], "state"))
                    ):
                        report, incomplete_report = report
                    self._apply_status(report, incomplete_report)
                    if (
                        repairing
                        and report.state == _plugin_recovery.RecoveryState.NOT_NEEDED
                        and not report.can_repair
                        and report.backup_path
                    ):
                        report_details = "\n".join(
                            dict.fromkeys(
                                line
                                for line in (report.detail, *report.issues)
                                if line
                            )
                        )
                        message = (
                            "修复并重装完成，尚未重启设备。请先按下方保护状态说明处理，"
                            "再从设备菜单手动重启。若原有紧急停用仍保留，请重新连接，"
                            "在“原生简体中文”中检测状态并使用“清除紧急停用”；"
                            "校验未通过时不要解除保护。\n"
                            + report_details
                            + f"\n原安装备份保留在：{report.backup_path}"
                        )
                        # Close only the originating client, before opening another dialog.
                        self._close_session(session)
                        self.status_label.setText(message)
                        _ui.show_info(self, _rmtool.APP_NAME, message)
                    elif close_on_success:
                        self._close_session(session)
                        self.status_label.setText(success)
                        if success:
                            _ui.show_info(self, _rmtool.APP_NAME, success)
                elif isinstance(exc, _package_download.PackageDownloadError):
                    self.status_label.setText("资源包下载失败，可手动加载后重试")

                    def retry():
                        if not sip.isdeleted(self) and self._session_current(session):
                            self._repair()

                    if show_errors:
                        _ui._show_package_download_error(self, exc, retry=retry)
                else:
                    self._recovery_report = None
                    self._incomplete_report = None
                    self._incomplete_session = None
                    self._on_connection_changed(True)
                    guidance = (
                        "未自动重启设备。恢复的旧程序不代表已通过校验；请勿重启或解除紧急停用，"
                        "先重新检测并保留备份，必要时导出诊断日志。"
                        if repairing else "未自动重启设备，请重新检测；必要时导出诊断日志。"
                    )
                    message = f"恢复操作未完成：{exc}\n{guidance}"
                    self.status_label.setText(message)
                    logging.error("Plugin recovery failed: %s", exc)
                    if show_errors:
                        _ui.show_error(self, _rmtool.APP_NAME, message)
            finally:
                if on_done is not None:
                    on_done()

        worker.signals.finished.connect(lambda report: finish(report=report))
        worker.signals.error.connect(lambda exc: finish(exc=exc))
        try:
            self.thread_pool.start(worker)
        except Exception as exc:
            finish(exc=exc)

    def _start_status_detection(self, *, on_done=None, show_errors=True):
        def inspect():
            report = _plugin_recovery.inspect_recovery(self.ssh_client)
            incomplete = None
            if not getattr(report, "can_repair", False):
                incomplete = _plugin_recovery.inspect_incomplete(self.ssh_client)
            return report, incomplete

        self._start_worker(
            inspect,
            pending="正在只读检测共享插件恢复状态…",
            on_done=on_done, show_errors=show_errors,
        )

    @require_connection
    def _repair(self):
        report = self._recovery_report
        if self._busy or report is None or not report.can_repair:
            return
        session = self._recovery_session
        if session is None or not self._session_current(session):
            return
        if not _ui.ask_confirmation(
            self, "修复并重装",
            "rmtool 将重新核对安装归属与当前固件。若插件文件完整且与目标版本一致，"
            "只备份并修复启动配置；否则下载并校验所需资源包，隔离原安装后用受信资源包重建损坏或过期的程序。"
            "这会影响同一共享运行时中全部已启用的插件，并保留各插件启用/停用状态。\n"
            "已完整校验且兼容的拼音配套服务将保留原文件，不重复安装。\n"
            f"涉及插件：{'、'.join(report.features) or '以重新检测结果为准'}\n"
            "原安装备份将保留，现有设置、字体、书籍等用户数据不会删除。"
            "修复期间会开启紧急停用保护；完整校验成功后仅解除本次新增的保护，"
            "原有保护保持不变，失败时保留保护。不会自动启动插件或重启设备。"
            "完成后请按结果说明处理原有保护，再从设备菜单手动重启。"
            f"重启后请等待至少 {_xovi_standalone.SHARED_STARTUP_STABLE_SECONDS} 秒再检测，期间不要再次重启或启动其他应用。是否继续？",
            confirm_text="修复并重装", cancel_text="取消",
            danger=True,
        ):
            return
        if (
            sip.isdeleted(self) or not self._session_current(session)
            or report is not self._recovery_report
        ):
            return
        self._start_worker(
            _plugin_recovery.repair, self.ssh_client, _rmtool.app_state_dir(),
            pending="正在校验资源包并修复共享插件…", session=session, repairing=True,
        )

    @require_connection
    def _cleanup_incomplete(self):
        report = self._incomplete_report
        session = self._incomplete_session
        if self._busy or report is None or not getattr(report, "can_cleanup", False):
            return
        if session is None or not self._session_current(session):
            return
        if not _ui.ask_confirmation(
            self,
            "清理不完整共享 Xovi",
            "rmtool 会在删除前再次只读核对固定路径、root 权限、文件指纹、挂载边界和外部启动配置。"
            "只有能确认归属于 rmtool 的残缺共享状态才会清理；未知文件、符号链接、权限异常、"
            "Vellum/AppLoad/Xovi、未知 drop-in 或双布局都会拒绝操作。"
            "清理只删除 rmtool 共享基座、受校验的 drop-in 和紧急停用标记，不会删除微信读书、"
            "KOReader、字体、书籍或其他 /data/rmtool 内容。不会自动重启设备，是否继续？",
            confirm_text="清理不完整状态",
            cancel_text="取消",
            danger=True,
        ):
            return
        if (
            sip.isdeleted(self)
            or not self._session_current(session)
            or self._incomplete_report is not report
            or self._busy
        ):
            return
        self._recovery_report = None
        self._recovery_session = None
        self._start_worker(
            _plugin_recovery.cleanup_incomplete,
            self.ssh_client,
            pending="正在重新验证并清理不完整共享 Xovi 状态…",
            session=session,
            close_on_success=True,
            success=(
                "不完整共享 Xovi 已清理，插件状态已恢复为未安装。SSH 会话已关闭；"
                "请手动重启设备后重新安装当前固件对应的插件。"
            ),
        )

    def _report_text(self, report) -> str:
        if report is None:
            return "未检测到固件升级残留；如需清理历史 Vellum 包请使用下方按钮。"
        lines = [
            "残留固件：{}（{}）".format(
                report.old_identity.firmware,
                _LEGACY_PLATFORM_LABELS.get(
                    report.old_identity.platform, report.old_identity.platform
                ),
            ),
            "当前固件：{}（{}）".format(
                report.new_identity.firmware,
                _LEGACY_PLATFORM_LABELS.get(
                    report.new_identity.platform, report.new_identity.platform
                ),
            ),
        ]
        for item in report.features:
            state = "已启用" if item.enabled else "未启用"
            target = "可迁移" if item.target_available else "当前固件无精确包"
            lines.append(f"{item.label}：{state}，{target}")
        for blocker in report.blockers:
            lines.append(f"阻断：{blocker}")
        lines.append(report.detail)
        return "\n".join(lines)

    @require_connection
    def _detect(self):
        if self._busy:
            return
        session = self._session_token()
        self._report = None
        self._report_session = None
        self._recovery_report = None
        self._recovery_session = None
        self._incomplete_report = None
        self._incomplete_session = None
        self._busy = True
        self._on_connection_changed(True)
        self.status_label.setText("正在验证共享 Xovi 固件残留…")
        worker = _rmtool.Worker(
            self._run_in_session, session, _residue_migration.inspect_residue, self.ssh_client
        )

        def on_finished(report):
            if sip.isdeleted(self) or not self._session_current(session):
                return
            self._busy = False
            self._report = report
            self._report_session = session
            self._on_connection_changed(self.ssh_client.is_connected())
            self.status_label.setText(self._report_text(report))

        def on_error(exc: Exception):
            if sip.isdeleted(self) or not self._session_current(session):
                logging.error("Residue detection failed after tab close: %s", exc)
                return
            self._busy = False
            self._on_connection_changed(self.ssh_client.is_connected())
            self.status_label.setText(f"检测迁移状态失败：{exc}")
            logging.error("Residue detection failed: %s", exc)
            _ui.show_error(self, _rmtool.APP_NAME, f"检测失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _migrate(self):
        if self._busy or self._report is None or not self._report.migratable:
            return
        report = self._report
        session = self._report_session
        if session is None or not self._session_current(session):
            return
        features = "、".join(
            item.label for item in report.features if item.enabled and item.target_available
        )
        skipped = "、".join(
            item.label for item in report.features if item.enabled and not item.target_available
        )
        migration_notice = (
            f"再把兼容的已启用功能（{features}）一次性替换为当前固件的精确包，"
            "并保持这些功能的启用状态。" if features else "当前没有可迁移的已启用功能。"
        )
        skip_notice = (
            f"将跳过不兼容入口（{skipped}），保留对应 App 和数据。" if skipped else ""
        )
        if not _ui.ask_confirmation(
            self,
            "迁移到当前固件",
            "rmtool 会先用旧固件的受信清单逐文件验证共享 Xovi 残留，"
            + migration_notice + skip_notice + "任一验证失败都不会写入。"
            + "迁移完成后请从设备菜单手动重启设备。是否继续？",
            confirm_text="迁移插件",
            cancel_text="取消",
        ):
            return

        if (sip.isdeleted(self) or not self._session_current(session)
                or self._busy or report is not self._report):
            return
        self._recovery_report = None
        self._recovery_session = None
        self._incomplete_report = None
        self._incomplete_session = None
        self._busy = True
        self._on_connection_changed(True)
        self.status_label.setText("正在验证并迁移共享 Xovi 插件…")
        worker = _rmtool.Worker(
            self._run_in_session, session,
            _residue_migration.migrate, self.ssh_client, _rmtool.app_state_dir()
        )

        def on_finished(report):
            if sip.isdeleted(self) or not self._session_current(session):
                return
            self._busy = False
            self._report = None
            self._on_connection_changed(self.ssh_client.is_connected())
            skipped = "、".join(
                item.label for item in report.features
                if item.enabled and not item.target_available
            )
            message = "迁移处理完成。"
            if skipped:
                message += f"已跳过不兼容入口（{skipped}），对应 App 和数据已保留。"
            message += "请从设备菜单手动重启设备后生效。"
            self.status_label.setText(message)
            _ui.show_info(
                self,
                _rmtool.APP_NAME,
                message,
            )

        def on_error(exc: Exception):
            if sip.isdeleted(self) or not self._session_current(session):
                logging.error("Residue migration failed after tab close: %s", exc)
                return
            self._busy = False
            self._on_connection_changed(self.ssh_client.is_connected())
            if isinstance(exc, _package_download.PackageDownloadError):
                # Download failed before any device change; keep the session.
                self.status_label.setText("资源包下载失败，可手动加载后重试")
                logging.error("Residue migration package download failed: %s", exc)
                def retry():
                    if not sip.isdeleted(self) and self._session_current(session):
                        self._migrate()

                _ui._show_package_download_error(self, exc, retry=retry)
                return
            self.status_label.setText(f"插件迁移失败：{exc}")
            logging.error("Residue migration failed: %s", exc)
            _ui.show_error(self, _rmtool.APP_NAME, f"迁移失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _cleanup_residue(self):
        if self._busy or self._report is None or not self._report.features:
            return
        report = self._report
        session = self._report_session
        if session is None or not self._session_current(session):
            return
        features = "、".join(item.label for item in report.features)
        if not _ui.ask_confirmation(
            self,
            "清理固件升级残留",
            "rmtool 会再次使用旧固件的受信清单逐文件验证残留，然后删除整套旧共享 "
            f"Xovi 状态（{features}）。不会在当前固件重建功能，也不会自动重启设备。"
            "任一验证失败都不会清理。是否继续？",
            confirm_text="清理残留",
            cancel_text="取消",
            danger=True,
        ):
            return

        if (sip.isdeleted(self) or not self._session_current(session)
                or self._busy or report is not self._report):
            return
        self._recovery_report = None
        self._recovery_session = None
        self._incomplete_report = None
        self._incomplete_session = None
        self._busy = True
        self._on_connection_changed(True)
        self.status_label.setText("正在重新验证并清理共享 Xovi 固件残留…")
        worker = _rmtool.Worker(
            self._run_in_session, session, _residue_migration.cleanup, self.ssh_client
        )

        def on_finished(_report):
            if sip.isdeleted(self) or not self._session_current(session):
                return
            self._busy = False
            self._report = None
            self._on_connection_changed(self.ssh_client.is_connected())
            self._close_session(session)
            self.status_label.setText("固件升级残留已清理。")
            _ui.show_info(
                self,
                _rmtool.APP_NAME,
                "已清理通过旧固件清单验证的共享 Xovi 残留。SSH 会话已关闭；"
                "重新连接后可安装当前固件支持的功能。",
            )

        def on_error(exc: Exception):
            if sip.isdeleted(self) or not self._session_current(session):
                logging.error("Firmware residue cleanup failed after tab close: %s", exc)
                return
            self._busy = False
            self._on_connection_changed(self.ssh_client.is_connected())
            self.status_label.setText(f"固件残留清理失败：{exc}")
            logging.error("Firmware residue cleanup failed: %s", exc)
            _ui.show_error(self, _rmtool.APP_NAME, f"清理失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _cleanup(self):
        if self._busy:
            return
        session = self._session_token()
        if not _ui.ask_confirmation(
            self,
            "一键卸载旧版插件",
            "rmtool 会先完整验证检测到的两个历史 Vellum 功能包，全部通过后才开始删除。"
            "任一包的标记、版本、所有权或文件哈希异常时，将不会删除任何包。"
            "Vellum、AppLoader、Xovi、当前 rmtool 共享功能和第三方插件都不会被卸载。"
            "是否继续？",
            confirm_text="卸载旧版插件",
            cancel_text="取消",
            danger=True,
        ):
            return

        if sip.isdeleted(self) or not self._session_current(session) or self._busy:
            return
        self._recovery_report = None
        self._recovery_session = None
        self._incomplete_report = None
        self._incomplete_session = None
        self._busy = True
        self._on_connection_changed(True)
        self.cleanup_status_label.setText("正在验证并卸载 rmtool 历史 Vellum 功能包…")
        worker = _rmtool.Worker(
            self._run_in_session, session, _legacy_vellum.remove_legacy_plugins, self.ssh_client
        )

        def on_finished(removed: tuple[str, ...]):
            if sip.isdeleted(self) or not self._session_current(session):
                return
            self._busy = False
            self._on_connection_changed(self.ssh_client.is_connected())
            if removed:
                names = "、".join(removed)
                self.cleanup_status_label.setText(f"已卸载：{names}")
                _ui.show_info(
                    self,
                    _rmtool.APP_NAME,
                    "已卸载通过验证的 rmtool 旧版插件。Vellum/AppLoader/Xovi 本体仍保留。",
                )
            else:
                self.cleanup_status_label.setText("未检测到 rmtool 安装的旧版 Vellum 插件")
                _ui.show_info(self, _rmtool.APP_NAME, "没有需要卸载的 rmtool 旧版插件。")

        def on_error(exc: Exception):
            if sip.isdeleted(self) or not self._session_current(session):
                logging.error("Legacy Vellum cleanup failed after tab close: %s", exc)
                return
            self._busy = False
            self._on_connection_changed(self.ssh_client.is_connected())
            self.cleanup_status_label.setText(f"旧版插件清理失败：{exc}")
            logging.error("Legacy Vellum cleanup failed: %s", exc)
            _ui.show_error(self, _rmtool.APP_NAME, f"操作失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)
