# 配置说明

## 前置条件

### 指纹浏览器

支持以下浏览器类型；外部客户端模式需要保持客户端运行：

- 内置 Chromium：`FINGERPRINT_BROWSER=bundled`，安装程序会配置浏览器路径。
- 普通 Chrome/Chromium：`FINGERPRINT_BROWSER=custom`，通过 `CUSTOM_BROWSER_PATH` 指定可执行文件；留空时会尝试查找系统 Chrome。
- [BitBrowser 官方下载页](https://www.bitbrowser.cn/download)：默认 API 为 `http://127.0.0.1:54345`。
- AdsPower：默认 API 为 `http://127.0.0.1:50325`，启用鉴权时还需 API Key。
- 其他指纹浏览器：`FINGERPRINT_BROWSER=custom_api` 并设置 `CUSTOM_BROWSER_API`；当前要求兼容 BitBrowser 的 `/browser/update|open|close|delete|list` 协议。

在 `.env` 中用 `FINGERPRINT_BROWSER=bitbrowser|bundled|custom|adspower|custom_api` 切换。默认使用 BitBrowser；所有网页注册流程统一通过 Chromium CDP 自动化。

### 网络出口

WebUI 左侧“网络出口”页提供三种互斥模式：

| 模式 | `PROXY_MODE` | 行为 |
|---|---|---|
| 自动轮换 | `clash_auto` | 从 Clash 代理组选择可响应节点，注册流程可按失败或批次继续换节点 |
| 固定节点 | `clash_fixed` | 始终强制使用 `CLASH_FIXED_NODE`，脚本传入其他节点也不会覆盖 |
| 动态住宅 IP | `residential` | 使用单个住宅代理或持久轮换代理池，可调用供应商换 IP 接口 |

五个平台可覆盖全局模式：`OUTLOOK_PROXY_MODE`、`CLAUDE_PROXY_MODE`、`CHATGPT_PROXY_MODE`、`GROK_PROXY_MODE`、`KIRO_PROXY_MODE`。值为 `inherit`、`clash_auto`、`clash_fixed` 或 `residential`。例如：

```env
PROXY_MODE=clash_auto
OUTLOOK_PROXY_MODE=clash_auto
CLAUDE_PROXY_MODE=residential
CHATGPT_PROXY_MODE=residential
GROK_PROXY_MODE=residential
REG_FACTORY_PROXY=http://user:pass@host:port
```

Claude 的登录、验证码和 magic-link 验证必须保持同一个出口 IP。住宅模式请在代理供应商控制台生成 Sticky/粘性会话 endpoint；按请求轮换 IP 的 endpoint 会在 Claude 任务启动时被检测并拒绝。多个 Sticky endpoint 可写入 `REG_FACTORY_PROXY_POOL`，提交邮箱前被风控时程序会先轮换代理池，再创建新的浏览器 profile。只有在确认供应商通过其他机制保持会话时，才设置 `CLAUDE_ALLOW_ROTATING_PROXY=true` 跳过检测。

编排器会为每个平台创建独立子进程环境；住宅代理认证直接写入该平台新建的浏览器 profile。网络页的“轮换/测试目标”可逐个平台验证出口 IP。

Clash 模式先安装 [Clash Verge 2.5.2 Windows x64](https://github.com/clash-verge-rev/clash-verge-rev/releases/download/v2.5.2/Clash.Verge_2.5.2_x64-setup.exe)。在 Clash Verge 的“设置”中进入 Clash 设置或内核设置，找到“外部控制器”或 External Controller，按下面顺序配置：

1. 启用 External Controller，并填仅本机监听地址，例如 `127.0.0.1:9097`。面板的控制器地址填写为 `http://127.0.0.1:9097`。mihomo 常见默认端口是 `9090`。
2. 在同一页设置 Secret、Controller Secret 或 API Secret。将完全相同的值写入 `CLASH_SECRET`。未设置密码时，`CLASH_SECRET` 也必须留空。
3. 找到 mixed-port 或混合端口，例如 `7897`，写入 `CLASH_PROXY=http://127.0.0.1:7897`。
4. 保存后应用设置或重载内核，再在网络页点击“应用并测试 IP”。外部控制器只应监听 `127.0.0.1`，不要公开到网络。

默认值：

```env
CLASH_API=http://127.0.0.1:9097
CLASH_PROXY=http://127.0.0.1:7897
CLASH_GROUP=GLOBAL
CLASH_SECRET=
```

固定节点模式额外配置：

```env
PROXY_MODE=clash_fixed
CLASH_FIXED_NODE=美国 01
```

Plus Codex 导入只处理已经开通的账号，不会提取优惠链、绑卡或发起支付。每个账号登录后都会进入手机号接码验证阶段，验证成功后才继续 Codex OAuth 和 SUB2API 导入。WebUI 的 Plus 导入页支持批量账号，并可选择接码平台、换号次数、等待时间、并发数、ChatGPT 节点和 SUB2API 分组。

账号行支持 Outlook、iCloud、ChatGPT session token 和完整 Codex OAuth JSON。Outlook RT 与 client_id 顺序均可，也兼容 Hotmail/Live/MSN、两段/三段记录、Tab、逗号、竖线、分号及 `email:password`：
```text
email@outlook.com----password----refresh_token----client_id
email@outlook.jp----password----client_id----refresh_token
email@hotmail.com----password----refresh_token
email@live.jp:password
email@icloud.com
__Secure-next-auth.session-token=...
{"email":"...","access_token":"...","refresh_token":"...","plan_type":"plus","codex_phone_status":"verified"}
```
Outlook Graph RT/client_id 可用时优先通过 Graph 读取 OpenAI 邮箱验证码，否则使用账号密码在浏览器登录 Outlook 取码。iCloud 地址使用已配置的 `ICLOUD_MAIL_*` API。原始 ChatGPT session cookie 会先验证登录态，再走手机号验证和 Codex OAuth；普通短期 access token 不能直接换出 refresh token，会明确拒绝。完整 OAuth JSON 仅在自身带 `codex_phone_status=verified` 时允许直接导入。

住宅代理支持 `http`、`https`、`socks4` 和 `socks5`；BitBrowser 窗口支持 `http`、`https` 和 `socks5`。代理池优先于单个代理；`.env` 中用逗号分隔，WebUI 中可每行填写一个：

```env
PROXY_MODE=residential
REG_FACTORY_PROXY=http://user:pass@host:port
REG_FACTORY_PROXY_POOL=http://user:pass@host-a:8000,http://user:pass@host-b:8000
REG_FACTORY_PROXY_ROTATE_URL=https://provider.example/rotate
REG_FACTORY_PROXY_ROTATE_METHOD=GET
```

`REG_FACTORY_PROXY_ROTATE_URL` 可留空。点击“立即轮换”时，程序会推进代理池索引，并在配置后调用供应商接口。当前池索引保存在 `runtime/state/residential_proxy_index.txt`。

新建浏览器 profile 时会写入当前住宅代理的地址、端口、用户名和密码。代理池轮换后，下一个新建窗口使用新的代理；已经打开的注册窗口不会被强制改 IP，以免破坏当前登录会话。

旧变量 `RESIDENTIAL_PROXY`、`DIRECT_PROXY` 和 `RESIDENTIAL_PROXY_POOL` 仍兼容；新配置应使用 `REG_FACTORY_*`。

### Python 与 Node.js

- Python 3.10+ 是主流程必需依赖。
- Node.js 20+ 只在构建或运行 Codex K12 时需要。
- Gmail Android 流程还需要 BlueStacks、ADB、Appium 2.x 和 UiAutomator2 driver。

## 创建配置

Windows：

```powershell
Copy-Item .env.example .env
```

macOS / Linux：

```bash
cp .env.example .env
```

真实进程环境变量优先于 `.env`。WebUI 保存配置后，新任务立即使用新值，不需要重启主服务。

本地邮箱/Cookie 读取接口默认只允许回环地址调用。需要由其他本机服务统一携带密钥时，设置 `REG_FACTORY_ASSET_API_KEY`，并使用 `X-API-Key` 或 Bearer Token。ChatGPT 健康扫描默认通过 `ASSET_SCAN_CHATGPT_PLUS_TRIAL=true` 标注 Plus 免费试用或明确 0 元优惠，活动标识由 `ASSET_SCAN_CHATGPT_PLUS_CAMPAIGN` 控制；完整接口见 [本地资产 API](api.md)。

## 配置分组

`.env.example` 是全部配置项和默认值的唯一完整清单。通常只需要填写当前流程涉及的分组。

| 分组 | 常用变量 | 使用场景 |
|---|---|---|
| 浏览器 | `FINGERPRINT_BROWSER`、`CUSTOM_BROWSER_*`、`BITBROWSER_API`、`ADSPOWER_*` | 浏览器注册流程 |
| 网络出口 | `PROXY_MODE`、`CLASH_*`、`REG_FACTORY_PROXY*` | Clash 节点或住宅代理 |
| Claude 验证 | `CLAUDE_VISION_*`、`CLAUDE_HCAPTCHA_*` | Claude 图形验证 |
| 通用视觉 | `VISION_*`、`VOTE_*`、`IMAGE_EDIT_*` | 多模型视觉投票 |
| ChatGPT iCloud 邮箱 | `CHATGPT_EMAIL_PROVIDER`、`ICLOUD_MAIL_*` | ChatGPT 不使用 Outlook 池时 |
| 临时邮箱 | `YYDS_API_KEY` 等 provider 配置 | Claude/Grok 不使用 Outlook 池时 |
| 接码 | `SMSMAN_*`、`SMS_API_NAME`、`SMS_TOKEN`、`HERO_SMS_*` | 手机验证；firefox.fun 使用 APIName 标识账号，token + 项目 ID 调用接口 |
| SUB2API | `SUB2API_*` | Codex / Grok 下游导入 |
| CPA | `CPA_URL`、`CPA_MGMT_KEY` | Codex 凭据导入 |
| chatgpt2api | `CHATGPT2API_URL`、`CHATGPT2API_KEY` | 普通 ChatGPT 网页号导入 |

密钥必须留在 `.env` 或进程环境变量中。不要把真实值写进 `.env.example`、README、测试和截图。

ChatGPT 使用 iCloud 邮箱时，将 `CHATGPT_EMAIL_PROVIDER=icloud`，并填写 `ICLOUD_MAIL_API_KEY`。默认接口地址为 `https://mail.no-replyca.xyz`；`email.manageh.shop` 仅是接口文档站。`ICLOUD_MAIL_TYPE=icloud-code`、`ICLOUD_MAIL_SERVICE=openai` 用于申请 ChatGPT 接码邮箱；需要普通 iCloud 子邮箱时改用 `ICLOUD_MAIL_TYPE=icloud`，对应 `/api/user/email?type=icloud&apikey=...`。程序随后轮询 `/api/user/mail` 获取验证码。`ICLOUD_MAIL_API_BASE` 也兼容直接填写完整的 `/api/user/email?...` 地址。

> **重要：提取 Graph RT 必须配置可接收验证码的辅助邮箱。**
>
> Outlook Graph 提取遇到 Microsoft `proofs/Add` 安全信息页时，默认使用 `OUTLOOK_GRAPH_RECOVERY_PROVIDER=yyds` 创建辅助临时邮箱，提交地址后轮询验证码并完成绑定。要用自定义邮箱则设 `OUTLOOK_GRAPH_RECOVERY_PROVIDER=custom`，并沿用 `CUSTOM_MAIL_*` 配置；也可以设置 `OUTLOOK_GRAPH_RECOVERY_PROVIDER=outlook` 使用自有 Outlook 辅助邮箱。自有邮箱格式为 `email@outlook.com----password----refresh_token----client_id`，程序会先通过 Graph API 验证 refresh token，再从 Inbox/Junk 轮询 Microsoft 安全码。也可写成 `yyds,outlook` 按顺序做故障转移。设为 `OUTLOOK_GRAPH_RECOVERY_EMAIL=false` 可恢复旧的跳过行为，但不保证能提取 RT。

## 连通性检查

推荐从 WebUI 的“环境配置”页面执行测试。命令行也可检查 Clash：

```bash
python -m common.proxy_switch list
python -m common.proxy_switch current
python -m common.proxy_switch rotate
python _clash_verge.py ping
```

配置问题的常见表现和处理方式见 [常见问题](troubleshooting.md)。
