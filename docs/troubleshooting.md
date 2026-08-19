# 常见问题

## WebUI 更新后仍是旧页面

使用 `update.bat` 或 `./update.sh`，不要只在后台服务运行时执行 `git pull`。更新脚本会检查任务、更新依赖、重启服务并核对版本。

## 指纹浏览器显示离线

确认客户端已启动并开启本地 API，再检查 `.env` 中的 `FINGERPRINT_BROWSER` 与对应 API 地址。`custom` 模式填写 `CUSTOM_BROWSER_PATH`；`custom_api` 默认兼容 BitBrowser 协议，也可设置 `CUSTOM_BROWSER_API_MODE=generic` 对接 REST API，并在 WebUI 配置 API Key、鉴权头和各操作路径。启动接口必须能返回 `ws`、`cdp`、`endpoint` 或 `debugPort` 之一。AdsPower 开启鉴权时还要填写 `ADSPOWER_API_KEY`。

## Clash 控制器连接失败

确认 External Controller 已启用，端口与 `CLASH_API` 一致，`CLASH_SECRET` 没有多余空格。可运行：

```bash
python _clash_verge.py ping
python -m common.proxy_switch list
```

## Claude 出现地区限制或安全验证

使用 `--node auto` 让流程探测可用节点。图形验证需要正确配置 `CLAUDE_VISION_API_BASE` 和 `CLAUDE_VISION_API_KEY`；需要人工接管时可设置 `CLAUDE_CAPTCHA_MANUAL_TIMEOUT=180`。

## Claude 提示 service abuse 或没有可用 RT 邮箱

`AADSTS70000` 和 `service abuse mode` 来自 Microsoft，表示该 Outlook 账号的 Graph refresh token 已不可用，不是 Claude 节点错误。新版会把这类邮箱写入 `emails_error_claude.txt`，避免每次重复检测。请导入 Graph RT 正常的新邮箱，或在 Claude 任务中选择已配置的临时邮箱来源。

## 注册任务关闭后仍在循环

在运行日志右上角点击“停止全部”。它会结束当前任务树，并扫描清理旧 WebUI 遗留的 reg-factory 注册任务；WebUI、BitBrowser 和 Clash 不会被关闭。

## Grok 返回 Cloudflare 403

更换干净节点或为 Grok 单独选择住宅出口。`register_grok.py --node auto` 在 Clash 模式下会探测节点，住宅模式下会直接使用写入浏览器 profile 的代理。

## Outlook 并发取码冲突

多个平台同时登录同一邮箱前，启动共享取码服务：

```bash
python mailbox_broker.py --port 8765
```

## Refresh token 无法取码

确认 `refresh_token` 与签发它的 `client_id` 成对保存。邮箱池标准格式为：

```text
email----password----refresh_token----client_id
```

## 敏感数据误入 Git 状态

不要提交 `.env`、账号、Cookie、Token、日志或截图。先确认路径已在 `.gitignore`，再把文件移出 Git 暂存区；不要为了清理状态删除唯一的数据副本。
