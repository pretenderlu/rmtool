# rmtool 重构工作记录

> 日期：2026-05-02（最后更新）
> 基线：4390 行单文件 `rmtool.py`、39 个测试、依赖未 pin、无 .gitignore
> 结果：主文件降至 746 行（-83%），拆出 6 个模块，测试 41/41 全绿，GUI 启动正常，git 基线已建立并同步到 GitHub。后续决定回归源码直跑、不再维护 PyInstaller 单文件打包。

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

### 阶段 G：git 基线、PyInstaller 验证、log 修复、远端同步（2026-05-01）

**git 基线建立** — commit `d9dc848 chore: initial commit after module split refactor`
- 4 月 16 日重构开始时本地仓库才 `git init`，重构前的 4390 行版本和远端 main HEAD 的 1863 行版本之间约 2500 行的渐进开发**完全没有 git 历史**
- 入库 19 个文件：源码（rmtool.py + 6 子模块 + rmrl/）、测试、web 仪表盘、`rmtool.spec`、`rmtool.bat`、`config.json`（默认配置无敏感信息）、`requirements.txt`、`README.md`、`REFACTOR_NOTES.md`、`.gitignore`
- `dist/`、`build/`、`__pycache__/`、`*.log`、`.claude/`、`.superpowers/` 经 `.gitignore` 正确忽略

