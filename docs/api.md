# 本地资产 API

主 WebUI 提供只读资产接口，用于按顺序或指定下标读取邮箱与已注册平台凭据。默认地址为 `http://127.0.0.1:8799`。每个读取请求都会在返回前在线扫描对应平台，只会从本次检测为 `normal` 的健康资产池输出数据。

控制台左侧打开“资产 API”，可以配置 API Key、选择平台和输出格式、生成 `curl` 命令、在线调用并重置游标；下面的接口也可供其他本地程序直接调用。

## 鉴权

未配置 `REG_FACTORY_ASSET_API_KEY` 时，接口只接受本机请求。配置后，请求必须携带其中一种请求头：

```text
X-API-Key: your-key
Authorization: Bearer your-key
```

不要把 WebUI 监听到公网；这些接口返回邮箱密码、refresh token、Cookie 或平台 token。

## 邮箱

```bash
# 按 emails.txt 顺序取下一个，并推进邮箱游标
curl http://127.0.0.1:8799/api/assets/emails

# 精确读取第 3 条，不推进游标
curl "http://127.0.0.1:8799/api/assets/emails?index=2"

# 返回原始四段文本
curl "http://127.0.0.1:8799/api/assets/emails?format=line"
```

`format=json` 返回 `email`、`password`、`refresh_token`、`client_id`；`format=line` 返回原始 `----` 分隔文本。响应还包含 `verification`，其中的 `checked_at` 和 `evidence` 表示本次在线检测时间与判定依据。

## 平台 Cookie 与下游格式

```bash
# Claude 有效 Cookie 数组
curl "http://127.0.0.1:8799/api/assets/cookies/claude?format=raw"

# 指定 Claude 第 1 个账号，输出浏览器扩展标准 Cookie JSON
curl "http://127.0.0.1:8799/api/assets/cookies/claude?format=cookies&index=0"

# 浏览器 Cookie 请求头
curl "http://127.0.0.1:8799/api/assets/cookies/chatgpt?format=header&index=0"

# ChatGPT -> SUB2API 导入内容
curl "http://127.0.0.1:8799/api/assets/cookies/chatgpt?format=sub2api"

# ChatGPT -> CPA codex 授权 JSON
curl "http://127.0.0.1:8799/api/assets/cookies/chatgpt?format=cpa"

# ChatGPT -> chatgpt2api account
curl "http://127.0.0.1:8799/api/assets/cookies/chatgpt?format=chatgpt2api"

# Grok -> SUB2API SSO 请求体
curl "http://127.0.0.1:8799/api/assets/cookies/grok?format=sub2api"

# Kiro Builder ID 账号凭据
curl "http://127.0.0.1:8799/api/assets/cookies/kiro?format=session"
```

支持的平台与格式：

| 平台 | 格式 |
|---|---|
| Claude | `cookies`、`raw`、`header` |
| ChatGPT | `cookies`、`raw`、`header`、`session`、`sub2api`、`cpa`、`chatgpt2api` |
| Grok | `cookies`、`raw`、`header`、`session`、`sub2api` |
| Kiro | `session` |

`cookies` 是浏览器扩展通用导入数组，包含 `domain`、`hostOnly`、`httpOnly`、`name`、`path`、`sameSite`、`secure`、`session`、`storeId`、`value`，持久 Cookie 额外包含 `expirationDate`。`raw` 保留注册脚本保存的原始字段，供旧调用兼容。

响应中的 `index` 是本次下标，`total` 是当前健康资产池总数，`next_index` 是下一下标。省略 `index` 会推进对应的独立游标；指定 `index` 只读取该条，不改变游标。封禁、过期、受限、凭据异常和未验证资产会被拦截。在线检测只能说明检测时刻可用，不能保证目标服务之后不会限制账号。

## 号池状态扫描

扫描任务在 WebUI 后台运行，不阻塞其他 API。支持的平台是 `outlook`、`chatgpt`、`claude`、`grok`、`kiro`。

```bash
# 读取当前号池明细、上次结果和正在运行的扫描进度
curl http://127.0.0.1:8799/api/assets/scan

# 一键扫描全部号池
curl -X POST http://127.0.0.1:8799/api/assets/scan \
  -H "Content-Type: application/json" \
  -d '{"platforms":["outlook","chatgpt","claude","grok","kiro"],"concurrency":4,"timeout":15}'

# 只扫描 Outlook 邮箱
curl -X POST http://127.0.0.1:8799/api/assets/scan \
  -H "Content-Type: application/json" \
  -d '{"platforms":["outlook"],"concurrency":2}'
```

扫描状态：

| 状态 | 含义 |
|---|---|
| `normal` | 官方会话、OAuth 或邮箱访问验证正常 |
| `unlock` | Outlook 明确返回锁定、补充验证，或历史扫描确认需要解锁 |
| `banned` | 官方响应明确表示账号停用/封禁，或 Outlook 历史结果为 dead/abuse lock |
| `expired` | Cookie、session、SSO 或 refresh token 已过期/撤销 |
| `restricted` | HTTP 403、限流、Cloudflare 或出口风控，不能据此判定账号封禁 |
| `invalid` | 本地资产缺少平台关键凭据或文件结构无效 |
| `unknown` | 尚未扫描，或缺少足够证据确认状态 |
| `error` | 请求超时、网络失败或官方服务异常 |

GET 响应中的 `summary` 是全号池统计，`items` 是逐条结果，`scan.progress` 是当前任务进度。重复启动扫描会返回 HTTP 409。

结果保存在 `runtime/state/asset_pool_scan.json`，只包含账号标识、状态、判定依据、来源文件名和检测时间，不包含密码、refresh token、Cookie、access token、sessionKey 或 SSO。

## 状态与重置

```bash
curl http://127.0.0.1:8799/api/assets/summary

# 重置全部顺序游标
curl -X POST http://127.0.0.1:8799/api/assets/cursors/reset \
  -H "Content-Type: application/json" -d '{"scope":"all"}'

# 只重置 ChatGPT CPA 健康资产游标
curl -X POST http://127.0.0.1:8799/api/assets/cursors/reset \
  -H "Content-Type: application/json" -d '{"scope":"verified:cookie:chatgpt:cpa"}'
```

游标保存在 `runtime/state/asset_api_cursors.json`；它不会修改 `emails.txt`、Cookie 或 Token 文件。
