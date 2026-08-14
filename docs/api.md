# 本地资产 API

主 WebUI 提供资产领取接口，用于按顺序或指定下标读取邮箱与已注册平台凭据。默认地址为 `http://127.0.0.1:8799`。每个读取请求直接从本地尚未领取的资产中输出数据，不会先调用在线状态扫描。接口不修改原始邮箱、Cookie 或 Token 文件，只持久化领取标识。

控制台左侧打开“资产 API”，可以配置 API Key、选择平台和输出格式、生成 `curl` 命令、在线调用并重置领取记录；下面的接口也可供其他本地程序直接调用。

## 鉴权

未配置 `REG_FACTORY_ASSET_API_KEY` 时，接口只接受本机请求。配置后，请求必须携带其中一种请求头：

```text
X-API-Key: your-key
Authorization: Bearer your-key
```

不要把 WebUI 监听到公网；这些接口返回邮箱密码、refresh token、Cookie 或平台 token。

## 邮箱

```bash
# 领取下一个未领取邮箱
curl http://127.0.0.1:8799/api/assets/emails

# 领取当前未领取列表中的第 3 条
curl "http://127.0.0.1:8799/api/assets/emails?index=2"

# 返回原始四段文本
curl "http://127.0.0.1:8799/api/assets/emails?format=line"

# 只领取 iCloud 注册邮箱
curl "http://127.0.0.1:8799/api/assets/emails?format=json&email_provider=icloud"

# 只领取最近一次号池扫描中状态为正常的邮箱
curl "http://127.0.0.1:8799/api/assets/emails?normal_only=true"
```

`format=json` 返回 `email`、`password`、`refresh_token`、`client_id`；`format=line` 返回原始 `----` 分隔文本。默认领取不读取扫描状态，但会永久排除已经被任意平台注册领取、尝试或成功使用的邮箱，防止同一 Outlook 邮箱再被单独售卖。设置 `normal_only=true` 后只使用最近一次号池扫描缓存筛选，并在响应中返回 `verification`；领取请求本身仍不会联网检测。没有缓存为正常且尚未领取的邮箱时返回 HTTP 409。

邮箱与平台资产响应都会包含 `email_provider`：`outlook`、`icloud`、`temporary` 或 `other`。可用 `email_provider` 查询参数按注册邮箱来源筛选。

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

# ChatGPT -> 只领取 Outlook 注册账号
curl "http://127.0.0.1:8799/api/assets/cookies/chatgpt?format=sub2api&email_provider=outlook"

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

响应中的 `index` 是本次在未领取列表中的下标，`total` 是领取前可用总数，`remaining` 是领取后的剩余数量，`claim_recorded=true` 表示领取记录已持久化。省略 `index` 时选择第一条；指定 `index` 时从当前未领取列表中选择。两种方式都会记录领取。同一平台账号按邮箱或来源文件识别，切换 `raw`、`cookies`、`session`、`sub2api`、`cpa`、`chatgpt2api` 等格式也不会重复返回。除邮箱显式设置 `normal_only=true` 外，领取接口不读取上次扫描结论；所有领取请求都不会在请求时联网检测。

## ChatGPT 注册国家与 Plus 优惠

ChatGPT 注册可通过 `--country JP` 等两位 ISO 国家码约束出口。脚本会在浏览器启动前校验 Cloudflare `loc`，找不到匹配网络则停止；成功后在 session 和号池扫描结果中写入 `registration_country` 与 `network_node`。

扫描 ChatGPT 账号时会额外调用只读优惠资格接口，并在号池扫描结果中写入 `plus_trial`、`plus_trial_detail`、`plus_trial_evidence`。该检测不创建结账单、不领取优惠、不绑卡、不扣款；失败只标记为 `unknown`，不会改变账号的健康状态。

| `plus_trial` | 含义 |
|---|---|
| `eligible` | 活动接口明确返回可使用 Plus 免费试用 |
| `zero_price` | 活动接口返回明确的应付 0 元或格式化 0 元价格 |
| `ineligible` | 活动接口明确返回不符合、已领取或已过期 |
| `active` | 本地会话表明账号已有 Plus 或其他付费套餐 |
| `unknown` | 缺少 AT、网络失败或接口没有返回明确资格 |
| `disabled` | 已通过配置关闭资格检测 |

默认检测活动为 `plus-1-month-free`。可通过 `ASSET_SCAN_CHATGPT_PLUS_TRIAL=false` 关闭，或用 `ASSET_SCAN_CHATGPT_PLUS_CAMPAIGN` 修改活动标识。检测结果只用于标记，最终优惠仍以用户自行打开的官方结账页为准。

## 号池状态扫描

扫描任务在 WebUI 后台运行，不阻塞其他 API，也不是默认领取的前置条件。支持的平台是 `outlook`、`chatgpt`、`claude`、`grok`、`kiro`。安全扫描模式下，同一平台账号始终串行，每个账号请求前随机等待 3–6 秒，默认复用 6 小时内的结果；出现 429 会立即暂停该平台，连续两次 403/挑战页或网络异常也会暂停，剩余记录标记为 `unknown`。`concurrency` 只控制不同平台的并行数，默认 1、上限 2。扫描依据检测时的官方接口与 HTTP 响应作尽力判断：明确的成功、撤销或停用响应可信度较高，但普通 403、超时和连接失败可能来自出口、地区或目标服务风控，不能据此断言账号永久失效。

```bash
# 读取当前号池明细、上次结果和正在运行的扫描进度
curl http://127.0.0.1:8799/api/assets/scan

# 一键扫描全部号池
curl -X POST http://127.0.0.1:8799/api/assets/scan \
  -H "Content-Type: application/json" \
  -d '{"platforms":["outlook","chatgpt","claude","grok","kiro"],"concurrency":1,"timeout":15}'

# 只扫描 Outlook 邮箱
curl -X POST http://127.0.0.1:8799/api/assets/scan \
  -H "Content-Type: application/json" \
  -d '{"platforms":["outlook"],"concurrency":1}'
```

确需忽略 6 小时缓存时，可在 POST JSON 中增加 `"force":true`。不要在短时间内反复强制扫描。等待间隔和缓存时长可分别通过 `ASSET_SCAN_MIN_INTERVAL`、`ASSET_SCAN_MAX_INTERVAL`、`ASSET_SCAN_CACHE_SECONDS` 调整。

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

GET 响应中的 `summary` 是全号池统计，`items` 是逐条结果，`scan.progress` 是当前任务进度，`safe_mode` 是最近一次扫描采用的限速和缓存参数。重复启动扫描会返回 HTTP 409。

结果保存在 `runtime/state/asset_pool_scan.json`，只包含账号标识、状态、判定依据、来源文件名和检测时间，不包含密码、refresh token、Cookie、access token、sessionKey 或 SSO。

## 状态与重置

```bash
curl http://127.0.0.1:8799/api/assets/summary

# 重置全部领取记录和兼容游标
curl -X POST http://127.0.0.1:8799/api/assets/cursors/reset \
  -H "Content-Type: application/json" -d '{"scope":"all"}'

# 只重置 ChatGPT 账号领取记录
curl -X POST http://127.0.0.1:8799/api/assets/cursors/reset \
  -H "Content-Type: application/json" -d '{"scope":"chatgpt"}'
```

领取账本保存在 `runtime/state/asset_api_claims.json`，只包含不可逆的 SHA-256 标识和平台范围，不保存邮箱或凭据。旧版兼容游标仍保存在 `runtime/state/asset_api_cursors.json`。重置不会修改 `emails.txt`、Cookie 或 Token 文件。