**PyInstaller 真机打包验证**
- 用 `pyinstaller rmtool.spec --clean --noconfirm`，72 秒构建成功
- 新 exe 132.8 MB，进程冒烟启动正常（5s+ 存活，bootloader + 主程序内存 55 MB）
- `warn-rmtool.txt` 仅 Unix-only 模块（pwd / grp / fcntl / termios / _scproxy）和 `collections.abc` 已知误报
- `_MEIPASS` 资源加载、6 个子模块 hiddenimports 全部解析正常
- `config.json` 与 `known_hosts` 仍正确读写 `%APPDATA%\rmtool\`，未受打包影响

**Log 路径 bug 修复** — commit `2580b2d fix(log): write log to %APPDATA%/rmtool instead of __file__'s parent`
- 发现：`rmtool.py` 顶部 `RotatingFileHandler` 用 `Path(__file__).resolve().parent / "remarkable_tool.log"`。在 PyInstaller `--onefile` 模式下，`__file__` 解析到 `_MEIxxxx` 临时解压目录，**exe 退出时整个临时目录连同日志一起被清理**——这就是 `dist/remarkable_tool.log` 自 4 月 15 日起不再有新条目的原因
- 修复：把 `app_state_dir()` 函数从原 296 行上移到 logging 块之前（依赖只有 `sys`、`os`、`Path`，全在文件顶部 import 已就绪），日志改写到 `app_state_dir() / "remarkable_tool.log"`，与 `config.json`、`known_hosts` 同处一目录
- 副作用：脚本模式日志位置也变了，从 `E:\rmtool-main\remarkable_tool.log` 迁到 `%APPDATA%\rmtool\remarkable_tool.log`。这是有意的统一
- 验证：重新打包 exe → 启动 → 在 `%APPDATA%\rmtool\` 成功创建日志文件；41/41 单元测试仍全绿

**README 同步至重构后现实** — commit `8fff92e docs: refresh README to match post-refactor reality`
- 依赖列表补 `rmscene`（之前漏写，会导致用户照 README 装包后 rmrl 渲染笔记报错）
- 壁纸功能补一句"上传待机壁纸时自动清除固件 3.27 carousel 插图覆盖"
- 新增引用块说明配置/known_hosts/日志统一存放在 `%APPDATA%\rmtool\` 或 `~/.config/rmtool/`

**同步到 GitHub `pretenderlu/rmtool`**
- 远端默认分支是 `main`（不是 `master`），最近一次 push 是 2025-10-11，含 18 个 PR 历史
- 远端 `rmtool.py` HEAD 是 1863 行，本地重构基线是 4390 行——**中间约 2500 行的渐进开发完全不在 git 历史里**，无法干净地拆成多个 commit
- 决策：**force push** 覆盖远端 main（用户授权），远端 main 的 18 个 commit 历史从分支视图消失，PR 列表页本身仍保留
- 当前远端 main 与本地完全一致：`8fff92e` ← `2580b2d` ← `d9dc848`

**Python 包结构迁移评估**
- 评估结论：**不做**。收益主要是去掉 `sys.modules.setdefault` hack 和子模块的 `import rmtool as _rmtool` 惰性访问、以及 `rmtool.spec` 6 条 hiddenimports 自动维护——但这些已是稳定的、文档化过的模式
- 代价：7 个文件 import 路径全要改 + 41 条测试里大量 `mock.patch.object(rmtool, "...")` 直接访问顶层符号，要么在 `__init__.py` re-export 要么测试改路径，机械工作量大且易回归
- 触发条件（现都不满足）：pip 安装/被其他项目 import、有新 contributor 看不懂惰性访问、IDE 跳转受下划线前缀困扰
- 等真有 SDK 化或开源宣传需求时再做

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

## 待办

### 真机回归剩 9 项（参见上方"真机回归测试"表）

字体上传 / 时间同步 / 时区设置 / Wi-Fi SSH / 文档上传 / 文档导出 / 仪表盘联动 / 主题切换 / 设备重启 / 前光亮度——晚点用户自行验证。

### README 中"恢复备份的 `suspended.png.backup`"FAQ 待 verify

待真机回归时确认当前 UI 是否仍提供这个恢复入口，决定该 FAQ 是否需要更新或删除。

---

## 已完成（2026-05-01）

- ✅ PyInstaller 打包验证（阶段 G）
- ✅ git init + 建立基线（阶段 G，commit d9dc848）
- ✅ 同步到 GitHub `pretenderlu/rmtool`（force push 覆盖远端）

---

## 阶段 H：移除单文件打包，回归源码直跑（2026-05-02）

**决策**：放弃 PyInstaller 单文件分发，项目坚持源码直跑路线。

**动机**：
- 保持开源项目轻量性，减少维护面（不再随依赖升级修 hiddenimports / 各平台 hook 兼容）
- 跨平台稳定性靠各平台原生 Python 解释器，比单文件 bootloader 在 macOS/Linux 上的边角问题更可靠
- 用户群本就是会装 Python 的开发者 / 进阶玩家，`pip install -r requirements.txt` 一行就能跑

**改动**：
- ❌ 删除 `rmtool.spec`、`dist/`（含 139 MB 的 `rmtool.exe`）、`build/`（含 PyInstaller 缓存与早期 UI 调试截图）
- ✏️ `rmtool.py` `resource_path()`：去掉 `_MEIPASS` 分支，只保留 `Path(__file__).resolve().parent.joinpath(...)`
- ✏️ `README.md`：删除"打包为单文件可执行程序"整章及虚拟环境提示，首段宣传语改为强调源码直跑
- ✏️ `.gitignore`：删除 `build/` `dist/` 两条 PyInstaller 专属忽略

**保留**：
- 阶段 G 的 PyInstaller 验证 / log 路径修复历史记录原样保留——log 路径修复（写入 `%APPDATA%\rmtool\` 而非 `__file__` 同目录）在源码模式下同样是更合理的位置

---

## 已评估并放弃

### Python 包结构迁移

收益小（仅去掉两个已文档化的稳定模式 + spec 6 条 hiddenimports），代价大（7 文件 import + 41 测试的 mock 路径都要改）。等有具体动机（pip 安装、SDK 化）再做。详见阶段 G 记录。

---

## 改动文件清单

- ✏️ 修改：`rmtool.py`、`rmtool.spec`、`requirements.txt`、`README.md`、`tests/test_rmtool_behaviors.py`
- ✏️ 阶段 E 修改：`rmtool.py`（+`sys.modules` 注册）、`_tab_connection.py`、`_tab_documents.py`（预览翻页）、`_tab_toolbox.py`、`_tab_wallpaper.py`（全部改为惰性导入）、`tests/test_rmtool_behaviors.py`（mock 方法名更新）
- ✏️ 阶段 F 修改：`_tab_documents.py`（预览改为封面）、`_tab_wallpaper.py`（+carousel 清除）、`_tab_toolbox.py`（KOReader 改为链接）、`rmtool.py`（移除 _tab_koreader 导入）、`rmtool.spec`（移除 _tab_koreader）、`tests/test_rmtool_behaviors.py`（mock 方法名更新）
- ✏️ 阶段 G 修改：`rmtool.py`（log 路径改用 `app_state_dir()`，函数上移）、`README.md`（依赖列表 + carousel + 配置/日志位置说明）
- ➕ 新增：`.gitignore`、`_styles.py`、`_ssh.py`、`_tab_connection.py`、`_tab_documents.py`、`_tab_wallpaper.py`、`_tab_toolbox.py`
- ➖ 已删除：`_tab_koreader.py`（KOReader 安装功能移除，改为链接引导）
- 🔒 未动：`rmrl/`、`web/`、`rmtool.bat`、`config.json`

---

## git 历史（远端 `pretenderlu/rmtool` main）

```
8fff92e docs: refresh README to match post-refactor reality
2580b2d fix(log): write log to %APPDATA%/rmtool instead of __file__'s parent
d9dc848 chore: initial commit after module split refactor
```

注：force push 覆盖了远端原 main 上 18 个 PR 的 commit 历史。PR 列表页（https://github.com/pretenderlu/rmtool/pulls）本身仍保留。
