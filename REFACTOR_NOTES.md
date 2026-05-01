# rmtool 重构工作记录

> 日期：2026-04-18（最后更新）
> 基线：4390 行单文件 `rmtool.py`、39 个测试、依赖未 pin、无 .gitignore
> 结果：主文件降至 746 行（-83%），拆出 6 个模块，测试 41/41 全绿，GUI 启动正常

---

## 当前文件结构

| 文件 | 行数 | 职责 |
|---|---|---|
| `rmtool.py` | ~745 | 常量、工具函数、配置、共用控件、MainWindow、main() |
| `_styles.py` | 1055 | Qt 样式表字符串 + QPalette 定义 |
| `_tab_documents.py` | ~780 | 文档上传/封面预览/管理 |
| `_tab_connection.py` | 596 | 设备连接侧栏 UI |
| `_tab_toolbox.py` | ~510 | FontTab、TimeTab、ControlTab、DashboardTab、ToolboxTab + KOReader 链接 |
| `_tab_wallpaper.py` | ~510 | 壁纸管理 Tab（含 carousel overlay 清除） |
| `_ssh.py` | 357 | SSH/SFTP 封装与装饰器 |

所有顶层符号通过 `from _XXX import ...` 在 `rmtool.py` 中再导出，`rmtool.WallpaperTab` / `rmtool.FontTab` / `rmtool.SSHClientWrapper` 等 20+ 个测试访问路径零修改。

> **重要**：子模块统一使用 `import rmtool as _rmtool` 惰性访问主模块符号，不再使用 `from rmtool import ...`。`rmtool.py` 顶部通过 `sys.modules.setdefault("rmtool", sys.modules[__name__])` 确保脚本运行时子模块拿到同一个模块对象。

---

## 已完成工作

### 阶段 A：仓库清理

- **`requirements.txt`** — 6 个依赖全部 pin 到当前测通版本：
  ```
  paramiko==4.0.0
  PyQt5==5.15.11
  PyQtWebEngine==5.15.7
  Pillow==12.2.0
  keyring==25.7.0
  rmscene==0.8.0
  ```
- **`.gitignore`** — 新建，覆盖 `__pycache__/`、`build/`、`dist/`、`*.log`、`.claude/`、`.superpowers/`、`docs/superpowers/` 等
- **`README.md`** — 新增"开发与测试"小节，说明 `python -m unittest discover -s tests -v` 如何运行

### 阶段 B：SSH 命令契约

- 审查后发现 `SSHClientWrapper.exec_checked`（rmtool.py 原 623 行）已存在并被 20 处调用，阶段 B 计划中的 `run_checked` 实际上已实现
- 剩下 2 处裸 `exec_command("reboot")` 是有意为之（reboot 会断连接，返回码不可靠）
- **新增 `ExecCheckedContractTests`（tests/test_rmtool_behaviors.py）** 两条用例文档化契约：
  - `test_exec_checked_returns_stdout_on_zero_exit`
  - `test_exec_checked_raises_runtime_error_on_nonzero_exit`

### 阶段 C：模块拆分 — 第一、二轮

原文件 `rmtool.py` 14+ 个类、4390 行，按"稳健"策略分轮拆分：

**第一轮**（样式 + SSH 层）
- `_styles.py`（1055 行）：`_DARK_STYLESHEET`、`_LIGHT_STYLESHEET`、`_dark_palette`、`_light_palette`
- `_ssh.py`（357 行）：`SSHClientWrapper`、`UnknownHostKeyError`、`remount_rw`、`require_connection`
  - 使用 `_get_known_hosts_path()`/`_get_host_key_fingerprint()`/`_get_app_name()` 惰性 getter 规避循环导入

**第二轮**（两个最大的 Tab）
- `_tab_documents.py`（803 行）：`DocumentsTab`
- `_tab_connection.py`（596 行）：`ConnectionWidget`
  - 遇坑：`keyring` 是可选依赖，测试用 `mock.patch.object(rmtool, 'keyring')` 打桩；`from rmtool import keyring` 会把值绑死在子模块命名空间，打桩失效。改为 `import rmtool` + `_keyring()` 惰性访问。

