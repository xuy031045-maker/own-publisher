# 自有发布台

Windows 本地图文发布器。Codex 可以登记和查看账号、预览批次、执行已确认的发布并查询逐账号结果。首批接入小红书和抖音，单批最多 10 个目标账号。

## 本机使用

在项目目录的 PowerShell 中运行。需要 Python 3.11、uv 和 Microsoft Edge。

```powershell
uv venv --python 3.11 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.\发布台.ps1 add-account xhs01 xiaohongshu --label "小红书主号"
.\发布台.ps1 add-account dy01 douyin --label "抖音主号"
.\发布台.ps1 login xhs01
.\发布台.ps1 login dy01
.\发布台.ps1 check xhs01
```

首次在打开的独立 Edge 窗口里正常登录，并确认是对应的账号。进入创作者图文入口后窗口会自动关闭。登录数据保存在当前 Windows 用户的 `%LOCALAPPDATA%\OwnPublisher\profiles`，一个别名对应一个浏览器资料目录。别名由你命名，当前登录检查核对创作者页面是否可用，账号昵称由你在首次登录时核对。

参考 [图文批次示例](./图文批次示例.json) 新建自己的批次，填写已登记账号和实际图片路径。相对路径从批次 JSON 所在目录解析。每个目标可以继承共同文案和图片，也可以覆盖标题、正文、话题或图片。首版校验标题 1 到 20 字、正文最多 1000 字、JPG 或 PNG 图片 1 到 18 张。这是发布台自己的输入限制。

```powershell
.\发布台.ps1 preview .\我的图文.json
.\发布台.ps1 publish <批次号> --confirm <批次号>
.\发布台.ps1 status <批次号>
```

预览会保存批次并列出各账号实际使用的内容。发布时按账号依次执行。同一账号和同一内容已有提交或待核对记录时，会跳过重复项。每条结果实时写入本地数据库。

- `submitted` 表示创作者页面出现提交确认。审核结果和作品可见性在创作者后台核对。
- `failed` 表示提交前失败，`login_needed` 表示需要正常登录或人工验证。处理后可用 `--retry-failed` 重试。
- `uncertain` 表示提交后的结果不明确，或程序在执行途中退出。先核对平台作品记录。程序不会重发该条。

## Codex

MCP 通过标准输入输出运行。若换了电脑或移动了项目，用下面命令重新注册，然后重新打开 Codex 会话。

```powershell
codex mcp add own-publisher -- (Resolve-Path .\.venv\Scripts\python.exe).Path -X utf8 (Resolve-Path .\agent_server.py).Path
```

可对 Codex 说「用 own-publisher 预览我的图文批次，列出每个账号的标题和图片。等我确认批次后发布，并逐账号给出结果」。

## 实现依据

从本机的蚁小二逆向报告采用任务组拆分、账号会话隔离及提交与最终状态分离的架构。创作者网页操作参考 MIT 许可的 `dreammis/social-auto-upload` 提交 `3847fff01f5434a8a32829833163cd6723cabc59` 的图文入口与表单定位。账号使用独立 Edge 资料目录，单条发布只点击一次。智能体接口以标准 MCP 工具提供。 [开源发布器](https://github.com/dreammis/social-auto-upload/tree/3847fff01f5434a8a32829833163cd6723cabc59) · [Playwright 持久化浏览器会话](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context) · [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
