# 将设备凭据保存在项目目录

## Goal

让源码直跑的 rmtool 在项目目录内持久保存设备连接信息和 root 密码，避免重复新增设备与输入密码，并减少对系统 keyring 的依赖。

## What I already know

- 当前设备配置保存在 `%APPDATA%/rmtool/config.json`。
- 当前 root 密码仅在用户勾选“记住密码”时写入系统 keyring。
- SSH 主机信任保存在 `%APPDATA%/rmtool/known_hosts`。
- 用户希望本地单机使用时，相关凭据集中保存在项目目录中的文件里。
- 当前 SSH 登录固定使用 `root`、密码认证，并禁用本地密钥和 SSH Agent。

## Assumptions (temporary)

- 最小方案复用现有 JSON 配置读写，不增加数据库或加密依赖。
- 凭据文件必须被 Git 忽略，避免随源码提交或推送。
- 不自动导入 `%APPDATA%`、旧项目根目录 `config.json` 或 keyring；项目本地文件不存在时从空列表开始。
- 用户手动选择设备；SSH 主机指纹用于连接安全校验，不用于后台自动识别。

## Open Questions

- 无。

## Requirements (evolving)

- 重启 rmtool 后仍能识别已保存设备并自动读取其 root 密码。
- 用户不需要依赖 Windows 凭据管理器才能记住密码。
- 不增加新的运行时依赖。
- 项目首次启动时自动创建空的项目本地 `devices.json`。
- `devices.json` 必须被 Git 忽略。
- 支持保存多台设备；启动时将已保存设备加载到下拉列表，由用户手动选择。
- 新增设备时由用户输入设备名称、连接方式、地址、型号和 root 密码。
- 仅当用户选择“记住密码”时保存该设备的密码；否则每次连接继续询问。
- 首次成功连接并确认 SSH 指纹后，持久保存对应主机信任信息。
- SSH 主机公钥继续采用 Paramiko 标准 `known_hosts` 文件，不嵌入 JSON。
- 项目本地状态目录为 `.rmtool/`，其中 `devices.json` 和 `known_hosts` 均被 Git 忽略。
- `devices.json` 复用当前完整配置结构，同时保存设备列表、活动设备、路径和主题，作为唯一配置源。
- 每个设备条目可包含明文 `password`；仅在用户选择“记住密码”时存在。
- 主机指纹变化时不得静默接受或继续使用旧信任记录。
- 不增加后台轮询、插拔监听或设备自动识别。
- 首次启动不创建假默认设备；设备列表为空时连接按钮不可用。
- 新增设备后立即保存资料；密码仅在勾选“记住密码”时写入设备条目。
- 选择设备并连接时优先使用条目中的密码，缺失时弹窗询问。
- 编辑设备可覆盖密码；取消“记住密码”或点击“忘记密码”会删除 `password` 字段。
- 删除设备时删除整个设备条目；MVP 不额外清理 `known_hosts` 中的历史公钥。
- 移除 Python `keyring` 运行时依赖和相关兼容逻辑。
- SSH 信任别名绑定不可变设备 UUID，而不是可编辑设备名。
- `devices.json` 使用同目录临时文件和 `os.replace()` 原子写入。
- JSON 损坏或项目目录不可写时明确报错，不静默清空，也不回退到 `%APPDATA%`。

## Acceptance Criteria (evolving)

- [ ] 新增设备并保存密码后，关闭并重启 rmtool，无需重新新增或输入密码。
- [ ] 本地文件包含设备 ID、连接地址和对应密码。
- [ ] 默认不会把真实凭据提交到 Git。
- [ ] 旧 `%APPDATA%` 配置不会被修改或删除。
- [ ] 项目首次启动生成空的 `devices.json`，不继承仓库中的其他用户设备。
- [ ] 保存多台设备后，重启应用可在下拉列表中逐台选择。
- [ ] 选择已记住密码的设备后，点击连接无需再次输入密码。
- [ ] 未记住密码的设备仍在每次连接时询问密码。
- [ ] SSH 指纹不匹配时拒绝静默连接。
- [ ] 主题、路径和活动设备在重启后仍从同一个 `devices.json` 恢复。
- [ ] 空设备列表不会生成默认设备，且连接操作不可用。
- [ ] 删除设备后其明文密码不再存在于 `devices.json`。
- [ ] 修改设备名称后继续使用同一 UUID 对应的 SSH 信任记录。
- [ ] 写入失败时原 `devices.json` 保持完整。
- [ ] 损坏 JSON 保持原样并阻止静默重置。

## Definition of Done

- Tests added or updated for persistence and first-run behavior.
- Config writes are atomic and failure paths preserve existing data.
- Existing tests pass.
- README documents the storage location and plaintext boundary.
- Rollback leaves the existing `%APPDATA%` and keyring path usable.

## Out of Scope

- 跨电脑云同步。
- 新增加密算法、主密码或数据库。
- SSH 私钥认证。

## Technical Notes

- `rmtool.py`: `app_state_dir`, `config_path`, `device_credential_key`.
- `_tab_connection.py`: `_load_password`, `_store_password`, `_delete_password`.
- `_ssh.py`: application-local `known_hosts` and password-only Paramiko connection.
- `_ssh.py`: `_fetch_remote_host_key` 可在密码认证前取得公钥；当前连接流程尚未用其查找设备记录。
- 当前设备 SSH 探测在需求讨论时超时，内部序列号路径尚未验证；MVP 不依赖登录后字段。
- 选择的文件组织方案：`.rmtool/devices.json` 保存设备资料和可选明文密码；`.rmtool/known_hosts` 复用现有 SSH 信任逻辑。
