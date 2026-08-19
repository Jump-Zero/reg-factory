# CLI 手册

所有命令默认从仓库根目录执行。WebUI 使用同一组脚本和参数。

## 主流程

端到端注册：

```bash
python run_full_flow.py
python run_full_flow.py --platforms claude chatgpt grok
python run_full_flow.py --rounds 12 --concurrency 3 --platforms claude chatgpt kiro
python run_full_flow.py --platforms grok --grok-sub2api
python run_full_flow.py --platforms kiro
python run_full_flow.py --platforms chatgpt --import-c2a
python run_full_flow.py --skip-email --email a@outlook.com --password xxx
python run_full_flow.py --dry-run
```

端到端流程默认并行运行同一邮箱的所选平台，`--concurrency` 控制同时处理的邮箱数。
住宅代理模式会为并发邮箱分配独立出口，代理池不足时自动降低并发；同一邮箱的各平台共享该邮箱出口。
需要逐个平台执行时添加 `--sequential-platforms`。住宅省流策略由
`REG_FACTORY_RESIDENTIAL_TRAFFIC_MODE` 统一继承，设为 `extreme` 可启用极致省流。

使用已有邮箱池：

```bash
python register_three_platforms.py --from-pool
python register_three_platforms.py --email a@outlook.com --password xxx --token <refresh_token>
python register_three_platforms.py --loop --parallel --max-inflight 3
```

并发登录同一邮箱时，先启动共享取码服务：

```bash
python mailbox_broker.py --port 8765
```

## 单个平台

```bash
# ChatGPT：自动选择国家，或只使用实际出口为日本的网络节点
python register_chatgpt.py --count 1 --node auto
python register_chatgpt.py --count 1 --node auto --country JP

# 已开通 Plus 账号：手机号接码验证 -> Codex OAuth -> SUB2API
python tools/import_plus_codex.py --accounts-file accounts.txt --sms-provider auto --phone-attempts 3

# Grok 指纹浏览器流程
python register_grok.py --count 1
python register_grok.py --count 1 --sub2api --sub2api-group grok
python register_grok.py --count 1 --node auto --latest-rt

# Grok 使用 Remail（需配置 GROK_USE_TEMP_EMAIL=true 和 Remail 项目）
python register_grok.py --count 1

# Claude 使用最新 Outlook refresh token
python register.py --count 1 --node auto --latest-rt

# Claude 使用 YYDS 临时邮箱
python register.py --count 1 --node auto --provider yyds

# Claude 使用 Remail
python register.py --count 1 --node auto --provider remail

# Claude 指定 Outlook；refresh token 与 client_id 必须配套
python register.py --email a@outlook.com --password xxx --token <refresh_token> --client-id <client_id> --node auto

# Kiro Builder ID；默认从 Outlook 资产池读取 Graph refresh token
python register_kiro.py --count 1
python register_kiro.py --email a@outlook.com --refresh-token <refresh_token> --client-id <client_id>

# 使用 Remail 等配置的临时邮箱（TEMP_EMAIL_PROVIDER=remail）
python register_kiro.py --count 1 --email-provider temp

# 使用自建 REST 邮箱（CUSTOM_MAIL_*）
python register_kiro.py --count 1 --email-provider custom

# ChatGPT 使用 Remail
python register_chatgpt.py --count 1 --email-provider remail
```

## Outlook

```bash
# 持续注册；默认不按总次数停止，最近 20 次成功率低于 10% 时停止
python outlook_reg_loop.py

# 可选硬上限：最多尝试 20 次后退出
python outlook_reg_loop.py --count 20

# 自定义成功率熔断：最近 30 次低于 20% 时停止
python outlook_reg_loop.py --min-success-rate 20 --success-rate-window 30

# 关闭成功率熔断（不建议长期无人值守）
python outlook_reg_loop.py --min-success-rate 0

# 住宅代理池并发；最终并发还受 REG_FACTORY_MAX_CONCURRENCY 和池大小约束
python outlook_reg_loop.py --concurrency 5

# 固定当前节点
python outlook_reg_loop.py --no-rotate

# 批量解锁
python unlock_outlook.py --input accounts.txt --concurrency 2

# 解锁 Outlook 并提取 Graph refresh token
python unlock_outlook.py --input outlook_accounts/accounts.txt
python unlock_outlook.py
```

邮箱池格式为：

```text
email----password----refresh_token----client_id
```

Outlook 的 `--timeout` 只限制注册阶段；Graph 授权使用独立的 `OUTLOOK_GRAPH_AUTH_TIMEOUT`。验证码控件消失不等于账号已经创建，程序只有在离开注册表单并进入明确的 Microsoft 后续页面后才会导出账号。遇到账号不存在、密码登录不可用或登录次数过多时，Graph 授权会停止提交凭据，避免继续触发临时限流。

## Codex OAuth 与下游导入

```bash
# 默认使用最新 ChatGPT Cookie，自动处理 add-phone
python oauth_codex.py

# 手动填写手机号并保留浏览器
python oauth_codex.py --manual-phone --keep

# 使用 CPA 生成授权地址并接收 OAuth callback（无需 SUB2API 登录）
python oauth_codex.py --auth-url-source cpa

# 注册后直接授权
python run_full_flow.py --platforms chatgpt --codex
```

补传已落盘 Token：

```bash
python tools/upload_tokens.py
python tools/upload_tokens.py chatgpt
python tools/upload_tokens.py grok
# 强制重新导入 Grok 到 SUB2API，可修复已标记上传但返回 401 的账号
python tools/upload_tokens.py grok --force
```

## 导出与校验

```bash
# 导出浏览器扩展可用的账号 Cookie
python tools/export_accounts.py
python tools/export_accounts.py claude chatgpt

# 导出指定平台、指定账号的标准浏览器 Cookie JSON
python tools/export_accounts.py --platform claude --format cookies --index 0

# 聚合导出 hank9999/kiro.rs 可直接读取的 credentials.json
python tools/export_kiro_credentials.py
python tools/export_kiro_credentials.py --output D:\\kiro.rs\\credentials.json

# 导出或上传普通 ChatGPT 网页号
python tools/export_chatgpt2api.py
python tools/export_chatgpt2api.py --json
python tools/export_chatgpt2api.py --post https://<host> --key <admin_key>

# 校验 Claude sessionKey
python tools/validate_keys.py cookies/accounts.txt
```

普通 ChatGPT 网页 session 没有可续期的 `refresh_token`；正式 Codex 凭据应使用 `oauth_codex.py` 获取。

`oauth_codex.py` 默认使用 SUB2API 生成授权地址。使用 CPA 模式前配置 `CPA_URL`、`CPA_MGMT_KEY`；CPA 会负责 PKCE、换码和凭据落盘。

## Gmail Android

Gmail 流程需要额外的本地 Android 环境，见 [Gmail Android 本地环境](gmail-android.md)。
