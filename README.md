<div align="center">

# reMarkable 管理工具（GUI 版）

</div>

一个面向 reMarkable 电子墨水平板的图形化管理工具，支持字体/壁纸上传、时间同步、设备控制、文档上传与预览等功能。新版引入基于 HTML5 的全屏仪表盘、更现代的界面视觉，内置多设备配置与密码安全存储，并能轻松打包成单文件可执行程序。

## 功能亮点

- **多设备管理**：在单个配置文件内保存多台 reMarkable 设备的连接信息与型号，密码可交由系统凭证管理器（`keyring`）安全保存。
- **HTML5 仪表盘**：主界面新增 Web 技术打造的仪表盘卡片视图，实时展示连接状态、设备信息与文档统计，并给出智能操作建议。
- **一键连接**：支持 USB 与 Wi-Fi 两种连接方式，连接成功即自动记忆地址并刷新各功能页。
- **字体管理**：直接从文件对话框选择字体（TTF/OTF），可选自动重命名为 `zwzt.ttf` 并上传至 `/home/root/.local/share/fonts/` 持久目录，系统更新后仍可保留。
- **壁纸处理**：针对不同型号自动匹配分辨率（Paper Pro 2160×1620、Paper Pro Move 1696×954、reMarkable 2 1404×1872），提供填充/裁剪/拉伸模式、偏移调节与实时预览。
- **时间设置**：同步本地时间、查看设备时间信息或一键将时区设为东八区。
- **设备控制**：支持重启设备、开启 Wi-Fi SSH 通道、提升前光亮度并创建持久化服务。
- **文档中心**：提供 PDF/EPUB 文档上传流程，可直接从桌面选取文件并推送到设备，并附带元数据视图与缩略图预览以便快速查阅内容。

## 运行环境

- Python 3.9 及以上版本（建议使用 64 位环境）。
- 依赖库：`paramiko`、`PyQt5`、`PyQtWebEngine`、`Pillow`、`keyring`（用于凭证安全存储，若缺失会自动降级为手动输入密码）。

```bash
pip install -r requirements.txt  # 或手动安装上述依赖
```

> **提示**：如果你计划打包成单文件可执行程序，推荐在干净的虚拟环境中安装依赖。

## 快速开始

1. 克隆或下载本仓库。
2. 在终端中运行：

   ```bash
   python rmtool.py
   ```

   Windows 用户可以继续使用仓库中的 `rmtool.bat` 启动脚本。

3. 首次连接时在界面左上角新建或选择设备条目，填写地址与 root 密码即可。勾选“记住密码”后凭证将保存到系统 keyring。连接成功后即可访问全部功能，无需手动创建 `fonts`、`wallpaper`、`documents` 目录。若希望后续通过 Wi-Fi 连接，在首次 USB 连接成功后请切换到“设备控制”页执行“开启 Wi-Fi SSH 通道”（底层命令 `rm-ssh-over-wlan on`），再改用 WLAN 地址连接。

## 打包为单文件可执行程序

项目已针对 PyInstaller 进行了优化，并提供了 `rmtool.spec` 以便在不同机器上生成自带 Python 运行时的单文件 EXE/APP。示例流程如下：

```bash
# 安装依赖后执行
pyinstaller rmtool.spec
```

PyInstaller 会自动将 Python 解释器、依赖库以及 HTML 仪表盘资源一并打包，因此目标机器无需预先安装 Python 环境。生成物位于 `dist/` 目录，`rmtool.exe` 可直接双击运行。如需在命令行自定义参数，可参考：

```bash
pyinstaller --noconsole --windowed --add-data "web/*;web" \
            --hidden-import PyQt5.QtWebEngineWidgets --hidden-import PyQt5.QtWebEngineCore \
            --name rmtool rmtool.py
```

如需在生成的可执行文件中使用自定义图标，可在执行 `pyinstaller rmtool.spec` 前设置环境变量 `RMTOOL_BUILD_ICON` 指向 `.ico` 文件。例如在 PowerShell 或 CMD 中：

```bash
set RMTOOL_BUILD_ICON=C:\\path\\to\\rmtool.ico
pyinstaller rmtool.spec
```

在类 Unix 系统中可以使用：

```bash
export RMTOOL_BUILD_ICON=/path/to/rmtool.ico
pyinstaller rmtool.spec
```

## 常见问题

- **连接失败？** 请确认 reMarkable 已开启开发者模式并允许 SSH 访问，且 USB/Wi-Fi 连接稳定。
- **上传壁纸后显示异常？** 可在壁纸页切换处理模式（填充/裁剪/拉伸）或调整偏移重新上传，必要时可在设置中恢复备份的 `suspended.png.backup`。

## 开源许可

欢迎提交 Issue 或 Pull Request 来帮助改进工具。

