# 自用小工具，方便对rmpp的使用，全部由Claude完成。编码小白，有问题别问我，哈哈，应该没啥问题。仅测试了Windows。readme也是Claude写的。。。

# reMarkable 管理工具

这是一个用于管理 reMarkable 电子墨水平板的 Python 工具。它提供了多种功能，使您能够轻松地对设备进行自定义和管理。

## 功能特性

1. 传输字体文件
2. 更换壁纸
3. 管理设备时间
4. 重启设备
5. 启用SSH
6. 调整前光亮度
7. 切换连接方式

## 安装要求

- Python 3.6 或更高版本
- 必要的 Python 库（脚本会自动安装）

## 安装步骤

1. 克隆或下载此仓库到本地机器。
2. 确保您的系统中已安装 Python 3.6 或更高版本。
3. 运行脚本时，它会自动检查并安装所需的依赖库。
4. **请在根目录自行创建`fonts` `wallpaper`文件夹。**

## 使用方法

1. 确保您的系统已安装 Python 3.6 或更高版本。
2. 下载或克隆此项目到本地。
3. 运行 `rmtool.py` 脚本:
   - 在 Windows 上,双击 `rmtool.bat` 文件。
   - 在 macOS 或 Linux 上,在终端中运行 `python3 rmtool.py`。
4. 首次运行时,脚本会自动安装所需的依赖。
5. 按照屏幕上的提示操作。

注意: 如果您的系统没有安装 pip,请先安装 pip 再运行脚本。

## 功能详解

### 1. 传输字体文件

允许您将自定义字体文件（.ttf 格式）传输到 reMarkable 设备。您可以选择是否将字体文件重命名为 zwzt.ttf（推荐修改，方便管理）。

### 2. 更换壁纸

使您能够更换 reMarkable 设备的待机屏幕壁纸。支持分辨率为1620x2160（最佳）的png格式（其他格式未测试）的图片文件。

### 3. 管理设备时间

提供查看和设置设备时间的功能。您可以手动设置时间或使用网络时间同步。

**时间存在错误的话，会导致无法进行账号配对同步。**

### 4. 重启设备

允许您远程重启 reMarkable 设备。

### 5. 启用 SSH over WLAN

帮助您启用 reMarkable 设备的 SSH over WLAN 功能，方便后续通过 WiFi 进行连接。

### 6. 调整前光亮度

默认前光亮度极低，可适当提高前光亮度。

### 7. 切换连接方式

支持在 USB 和 WiFi 连接方式之间切换，为不同的使用场景提供灵活性。

## 注意事项

- 在使用此工具之前，请确保您的 reMarkable 设备已启用 SSH 访问（开启开发者模式，会重置设备）。
- 首次使用时，工具会提示您输入设备连接信息（IP 地址和 root 密码）。这些信息将被保存在本地的 config.json 文件中，以便后续使用（明文保存，但应该没有任何安全影响）。
- 使用此工具时请小心谨慎，特别是在修改系统文件或重启设备时。
- 建议在操作前备份重要数据。

## 贡献

欢迎提出建议和改进意见！如果您发现了 bug 或有新功能建议，请创建一个 issue 或提交 pull request。

## 常见问题

### 问题1：报“ModuleNotFoundError: No module named 'pkg_resources'”
解释：这个错误通常发生在尝试导入pkg_resources模块时，但Python找不到这个模块。pkg_resources是setuptools库的一部分，它用于处理Python包的安装和分发。
解决方法：
1. 检查setuptools是否已安装
你需要确认setuptools是否已经在你的Python环境中安装，你可以使用pip工具来查看已安装的库：
`pip list`
如果你看到setuptools在列表中，那么它已经被安装，如果没有，你需要安装它。
2. 安装setuptools
如果setuptools没有被安装，你可以使用pip来安装它：
`pip install setuptools`
这将下载并安装setuptools库。

### 问题2：报“ModuleNotFoundError: No module named 'paramiko'”
解释：
ModuleNotFoundError: No module named 'paramiko' 表示Python解释器无法找到名为paramiko的模块。paramiko是一个用于实现SSH连接和操作远程服务器的Python库。
解决方法：
安装paramiko模块。可以通过pip命令安装：
`pip install paramiko`