### 阶段 D：模块拆分 — 第三轮

**第三轮**（剩余全部 Tab 类，rmtool.py 从 1705 行降至 742 行）
- `_tab_wallpaper.py`（491 行）：`WallpaperTab`
- `_tab_toolbox.py`（492 行）：`FontTab`、`TimeTab`、`ControlTab`、`DashboardTab`、`ToolboxTab`
  - 遇坑：测试通过 `mock.patch.object(rmtool, 'Worker')` 打桩；子模块 `from rmtool import Worker` 会在导入时绑死引用，打桩失效。改为 `import rmtool as _rmtool` + `_rmtool.Worker(...)` 惰性访问。
  - 遇坑：测试 `rmtool.uuid` 需要 `uuid` 在 rmtool 命名空间可见，补回 `import uuid`。
- 同步修复 `rmtool.spec`：`hiddenimports` 补入全部 6 个子模块

### 阶段 E：启动修复与功能增强（2026-04-17）

**循环导入修复**

拆分后首次真机启动失败，报 `cannot import name 'ConnectionWidget' from '_tab_connection'`。根因：
- 子模块使用 `from rmtool import (APP_NAME, Worker, ...)` 在导入时绑定符号，但 `rmtool.py` 作为脚本运行时模块名是 `__main__`，子模块 `import rmtool` 触发第二次导入，形成循环。
- 修复措施：
  1. `rmtool.py` 顶部加 `sys.modules.setdefault("rmtool", sys.modules[__name__])`，确保只有一个模块对象。
  2. 四个子模块（`_tab_connection.py`、`_tab_documents.py`、`_tab_toolbox.py`、`_tab_wallpaper.py`）全部将 `from rmtool import ...` 改为 `import rmtool as _rmtool` + 运行时惰性访问（如 `_rmtool.APP_NAME`、`_rmtool.Worker(...)`）。
- 附带修复：双模块问题导致设备配置保存后重启丢失，统一模块对象后一并解决。

### 阶段 F：功能改进与真机验证（2026-04-18）

**文档预览改为封面预览**

- `_fetch_preview_pages` → `_fetch_preview_cover`：只下载 `.thumbnails` 目录中的第一张图片（封面）
- 移除翻页按钮（上一页/下一页）和页码标签，预览区直接显示封面
- 测试中 mock 方法名同步更新

**固件 3.27 待机壁纸 carousel 覆盖修复**

固件 3.27 新增了 `/usr/share/remarkable/carousel/` 目录，包含 3 张 `sleep_Illustration_*.png`，系统待机时会将这些插图叠加在 `suspended.png` 上方，导致自定义壁纸被部分遮挡。

- `_tab_wallpaper.py` 新增 `_clear_carousel_overlays()` 方法
- 上传待机壁纸时自动将 carousel 目录中的所有 PNG 替换为 1×1 透明图片
- 仅在目标为 `suspended.png` 时触发，其他壁纸类型不受影响
- 真机验证通过

**KOReader 安装功能调研与决策**

进行了完整的 KOReader / xovi / AppLoad 安装流程调研：
- 手动安装 xovi 后，`xovi/start` 执行 `systemctl restart xochitl` 会导致 Paper Pro 完整重启（`OnFailure=emergency.target`），tmpfs 挂载丢失
- AppLoad v0.5.0 不兼容固件 3.27（`Couldn't resolve the hashed identifier ... required by AppLoad hooks in main UI`）
- vellum 包管理器可正常安装 xovi，但 appload 包也受 OS 版本限制（`remarkable-os<3.27`）

**决策**：KOReader 安装功能**不集成到软件中**，因为过度依赖上游项目的固件兼容性同步更新。改为在 ToolboxTab 中以链接形式引导用户自行安装：
- vellum（包管理器）
- xovi（扩展框架）
- rm-appload（应用加载器）
- KOReader 安装指南

---

## 测试状态

```bash
python -m unittest discover -s tests -v
# Ran 41 tests in 0.4s — OK
```

41 = 原 39 + 新增 2（ExecCheckedContractTests）。

GUI 手动验证：主窗口正常启动，设备配置新增/保存/重启后持久化正常。

