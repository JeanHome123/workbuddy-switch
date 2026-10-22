# WorkBuddy 切号器

Windows 上的 WorkBuddy 多账号切换工具。在客户端账号菜单和登录页提供「切换账号」入口，选择账号后自动重启客户端。

- 每个档案使用独立配置目录，隔离登录态、会话和技能。
- 账号首次登录后，可在已有档案之间切换。
- 使用 Python 标准库，通过 CDP 注入界面，无需修改客户端安装文件。
- 不保存密码，不解析客户端访问令牌；登录态文件按档案保存，默认使用 Windows DPAPI 再加密。

## 安装和使用

需要 Windows、已安装的 WorkBuddy，以及客户端自带的 Python。先打开一次 WorkBuddy，使其准备好 Python 运行时。

1. 下载并解压完整源码到固定位置，双击 `安装.bat`。
2. 确认显示后台进程运行中。首次安装后，完全退出 WorkBuddy 再打开一次。
3. 点击左下角头像菜单中的「切换账号」；登录页也提供该入口。
4. 点「添加账号」，再选择新账号并完成首次登录。

安装会设置当前用户的调试端口环境变量和 Windows 登录自启项。安装后请保留源码目录的位置，自启项依赖其中的启动脚本。

正常列表展示账号昵称和头像；「管理」中可删除非默认、非运行中的档案。界面删除只移出列表，保留客户端数据。

双击 `卸载.bat` 可结束后台、移除自启项和相关环境变量；账号数据保留。完全退出并重开客户端后，当前页面的注入界面和调试连接才会退出。

## 命令行

以下命令中的 `python` 需要指向可用的 Python 3.10 或更高版本；双击脚本会自动定位 WorkBuddy 自带的 Python。

```powershell
python wbs.py list
python wbs.py new 工作号
python wbs.py switch 工作号 --dry-run
python wbs.py switch 工作号
python wbs.py status
python wbs.py doctor
python wbs.py rm 工作号
```

关闭超时且仍有可见窗口时，切换会取消。确认没有进行中的任务后，可在面板选择「强制切换」，或使用 `switch 工作号 --force`。只有托盘残留进程时，工具会结束残留进程继续切换。

`rm 工作号 --purge` 会删除该档案的数据目录，请谨慎使用。

## 数据与隐私

默认数据目录为 `%LOCALAPPDATA%\WorkBuddySwitch\`，默认档案沿用 `%USERPROFILE%\.workbuddy\`。运行数据不属于源码，不能公开上传，包括：

- `settings.json`、`agent.json`：包含本地接口 token。
- `profiles.json`、`runtime.json`、`operations/`：包含账号档案、路径和操作记录。
- `profiles/`：包含客户端配置、登录态快照、头像及账号资料。
- `backups/`、`quarantine/`、`logs/`：可能包含登录态文件、个人信息或诊断数据。
- 客户端共享登录态目录 `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\`。

加密的登录态文件也应作为敏感数据保管。报告问题时，请先删除日志和截图中的账号信息、token 与个人路径。

后台 HTTP 接口只绑定 `127.0.0.1`，业务接口校验随机生成并持久化的 token。CDP 默认端口为 `9339`，后台接口默认端口为 `9340`；CDP 不使用该接口 token，同机进程可能访问客户端调试能力，请勿将端口对外转发。

不要在两个档案里同时登录同一个账号，以免客户端刷新登录态时互相影响。

## 故障排查

入口没有出现时，重新运行 `安装.bat`，完全退出客户端后重开，并检查：

```powershell
python wbs.py status
python wbs.py doctor
```

本工具依赖客户端的配置目录环境变量和页面结构，客户端升级可能影响兼容性。切换前会检查目录隔离能力和目标快照；恢复或启动失败时会尝试回滚。

## 源码结构

```text
wbs.py          命令行入口
app/core.py     档案、快照、切换与回滚
app/win32.py    Windows 进程、窗口、DPAPI 和环境变量
app/cdp.py      WebSocket 与 CDP 通信
app/agent.py    本地 HTTP 接口
app/watch.py    后台循环与注入
inject/panel.js 客户端内的账号界面
```

仓库采用 `.gitignore` 文件白名单，仅收录运行所需源码、启动脚本与说明。测试、调试工具、截图、开发记录和运行数据均不收录；新增需要公开的文件时需明确更新白名单。

## 许可证

[MIT](LICENSE)。许可证适用于本仓库代码，不包含 WorkBuddy 客户端及其资源。
