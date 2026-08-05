# CLI 手册

所有命令默认从仓库根目录执行。WebUI 使用同一组脚本和参数。

## 主流程

端到端注册：

```bash
python run_full_flow.py
python run_full_flow.py --platforms claude chatgpt grok
python run_full_flow.py --platforms grok --grok-sub2api
python run_full_flow.py --platforms kiro
python run_full_flow.py --platforms chatgpt --import-c2a
python run_full_flow.py --skip-email --email a@outlook.com --password xxx
python run_full_flow.py --dry-run
```

使用已有邮箱池：

```bash
python register_three_platforms.py --from-pool
python register_three_platforms.py --email a@outlook.com --password xxx --token <refresh_token>
python register_three_platforms.py --loop
```

并发登录同一邮箱时，先启动共享取码服务：

```bash
python mailbox_broker.py --port 8765
```

## 单个平台

```bash
# ChatGPT
python register_chatgpt.py --count 1 --node auto

# 注册成功后加入本地 zkky Plus 工作台；支持批量 AT 和一次填卡后自动批处理
python register_chatgpt.py --count 1 --node auto --plus-subscription

# Grok 指纹浏览器流程
python register_grok.py --count 1
python register_grok.py --count 1 --sub2api --sub2api-group grok
python register_grok.py --count 1 --node auto --latest-rt

# Claude 使用最新 Outlook refresh token
python register.py --count 1 --node auto --latest-rt

# Claude 使用 YYDS 临时邮箱
python register.py --count 1 --node auto --provider yyds

# Claude 指定 Outlook；refresh token 与 client_id 必须配套
python register.py --email a@outlook.com --password xxx --token <refresh_token> --client-id <client_id> --node auto

# Kiro Builder ID；默认从 Outlook 资产池读取 Graph refresh token
python register_kiro.py --count 1
python register_kiro.py --email a@outlook.com --refresh-token <refresh_token> --client-id <client_id>
```

## Outlook

```bash
# 常驻注册
python outlook_reg_loop.py

# 注册指定数量后退出
python outlook_reg_loop.py --count 20

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

## Codex OAuth 与下游导入

```bash
# 默认使用最新 ChatGPT Cookie，自动处理 add-phone
python oauth_codex.py

# 手动填写手机号并保留浏览器
python oauth_codex.py --manual-phone --keep

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

# 导出或上传普通 ChatGPT 网页号
python tools/export_chatgpt2api.py
python tools/export_chatgpt2api.py --json
python tools/export_chatgpt2api.py --post https://<host> --key <admin_key>

# 校验 Claude sessionKey
python tools/validate_keys.py cookies/accounts.txt
```

普通 ChatGPT 网页 session 没有可续期的 `refresh_token`；正式 Codex 凭据应使用 `oauth_codex.py` 获取。

## Gmail Android

Gmail 流程需要额外的本地 Android 环境，见 [Gmail Android 本地环境](gmail-android.md)。