---

## 真机回归测试（2026-04-18，Paper Pro，固件 3.27）

| # | 功能 | 预期 | 通过 |
|---|---|---|---|
| 1 | USB 连接 | 连接成功，状态栏显示设备名 | ☑ |
| 3 | 壁纸上传 | 选择图片 → 预览 → 选择变体 → 上传 → carousel 覆盖自动清除 | ☑ |
| 7 | 文档列表 | 连接后文档中心自动刷新，显示文档数量 | ☑ |
| 14 | 文档封面预览 | 选中文档 → 显示封面缩略图 | ☑ |
| 15 | 设备配置持久化 | 新增设备 → 关闭重启 → 下拉菜单仍有该设备 | ☑ |
| 2 | 字体上传 | 选择 .ttf → 预览 → 上传 → 提示重启 | ☐ |
| 4 | 时间同步 | 点击同步 → 输出框显示同步结果 | ☐ |
| 5 | 时区设置 | 设置东八区 → 查看时间信息确认 | ☐ |
| 6 | Wi-Fi SSH | 开启 → 提示成功 | ☐ |
| 8 | 文档上传 | 选择 PDF → 上传 → 列表刷新 | ☐ |
| 9 | 文档导出 | 选择笔记 → 导出为 PDF | ☐ |
| 10 | 仪表盘 | 连接/断开状态实时更新，文档统计联动 | ☐ |
| 11 | 主题切换 | 深色 ↔ 浅色切换，所有 Tab 样式正常 | ☐ |
| 12 | 设备重启 | 设备控制 → 重启 → 确认弹窗 → 发送命令 | ☐ |
| 13 | 前光亮度 | 提升前光亮度 → 提示成功 | ☐ |

---

## 待办：PyInstaller 打包验证

`rmtool.spec` 的 `hiddenimports` 已补入全部 6 个子模块。需实际打包确认：

```bash
pyinstaller rmtool.spec
./dist/rmtool.exe
```

如果仍有 `ModuleNotFoundError`，检查报错模块名并追加到 `hiddenimports`。

---

## 可选后续

### 改为 Python 包结构

目前是散文件布局（`_styles.py` 等与 `rmtool.py` 平级）。迁移到 `rmtool/` 包需同步修改：
- `rmtool.spec`：入口 `rmtool.py` → `rmtool/__main__.py`
- `rmtool.bat`：`python rmtool.py` → `python -m rmtool`
- 内部 `from _ssh import ...` → `from rmtool.ssh import ...`

好处：符号空间清晰、PyInstaller `hiddenimports` 不再需要手写。代价：所有 import 路径都要改一遍。建议真机验证通过后再考虑。

### git init + 建立基线

`.gitignore` 已就位，随时可：
```bash
git init
git add .
git commit -m "chore: initial commit after module split refactor"
```

---

## 改动文件清单

- ✏️ 修改：`rmtool.py`、`rmtool.spec`、`requirements.txt`、`README.md`、`tests/test_rmtool_behaviors.py`
- ✏️ 阶段 E 修改：`rmtool.py`（+`sys.modules` 注册）、`_tab_connection.py`、`_tab_documents.py`（预览翻页）、`_tab_toolbox.py`、`_tab_wallpaper.py`（全部改为惰性导入）、`tests/test_rmtool_behaviors.py`（mock 方法名更新）
- ✏️ 阶段 F 修改：`_tab_documents.py`（预览改为封面）、`_tab_wallpaper.py`（+carousel 清除）、`_tab_toolbox.py`（KOReader 改为链接）、`rmtool.py`（移除 _tab_koreader 导入）、`rmtool.spec`（移除 _tab_koreader）、`tests/test_rmtool_behaviors.py`（mock 方法名更新）
- ➕ 新增：`.gitignore`、`_styles.py`、`_ssh.py`、`_tab_connection.py`、`_tab_documents.py`、`_tab_wallpaper.py`、`_tab_toolbox.py`
- ➖ 已删除：`_tab_koreader.py`（KOReader 安装功能移除，改为链接引导）
- 🔒 未动：`rmrl/`、`web/`、`rmtool.bat`、`config.json`
