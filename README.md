<div align="center">

# reMarkable 管理工具（GUI 版）

</div>

一个面向 reMarkable 电子墨水平板的图形化管理工具，支持字体/壁纸上传、时间同步、设备控制、文档上传与预览等功能。新版引入基于 HTML5 的全屏仪表盘、更现代的界面视觉，内置多设备配置与密码安全存储。项目坚持源码直跑路线，跨平台无需打包步骤，部署即用。

## 功能亮点

- **多设备管理**：在单个配置文件内保存多台 reMarkable 设备的连接信息与型号，密码可交由系统凭证管理器（`keyring`）安全保存。
- **HTML5 仪表盘**：主界面新增 Web 技术打造的仪表盘卡片视图，实时展示连接状态、设备信息与文档统计，并给出智能操作建议。
- **一键连接**：支持 USB 与 Wi-Fi 两种连接方式，连接成功即自动记忆地址并刷新各功能页。
- **字体管理**：直接从文件对话框选择字体（TTF/OTF），可选自动重命名为 `zwzt.ttf` 并上传至 `/home/root/.local/share/fonts/` 持久目录，系统更新后仍可保留。
- **壁纸处理**：针对不同型号自动匹配分辨率（Paper Pro 2160×1620、Paper Pro Move 1696×954、reMarkable 2 1404×1872），提供填充/裁剪/拉伸模式、偏移调节与实时预览。上传待机壁纸时会自动清除固件 3.27 引入的 carousel 插图覆盖，避免自定义壁纸被遮挡。
- **时间设置**：同步本地时间、查看设备时间信息或一键将时区设为东八区。
- **设备控制**：支持重启设备、开启 Wi-Fi SSH 通道、提升前光亮度并创建持久化服务。
- **文档中心**：提供 PDF/EPUB 文档上传流程，可直接从桌面选取文件并推送到设备，并附带元数据视图与缩略图预览以便快速查阅内容。

## 运行环境

- Python 3.9 及以上版本（建议使用 64 位环境）。
- 依赖库：`paramiko`、`PyQt5`、`PyQtWebEngine`、`Pillow`、`keyring`（用于凭证安全存储，若缺失会自动降级为手动输入密码）、`rmscene`（手写笔记渲染所需）。具体版本见 `requirements.txt`。
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

   Windows 用户可以继续使用仓库中的 `rmtool.bat` 启动脚本。

3. 首次连接时在界面左上角新建或选择设备条目，填写地址与 root 密码即可。勾选“记住密码”后凭证将保存到系统 keyring。连接成功后即可访问全部功能，无需手动创建 `fonts`、`wallpaper`、`documents` 目录。若希望后续通过 Wi-Fi 连接，在首次 USB 连接成功后请切换到“设备控制”页执行“开启 Wi-Fi SSH 通道”（底层命令 `rm-ssh-over-wlan on`），再改用 WLAN 地址连接。

> 配置文件、SSH 已知主机指纹与运行日志统一存放在 `%APPDATA%\rmtool\`（Windows）或 `~/.config/rmtool/`（Linux/macOS），排错时可在该目录查看 `remarkable_tool.log`。

## 常见问题

- **连接失败？** 请确认 reMarkable 已开启开发者模式并允许 SSH 访问，且 USB/Wi-Fi 连接稳定。
- **上传壁纸后显示异常？** 可在壁纸页切换处理模式（填充/裁剪/拉伸）或调整偏移重新上传，必要时可在设置中恢复备份的 `suspended.png.backup`。

## 开发与测试

本地运行测试套件：

```bash
python -m unittest discover -s tests -v
```

测试默认使用 `QT_QPA_PLATFORM=offscreen`，无需真实显示器。

## 开源许可

欢迎提交 Issue 或 Pull Request 来帮助改进工具。

