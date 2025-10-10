<div align="center">

# rmtool · 现代化 reMarkable 桌面伴侣

一套完全基于 Web 技术打造的多设备管理器，涵盖文档上传、笔记预览导出、壁纸裁切以及安全凭证存储，并可通过 Electron 打包成跨平台可执行程序。

</div>

## ✨ 重新设计的亮点

- **全新技术栈**：Node.js + Express 负责与设备通信，React + Tailwind CSS 打造现代化深色 UI，Electron 将前后端封装为单一桌面应用，无需预装 Python。
- **多设备中心**：保存不同 reMarkable 设备的主机信息、型号与自定义目录，凭证以 AES-256-GCM 加密写入本地 `data/`，也可通过 `RMTOOL_SECRET_KEY` 预置密钥。
- **智能文档流**：直接在浏览器界面上传 PDF/EPUB，自动生成必要的 `.metadata`、`.content` 与缓存结构，并重启 `xochitl` 以立即刷新；同时扫描 `xochitl` 目录生成缩略图卡片并支持一键导出。
- **笔记转 PDF**：利用设备生成的缩略图生成高分辨率 PDF 预览，便于批量归档手写内容；若原笔记已经自带 PDF，仍可直接下载。
- **壁纸工作室**：针对 Paper Pro（2160×1620）与 Paper Pro Move（1696×954）进行不同的裁切策略，可选择填充或等比留白，并在上传前完成灰阶优化。
- **面向发布**：提供从开发调试到 `electron-builder` 打包的完整脚本，可生成 Windows、macOS、Linux 可执行文件与安装包。

## 🛠️ 环境准备

1. 安装 Node.js 18 或以上版本，以及 Git。
2. 克隆仓库后安装依赖：

   ```bash
   npm install
   ```

3. reMarkable 设备需开启开发者模式并允许 SSH 访问。

> 首次运行会在 `data/` 目录生成 `devices.json`、`secrets.json` 以及本地随机密钥（如未设置 `RMTOOL_SECRET_KEY`）。请妥善保管这些文件以便迁移。 

## 🚀 开发流程

- 启动前端、后端与 Electron 调试窗口：

  ```bash
  npm run dev
  ```

  - `Vite` 监听 `5173` 端口渲染 React 应用。  
  - `ts-node-dev` 运行 Express API（默认 `7788`）。  
  - Electron 自动指向开发服务器，并在需要时启动后端。  

- 仅启动 API 层（便于调试 REST 接口）：

  ```bash
  npm run dev:server
  ```

- 代码检查：

  ```bash
  npm run lint
  ```

## 📦 打包为桌面应用

1. 生成生产构建与编译 TypeScript：

   ```bash
   npm run build
   ```

   该命令会输出：

   - `dist/`：Vite 打包后的静态资源与复制的 Node API。  
   - `build/server`、`build/electron`：编译好的后台与 Electron 主进程。  

2. 使用 Electron Builder 产出可执行文件：

   ```bash
   npm run package
   ```

   默认会生成：

   - Windows `NSIS` 安装包 (`dist/win-unpacked`、`dist/*.exe`)  
   - macOS `dmg`  以及 Linux `AppImage` 构件。

3. 若需自定义应用 ID、图标或渠道包，可修改 `package.json` 中的 `build` 字段以及 `build-resources/` 目录资源。

## 🔐 凭证存储说明

- 默认情况下，应用会在 `data/.secret` 生成 32 字节随机密钥，并使用 AES-256-GCM 加密 `secrets.json` 内的 SSH 密码。  
- 若希望使用外部密钥管理，可在运行应用前设置：

  ```bash
  export RMTOOL_SECRET_KEY=$(openssl rand -base64 32)
  ```

- 删除设备时会自动清理对应的密文，避免遗留凭证。

## 📚 REST API 速览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| `GET` | `/api/devices` | 列出所有设备（不含密码）。 |
| `POST` | `/api/devices` | 新建设备配置，可附带密码。 |
| `PUT` | `/api/devices/:id` | 更新设备参数或密码。 |
| `DELETE` | `/api/devices/:id` | 删除设备。 |
| `GET` | `/api/devices/:id/documents` | 列出文档并生成缩略图。 |
| `POST` | `/api/devices/:id/documents` | 上传 PDF/EPUB。表单字段：`document`。 |
| `POST` | `/api/devices/:id/documents/:docId/export` | 基于缩略图生成 PDF 并返回二进制流。 |
| `POST` | `/api/devices/:id/wallpapers` | 上传壁纸。表单字段：`wallpaper` 及 `mode/focus/zoom` 等参数。 |

## 🤝 贡献与反馈

欢迎通过 Issue 或 Pull Request 分享你的需求与改进想法。如果你为特定设备增加了额外分辨率或转换算法，也期待你提交上游以便更多人受益。

