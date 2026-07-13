<div align="center">

# reMarkable 管理工具（GUI 版）

</div>

一个面向 reMarkable 电子墨水平板的图形化管理工具，支持字体/壁纸上传、时间同步、设备控制、文档上传与预览等功能。新版引入基于 HTML5 的全屏仪表盘、更现代的界面视觉，内置多设备配置。项目支持源码运行，也提供免安装 Python 的 Windows 便携版构建。

## 功能亮点

- **多设备管理**：在 `.rmtool/devices.json` 内保存多台 reMarkable 设备的连接信息与型号；勾选“记住密码”后也会保存明文 root 密码。
- **HTML5 仪表盘**：主界面新增 Web 技术打造的仪表盘卡片视图，实时展示连接状态、设备信息与文档统计，并给出智能操作建议。
- **一键连接**：支持 USB 与 Wi-Fi 两种连接方式，连接成功即自动记忆地址并刷新各功能页。
- **字体管理**：直接从文件对话框选择字体（TTF/OTF），可选自动重命名为 `zwzt.ttf` 并上传至 `/home/root/.local/share/fonts/` 持久目录；上传后会写入用户层 fontconfig 规则，让默认 CJK 字体优先匹配所选字体，系统更新后仍可保留。
- **壁纸处理**：针对不同型号自动匹配分辨率（Paper Pro 2160×1620、Paper Pro Move 1696×954、reMarkable 2 1404×1872），提供填充/裁剪/拉伸模式、偏移调节与实时预览。上传待机壁纸时会自动清除固件 3.27 引入的 carousel 插图覆盖，避免自定义壁纸被遮挡。
- **时间设置**：同步本地时间、查看设备时间信息或一键将时区设为东八区。
- **设备控制**：支持重启设备、开启 Wi-Fi SSH 通道、提升前光亮度并创建持久化服务。
- **原生界面汉化**：在“设备工具箱 > 系统汉化”中检测、启用或还原原生 Qt 中文翻译。当前仅支持 Paper Pro 固件 `20260629074044`（xochitl `3.28-tentacruel`，commit `8bee0a4`）；不兼容固件会被阻止写入。为避免 QMD 注入，中文目录借用内置法语槽位，启用期间无法使用法语。部署与还原会先停止 xochitl，防止运行中的设置缓存覆盖文件，再备份并原子替换目标；完成后关闭 SSH，会话内绝不启动或重启 xochitl，需由用户另行重启设备。
- **文档中心**：提供 PDF/EPUB 文档上传流程，可直接从桌面选取文件并推送到设备，并附带元数据视图与缩略图预览以便快速查阅内容。

## 运行环境

- Python 3.9 及以上版本（建议使用 64 位环境）。
- 依赖库：`paramiko`、`PyQt5`、`PyQtWebEngine`、`Pillow`、`rmscene`（手写笔记渲染所需）。具体版本见 `requirements.txt`。
- 仓库已内置用于渲染手写笔记的 `rmrl` 模块（依赖 `rmscene`），无需额外配置即可生成高清 PDF。

```bash
pip install -r requirements.txt  # 或手动安装上述依赖
```

## 快速开始

1. 克隆或下载本仓库。
2. 在终端中运行：

   ```bash
   python rmtool.py
   ```

   Windows 用户可以双击仓库中的 `rmtool.bat`，它会通过 `pythonw.exe` 静默启动主程序，不会留下黑色控制台窗口。如需查看运行日志，点击侧栏底部"日志"按钮即可在主窗口下方展开常驻日志面板，实时显示日志记录；面板高度可拖动调整，状态自动持久化。

3. 首次启动会在 `rmtool.py` 同目录创建 `.rmtool/devices.json`，设备列表为空。连接前先添加一台或多台设备，之后从下拉框手动选择已保存的设备；只有勾选“记住密码”时，root 密码才会以明文写入该文件。连接成功后即可访问全部功能，无需手动创建 `fonts`、`wallpaper`、`documents` 目录。若希望后续通过 Wi-Fi 连接，在首次 USB 连接成功后请切换到“设备控制”页执行“开启 Wi-Fi SSH 通道”（底层命令 `rm-ssh-over-wlan on`），再改用 WLAN 地址连接。

> `.rmtool/` 位于 `rmtool.py` 同目录且已被 Git 忽略。`.rmtool/devices.json` 保存设备列表、当前活动设备、路径设置、主题，以及勾选“记住密码”后才写入的明文 root 密码；`.rmtool/known_hosts` 按设备 UUID 隔离保存 SSH 主机信任；`.rmtool/remarkable_tool.log` 是滚动应用日志。
>
> **安全警告：** 复制、共享、备份到不受信任的位置或发布 `.rmtool/`，会暴露其中记住的 root 密码。旧版 `AppData` 与项目根目录中的配置、操作系统凭据库中的 root 密码及旧的 SSH 主机信任记录不会被导入，也不会被删除。

## Windows 便携版

使用 Windows x64 和 Python 在仓库根目录运行：

```powershell
.\build-portable.ps1
```

脚本会在 `build/.venv/` 创建隔离环境，按 `requirements.txt` 安装固定版本的运行时依赖和 PyInstaller `6.21.0`，然后生成 `dist/rmtool/` 和 `dist/rmtool-windows-x64.zip`。分发 ZIP 即可；便携版首次启动会在 `rmtool.exe` 旁创建 `.rmtool/`，请勿将该目录随软件一起发布。

## 常见问题

- **连接失败？** 请确认 reMarkable 已开启开发者模式并允许 SSH 访问，且 USB/Wi-Fi 连接稳定。
- **上传壁纸后显示异常？** 可在壁纸页切换处理模式（填充/裁剪/拉伸）或调整偏移重新上传，必要时可在设置中恢复备份的 `suspended.png.backup`。
- **汉化按钮不可用？** 先连接设备并点击“检测状态”。只有精确匹配支持固件时才允许启用；写入完成后请手动重启设备。
- **想看应用打印了什么？** 主界面侧栏底部"日志"按钮会在主窗口下方展开常驻日志面板，实时显示当前会话的日志，并附带级别过滤、暂停、自动滚动、清屏与"打开日志文件"快捷入口；面板内右上角的 × 可收起，再次点击侧栏按钮即可重新展开。

## 开发与测试

本地运行测试套件：

```bash
python -m unittest discover -s tests -v
```

测试默认使用 `QT_QPA_PLATFORM=offscreen`，无需真实显示器。

## 开源许可

本项目采用 [GNU General Public License v3.0](LICENSE)。中文翻译基于
[boangs/rmkit](https://github.com/boangs/rmkit) 的 GPL-3.0 翻译目录适配，
详见 [NOTICE.md](NOTICE.md)。欢迎提交 Issue 或 Pull Request 来帮助改进工具。
