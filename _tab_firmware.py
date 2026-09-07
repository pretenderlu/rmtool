"""Native firmware page; all device work stays in the existing worker pool."""
from pathlib import Path

from PyQt5 import QtCore, QtWidgets, sip

import _firmware as firmware
from _dialogs import ask_confirmation, show_error
import rmtool as _rmtool


class FirmwareTab(QtWidgets.QWidget):
    def __init__(self, ssh_client, config=None, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.image = None
        self.state = None
        self.transaction = ("none", "尚未检测")
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
        self.platform = QtWidgets.QComboBox()
        self.platform.addItem("Paper Pro", "ferrari")
        self.platform.addItem("Paper Pro Move", "chiappa")
        self.releases = QtWidgets.QComboBox()
        self.releases.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.releases.setMinimumContentsLength(16)
        form = QtWidgets.QFormLayout()
        form.addRow("设备", self.platform)
        form.addRow("官方固件", self.releases)
        root.addLayout(form)
        self.selected = QtWidgets.QLabel("尚未选择 SWU")
        self.selected.setWordWrap(True)
        root.addWidget(self.selected)
        actions = QtWidgets.QGridLayout()
        self.buttons = {}
        for index, (key, label, callback) in enumerate((
            ("list", "获取官方列表", self.load_releases),
            ("download", "下载所选固件", self.download),
            ("local", "选择本地 SWU", self.choose_local),
            ("refresh", "检测设备 / 查询进度", self.refresh),
            ("pause", "暂停自动更新", self.pause),
            ("restore", "恢复自动更新", self.restore),
            ("install", "检查并安装", self.install),
            ("switch", "检查并切换 A/B", self.switch),
            ("reboot", "确认重启", self.reboot),
        )):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(callback)
            self.buttons[key] = button
            actions.addWidget(button, index // 2, index % 2)
        root.addLayout(actions)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        root.addWidget(self.progress)
        root.addStretch()
        self.platform.currentIndexChanged.connect(self.releases.clear)
        self.releases.currentIndexChanged.connect(self._update)
        ssh_client.connection_changed.connect(self.connection_changed)
        self._update()

    def _update(self):
        connected = self.ssh_client.is_connected()
        safe = self.transaction[0] in ("none", "completed")
        for key, button in self.buttons.items():
            enabled = not self.busy
            if key in ("refresh", "pause", "restore", "install", "switch", "reboot"):
                enabled &= connected
            if key in ("pause", "restore", "install", "switch"):
                enabled &= safe
            if key == "install":
                enabled &= self.image is not None
            if key == "download":
                enabled &= self.releases.currentData() is not None
            if key == "reboot":
                enabled &= self.transaction[0] == "success"
            button.setEnabled(enabled)
        self.platform.setEnabled(not self.busy)
        self.releases.setEnabled(not self.busy)

    def connection_changed(self, connected):
        self.state = None
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
            show_error(self, "固件管理", str(exc) + "\n若已暂停自动更新且尚未提交事务，可恢复自动更新。")

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
        self.selected.setText(f"{image.version} / {image.platform}\n{image.path.name}\n结构与 SHA-256 已检查；原生签名将在设备端验证")

    def choose_local(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择固件", "", "固件 (*.swu)")
        if path:
            self._run(lambda callback: firmware.inspect_image(Path(path), progress=callback), self._image_loaded, progress=True)

    def download(self):
        release = self.releases.currentData()
        if release:
            self._run(lambda callback: firmware.download_release(release, _rmtool.app_state_dir() / "cache" / "firmware", callback), self._image_loaded, progress=True)

    def _device_loaded(self, result):
        self.state, self.transaction = result
        state = self.state
        self.platform.setCurrentIndex(self.platform.findData(state.platform))
        self.status.setText(f"{state.platform} / {state.values['version']}\n"
                            f"当前 {state.active.upper()} · 下次启动 {state.next_boot.upper()} · "
                            f"电量 {state.values['battery']}%\n{self.transaction[1].replace('事务', '安装状态')}")

    def refresh(self):
        self._run(lambda: firmware.inspect_device(self.ssh_client), self._device_loaded, device=True)

    def _updater(self, restore):
        token = self.ssh_client.ensure_client()
        message = "恢复本次暂停前的自动更新服务状态？" if restore else "临时暂停空闲的自动更新服务？重启后临时屏蔽失效，不改变永久更新策略。"
        if ask_confirmation(self, "固件管理", message):
            self._run(lambda: firmware.prepare_updater(self.ssh_client, token, confirmed=True, restore=restore), self.status.setText, device=True)

    def pause(self):
        self._updater(False)

    def restore(self):
        self._updater(True)

    def _confirm_plan(self, plan):
        target = plan.image.version if plan.image else plan.slot["version"]
        operation = "安装" if plan.image else "切换备用分区"
        detail = ("共享笔记数据不会随固件回退，请先备份。旧版系统可能无法读取新版数据。\n"
                  "现有可信插件将保持禁用，设置保留；需在目标固件上重新核验兼容性。\n"
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
