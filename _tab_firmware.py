"""Native firmware page; all device work stays in the existing worker pool."""
from pathlib import Path

from PyQt5 import QtCore, QtWidgets, sip

import _firmware as firmware
import _residue_migration as residue_migration
from _dialogs import ask_confirmation, show_error
import rmtool as _rmtool


class FirmwareTab(QtWidgets.QWidget):
    def __init__(self, ssh_client, config=None, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.image = None
        self.state = None
        self.standby_slot = None
        self.standby_error = ""
        self.transaction = ("none", "尚未检测")
        self.restore_report = None
        self.busy = False
        self.worker = None
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        content = QtWidgets.QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        root = QtWidgets.QVBoxLayout(content)
        root.setContentsMargins(24, 24, 24, 24)
        title = QtWidgets.QLabel("固件管理")
        title.setObjectName("toolboxBrowserTitle")
        root.addWidget(title)
        self.status = QtWidgets.QLabel("未连接")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        root.addWidget(self.status)

        self.partition_grid = QtWidgets.QGridLayout()
        self.partition_grid.setContentsMargins(0, 8, 0, 12)
        self.partition_grid.setSpacing(12)
        self.partition_cards = {
            slot: self._make_partition_card(slot) for slot in ("a", "b")
        }
        self._partitions_stacked = None
        root.addLayout(self.partition_grid)
        self._arrange_partition_cards(False)

        install_title = QtWidgets.QLabel("安装固件到备用分区")
        install_title.setObjectName("firmwareSectionTitle")
        root.addWidget(install_title)
        install_hint = QtWidgets.QLabel("新固件写入备用分区，安装完成后由你确认重启切换。")
        install_hint.setObjectName("firmwareSectionHint")
        install_hint.setWordWrap(True)
        root.addWidget(install_hint)
        self.platform = QtWidgets.QComboBox()
        self.platform.addItem("Paper Pro", "ferrari")
        self.platform.addItem("Paper Pro Move", "chiappa")
        self.releases = QtWidgets.QComboBox()
        self.releases.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.releases.setMinimumContentsLength(16)
        form = QtWidgets.QFormLayout()
        form.addRow("设备型号", self.platform)
        form.addRow("固件版本", self.releases)
        root.addLayout(form)
        self.selected = QtWidgets.QLabel("尚未选择 SWU")
        self.selected.setWordWrap(True)
        root.addWidget(self.selected)
        actions = QtWidgets.QGridLayout()
        self.buttons = {}
        for index, (key, label, callback) in enumerate((
            ("list", "获取官方列表", self.load_releases),
            ("download", "下载所选固件", self.download),
            ("refresh", "刷新设备状态", self.refresh),
            ("install", "安装所选固件", self.install),
            ("reboot", "确认重启", self.reboot),
            ("restore_plugins", "恢复更新前插件", self.restore_plugins),
        )):
            button = QtWidgets.QPushButton(label)
            if key == "install":
                button.setProperty("btnRole", "primary")
            button.clicked.connect(callback)
            self.buttons[key] = button
            if key in ("reboot", "restore_plugins"):
                actions.addWidget(button, 2, 0, 1, 2)
            else:
                actions.addWidget(button, index // 2, index % 2)
        root.addLayout(actions)
        self.advanced_toggle = QtWidgets.QToolButton()
        self.advanced_toggle.setText("高级选项")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(QtCore.Qt.RightArrow)
        root.addWidget(self.advanced_toggle, 0, QtCore.Qt.AlignLeft)
        self.advanced = QtWidgets.QWidget()
        advanced_layout = QtWidgets.QGridLayout(self.advanced)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        self.advanced_status = QtWidgets.QLabel("连接设备并刷新后显示分区信息")
        self.advanced_status.setWordWrap(True)
        advanced_layout.addWidget(self.advanced_status, 0, 0, 1, 2)
        for index, (key, label, callback) in enumerate((
            ("local", "选择本地 SWU", self.choose_local),
            ("switch", "下次重启切换到备用分区", self.switch),
            ("restore", "恢复自动更新", self.restore),
        )):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(callback)
            self.buttons[key] = button
            advanced_layout.addWidget(button, 1 + index // 2, index % 2)
        self.advanced.setVisible(False)
        root.addWidget(self.advanced)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        root.addWidget(self.progress)
        root.addStretch()
        self.platform.currentIndexChanged.connect(self.releases.clear)
        self.releases.currentIndexChanged.connect(self._update)
        ssh_client.connection_changed.connect(self.connection_changed)
        self._update()

    def _make_partition_card(self, slot):
        frame = QtWidgets.QFrame()
        frame.setObjectName("firmwarePartitionCard")
        frame.setProperty("active", False)
        frame.setMinimumHeight(126)
        frame.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(5)
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel(f"分区 {slot.upper()}")
        title.setObjectName("firmwarePartitionTitle")
        badge = QtWidgets.QLabel("未检测")
        badge.setObjectName("firmwarePartitionBadge")
        badge.setProperty("active", False)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(badge)
        layout.addLayout(header)
        caption = QtWidgets.QLabel("固件版本")
        caption.setObjectName("firmwarePartitionCaption")
        layout.addWidget(caption)
        version = QtWidgets.QLabel("—")
        version.setObjectName("firmwarePartitionVersion")
        layout.addWidget(version)
        role = QtWidgets.QLabel("")
        role.setObjectName("firmwarePartitionRole")
        role.setWordWrap(True)
        layout.addWidget(role)
        frame._badge = badge
        frame._version = version
        frame._role = role
        return frame

    @staticmethod
    def _refresh_style(widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _arrange_partition_cards(self, stacked):
        if self._partitions_stacked == stacked:
            return
        self._partitions_stacked = stacked
        for card in self.partition_cards.values():
            self.partition_grid.removeWidget(card)
        if stacked:
            self.partition_grid.addWidget(self.partition_cards["a"], 0, 0)
            self.partition_grid.addWidget(self.partition_cards["b"], 1, 0)
        else:
            self.partition_grid.addWidget(self.partition_cards["a"], 0, 0)
            self.partition_grid.addWidget(self.partition_cards["b"], 0, 1)

    def resizeEvent(self, event):
        self._arrange_partition_cards(event.size().width() < 620)
        super().resizeEvent(event)

    def _update_partition_cards(self):
        active = self.state.active if self.state else ""
        standby = "b" if active == "a" else "a" if active == "b" else ""
        for slot, card in self.partition_cards.items():
            is_active = slot == active
            card.setProperty("active", is_active)
            card._badge.setProperty("active", is_active)
            if not self.state:
                badge, version, role = "未检测", "—", "连接设备后显示"
            elif is_active:
                badge = "当前运行"
                version = self.state.values["version"]
                role = "设备正在使用此分区"
            else:
                badge = "备用"
                version = self.standby_slot["version"] if self.standby_slot else "—"
                role = "固件安装目标"
                if self.standby_error:
                    version, role = "无法读取", self.standby_error
                if slot == standby and self.transaction[0] == "success" and self.image:
                    badge, version, role = "等待切换", self.image.version, "重启后成为当前运行分区"
            card._badge.setText(badge)
            card._version.setText(version)
            card._role.setText(role)
            card.setToolTip(role if self.standby_error and not is_active else "")
            self._refresh_style(card)
            self._refresh_style(card._badge)

    def _update(self):
        connected = self.ssh_client.is_connected()
        safe = self.transaction[0] in ("none", "completed")
        for key, button in self.buttons.items():
            enabled = not self.busy
            if key in ("refresh", "restore", "install", "switch", "reboot", "restore_plugins"):
                enabled &= connected
            if key in ("restore", "install", "switch"):
                enabled &= safe
            if key == "install":
                enabled &= self.image is not None
            if key == "download":
                enabled &= self.releases.currentData() is not None
            if key == "reboot":
                enabled &= self.transaction[0] == "success"
            if key == "restore":
                enabled &= bool(self.state and self.state.values.get("engine_file") == "masked-runtime")
            if key == "restore_plugins":
                enabled &= self.restore_report is not None
            button.setEnabled(enabled)
        self.buttons["reboot"].setVisible(self.transaction[0] == "success")
        self.buttons["restore_plugins"].setVisible(self.restore_report is not None)
        self.buttons["restore"].setVisible(
            bool(self.state and self.state.values.get("engine_file") == "masked-runtime")
        )
        self.platform.setEnabled(not self.busy)
        self.releases.setEnabled(not self.busy)
        self._update_partition_cards()

    def _toggle_advanced(self, expanded):
        self.advanced_toggle.setArrowType(QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)
        self.advanced.setVisible(expanded)

    def connection_changed(self, connected):
        self.state = None
        self.standby_slot = None
        self.standby_error = ""
        self.restore_report = None
        self.transaction = ("unknown", "请检测设备状态")
        self.status.setText("已连接，请检测设备状态" if connected else "未连接；已提交的安装不会因断线停止")
        self._update()

    def _run(self, function, done, *, device=False, progress=False):
        if self.busy:
            return
        token = self.ssh_client.ensure_client() if device else None
        self.busy = True
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self._update()
        worker = _rmtool.Worker(lambda: function(lambda done, total: worker.signals.progress.emit(
            min(100, int(done * 100 / max(total, 1))), 100))) if progress else _rmtool.Worker(function)
        self.worker = worker

        def finish(result):
            if sip.isdeleted(self):
                return
            self.busy = False
            self.worker = None
            self.progress.setVisible(False)
            if device and getattr(self.ssh_client, "firmware_guard_reason", ""):
                self.transaction = ("unknown", "请查询进度")
            if not device or (self.ssh_client.is_connected() and self.ssh_client._client is token):
                done(result)
            self._update()

        def fail(exc):
            if sip.isdeleted(self):
                return
            self.busy = False
            self.worker = None
            self.progress.setVisible(False)
            if device and getattr(self.ssh_client, "firmware_guard_reason", ""):
                self.transaction = ("unknown", "请查询进度")
            self._update()
            show_error(self, "固件管理", str(exc))

        worker.signals.finished.connect(finish)
        worker.signals.error.connect(fail)
        worker.signals.progress.connect(self._progress)
        QtCore.QThreadPool.globalInstance().start(worker)

    def _progress(self, done, total):
        self.progress.setRange(0, total)
        self.progress.setValue(done)

    def load_releases(self):
        platform = self.platform.currentData()

        def done(releases):
            self.releases.clear()
            for release in releases:
                self.releases.addItem(f"{release.version} · {release.channel}", release)
            self.status.setText(f"官方列表：{len(releases)} 个版本")

        self._run(lambda: firmware.list_releases(platform), done)

    def _image_loaded(self, image):
        self.image = image
        device = "Paper Pro" if image.platform == "ferrari" else "Paper Pro Move"
        self.selected.setText(f"目标固件：{image.version} · {device}\n已下载并完成完整性检查")

    def choose_local(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择固件", "", "固件 (*.swu)")
        if path:
            self._run(lambda callback: firmware.inspect_image(Path(path), progress=callback), self._image_loaded, progress=True)

    def download(self):
        release = self.releases.currentData()
        if release:
            self._run(lambda callback: firmware.download_release(release, _rmtool.app_state_dir() / "cache" / "firmware", callback), self._image_loaded, progress=True)

    def _device_loaded(self, result):
        self.state, self.transaction, self.standby_slot, self.standby_error = result
        state = self.state
        self.platform.setCurrentIndex(self.platform.findData(state.platform))
        device = "Paper Pro" if state.platform == "ferrari" else "Paper Pro Move"
        self.status.setText(f"{device} · 当前固件 {state.values['version']} · 电量 {state.values['battery']}%\n"
                            f"{self.transaction[1].replace('事务', '安装状态')}")
        self.advanced_status.setText(f"自动更新 {state.values['engine_file']}")
        self.restore_report = None
        if self.transaction[0] == "completed":
            QtCore.QTimer.singleShot(0, self._detect_plugin_restore)

    def _detect_plugin_restore(self):
        if self.busy or not self.ssh_client.is_connected():
            return
        self._run(
            lambda: residue_migration.inspect_residue(self.ssh_client),
            self._plugin_restore_detected,
            device=True,
        )

    def _plugin_restore_detected(self, report):
        self.restore_report = report
        self.buttons["restore_plugins"].setText(
            "恢复更新前插件" if report is not None and report.migratable else "查看插件恢复状态"
        )
        if report is not None:
            suffix = "可以一键恢复。" if report.migratable else "暂时不能自动恢复。"
            self.status.setText(self.status.text() + "\n检测到更新前插件，" + suffix)
        self._update()

    def restore_plugins(self):
        report = self.restore_report
        if report is None:
            return
        if not report.migratable:
            detail = "\n".join(report.blockers) or report.detail
            show_error(self, "恢复更新前插件", detail)
            return
        enabled = "、".join(item.label for item in report.features if item.enabled) or "无"
        disabled = "、".join(item.label for item in report.features if not item.enabled) or "无"
        detail = f"原来启用：{enabled}\n原来停用：{disabled}\n\n恢复后请手动重启设备生效。"
        if not ask_confirmation(
            self,
            "恢复更新前插件",
            "使用当前固件的精确插件包恢复更新前状态？",
            detail=detail,
        ):
            return
        self._run(
            lambda: residue_migration.migrate(self.ssh_client, str(_rmtool.app_state_dir())),
            self._plugin_restore_finished,
            device=True,
        )

    def _plugin_restore_finished(self, _report):
        self.restore_report = None
        self.buttons["restore_plugins"].setText("恢复更新前插件")
        self.status.setText("更新前插件已按当前固件恢复；请手动重启设备生效。")
        self._update()

    def refresh(self):
        def inspect():
            state, transaction = firmware.inspect_device(self.ssh_client)
            try:
                slot, error = firmware.inspect_slot_metadata(self.ssh_client, state), ""
            except (RuntimeError, OSError, ValueError) as exc:
                slot, error = None, str(exc)
            return state, transaction, slot, error

        self._run(inspect, self._device_loaded, device=True)

    def restore(self):
        token = self.ssh_client.ensure_client()
        if ask_confirmation(self, "固件管理", "恢复 rmtool 暂停前的自动更新状态？"):
            self._run(
                lambda: firmware.prepare_updater(self.ssh_client, token, confirmed=True, restore=True),
                self.status.setText,
                device=True,
            )

    def _confirm_plan(self, plan):
        target = plan.image.version if plan.image else plan.slot["version"]
        operation = "安装" if plan.image else "切换备用分区"
        detail = ("共享笔记数据不会随固件回退，请先备份。旧版系统可能无法读取新版数据。\n"
                  "rmtool 会在提交前临时暂停自动更新；若预检失败会自动恢复。\n"
                  "第三方应用和插件不会参与固件检查；更新后请重新确认兼容性。\n"
                  "提交后不能取消，不会自动重启。目标插件兼容性尚未核验。")
        if not ask_confirmation(self, "固件管理", f"{operation}到 {target}？", detail=detail, danger=True):
            return
        if plan.downgrade and not ask_confirmation(self, "确认降级", "确认承担共享数据不兼容风险并降级？", danger=True):
            return
        function = firmware.start_install if plan.image else firmware.switch_slot
        if plan.image:
            self._run(lambda callback: function(self.ssh_client, plan, confirmed=True, downgrade_confirmed=plan.downgrade, progress=callback), self._transaction_loaded, device=True, progress=True)
        else:
            self._run(lambda: function(self.ssh_client, plan, confirmed=True, downgrade_confirmed=plan.downgrade), self._transaction_loaded, device=True)

    def _transaction_loaded(self, result):
        self.transaction = result
        self.status.setText(result[1].replace("事务", "安装状态"))

    def install(self):
        image = self.image
        self._run(lambda: firmware.preflight(self.ssh_client, image), self._confirm_plan, device=True)

    def switch(self):
        self._run(lambda: firmware.preflight(self.ssh_client, switch=True), self._confirm_plan, device=True)

    def reboot(self):
        token = self.ssh_client.ensure_client()
        if ask_confirmation(self, "确认重启", "已确认设备端事务成功。现在重启进入目标固件？", danger=True):
            self._run(lambda: firmware.reboot_after_success(self.ssh_client, token, confirmed=True), lambda _: None, device=True)
