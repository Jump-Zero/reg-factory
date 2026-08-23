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

# 按最近一次扫描状态领取；显式 status 会覆盖默认的正常筛选
curl "http://127.0.0.1:8799/api/assets/emails?status=normal"
curl "http://127.0.0.1:8799/api/assets/cookies/chatgpt?format=session&status=normal"
# 多个状态用逗号分隔
curl "http://127.0.0.1:8799/api/assets/emails?status=normal,error"
```

`format=json` 返回 `email`、`password`、`refresh_token`、`client_id`；`format=line` 返回原始 `----` 分隔文本；`format=four` 始终返回 `邮箱----密码----refresh_token----client_id` 四段。默认领取不读取扫描状态，但会永久排除已经被任意平台注册领取、尝试或成功使用的邮箱，防止同一 Outlook 邮箱再被单独售卖。设置 `normal_only=true` 或 `status=normal` 后只使用最近一次号池扫描缓存筛选，并在响应中返回 `verification`；`status` 支持 `normal`、`unlock`、`banned`、`expired`、`restricted`、`invalid`、`unknown`、`error`，多个状态用逗号分隔。显式 `status` 优先于默认正常筛选。领取请求本身仍不会联网检测。没有匹配状态且尚未领取的资产时返回 HTTP 409。

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

# ChatGPT -> Outlook 注册邮箱四段式
curl "http://127.0.0.1:8799/api/assets/cookies/chatgpt?format=email_four&email_provider=outlook"

# ChatGPT -> iCloud 注册邮箱
curl "http://127.0.0.1:8799/api/assets/cookies/chatgpt?format=email_four&email_provider=icloud"

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
| ChatGPT | `cookies`、`raw`、`header`、`session`、`email_four`、`sub2api`、`cpa`、`chatgpt2api` |
| Grok | `cookies`、`raw`、`header`、`session`、`sub2api` |
| Kiro | `session` |

## 批量文件导出

```bash
curl -X POST http://127.0.0.1:8799/api/assets/export \
  -H "Content-Type: application/json" \
  -d '{"resource":"emails","format":"four","limit":100,"consume":true}' \
  -o emails.zip

curl -X POST http://127.0.0.1:8799/api/assets/export \
  -H "Content-Type: application/json" \
  -d '{"resource":"chatgpt","format":"sub2api","limit":100,"consume":true}' \
  -o chatgpt-sub2api.zip
```

批量接口默认带 `normal_only=true`，只导出最近一次扫描缓存中 `status=normal` 的账号；导出请求本身不会联网扫描。ZIP 中每个账号保存为独立文件并附带不含凭据的 `manifest.json`。`consume` 默认是 `true`：ZIP 生成成功后，源记录会移动到 `runtime/assets/exported/`，因此不会再出现在活动号池；文件仍可人工恢复。消费式批量导出默认带 `include_claimed=true`，因此可排空仍在活动目录、但已被旧领取账本标记过的正常记录。设置 `consume=false` 时不会移动源文件，此时 `include_claimed` 默认也是 `false`。

`cookies` 是浏览器扩展通用导入数组，包含 `domain`、`hostOnly`、`httpOnly`、`name`、`path`、`sameSite`、`secure`、`session`、`storeId`、`value`，持久 Cookie 额外包含 `expirationDate`。`raw` 保留注册脚本保存的原始字段，供旧调用兼容。

响应中的 `index` 是本次在未领取列表中的下标，`total` 是领取前可用总数，`remaining` 是领取后的剩余数量，`claim_recorded=true` 表示领取记录已持久化。省略 `index` 时选择第一条；指定 `index` 时从当前未领取列表中选择。两种方式都会记录领取。同一平台账号按邮箱或来源文件识别，切换 `raw`、`cookies`、`session`、`sub2api`、`cpa`、`chatgpt2api` 等格式也不会重复返回。除邮箱显式设置 `normal_only=true` 外，领取接口不读取上次扫描结论；所有领取请求都不会在请求时联网检测。

## ChatGPT 注册国家与 Plus 优惠

ChatGPT 注册可通过 `--country JP` 等两位 ISO 国家码约束出口。脚本会在浏览器启动前校验 Cloudflare `loc`，找不到匹配网络则停止；成功后在 session 和号池扫描结果中写入 `registration_country` 与 `network_node`。

首次打开 ChatGPT 登录页默认单次等待 30 秒、最多 3 次。若超时时登录文档已经提交，流程直接继续；否则在 `auto` Clash 模式下切换到下一个通过探测的节点、断开旧连接并新建标签页，避免在同一条卡死连接上重复等待。可用 `CHATGPT_GOTO_TIMEOUT_SECONDS` 和 `CHATGPT_GOTO_ATTEMPTS` 调整该边界。

扫描 ChatGPT 账号时会额外调用只读优惠资格接口，并在号池扫描结果中写入 `plus_trial`、`plus_trial_detail`、`plus_trial_evidence`。该检测不创建结账单、不领取优惠、不绑卡、不扣款；失败只标记为 `unknown`，不会改变账号的健康状态。

| `plus_trial` | 含义 |
|---|---|
| `eligible` | 兼容旧缓存的活动标记，不代表已确认 0 元，不能进入协议号池 |
| `zero_price` | 活动接口返回明确的应付 0 元、格式化 0 元价格或 100% 折扣 |
| `discount` | 活动接口确认有折扣但低于 100%，不是 0 元 |
| `ineligible` | 活动接口明确返回不符合、已领取或已过期 |
| `active` | 本地会话表明账号已有 Plus 或其他付费套餐 |
| `unknown` | 缺少 AT、网络失败或接口没有返回明确资格 |
| `disabled` | 已通过配置关闭资格检测 |

默认检测活动为 `plus-1-month-free`。可通过 `ASSET_SCAN_CHATGPT_PLUS_TRIAL=false` 关闭，或用 `ASSET_SCAN_CHATGPT_PLUS_CAMPAIGN` 修改活动标识。检测结果只用于标记，最终优惠仍以用户自行打开的官方结账页为准。

## Plus 协议提链

Plus 导入页把“授权导入”“渠道选择”“批量协议提链”和“批量协议支付”放在一个任务面板中。OAuth 导入成功后可从本地 session 读取当前 AT；也可选择“号池有资格账号”，直接载入资产扫描缓存中标记为 `zero_price` 且存在可用本地 session 的账号。`eligible`、`discount` 和 `unknown` 不会进入协议号池。无论来源如何，协议任务都会逐账号实时复检优惠资格，未再次命中的账号不会进入提链或支付。

渠道目录包括 PayPal、GoPay、GCash、GrabPay、UPI、iDEAL、PIX、Kakao Pay、BLIK、TWINT、Direct Card Checkout 和 MoMo。BLIK 的上游协议不支持批量，因此在批量界面中明确禁用。除 PayPal 外，批量任务只生成支付链接或二维码；PayPal 可通过明确选择“批量协议支付”、勾选真实支付确认并再次确认弹窗后调用上游 `paypal_auto` 执行器。支付资料可仅在本次任务中录入，也可使用协议引擎已有的 `paypal_auto` 配置。任务输入文件以本机权限保护，子进程结束即删除；最终报告不保存 AT、Cookie、卡片、地址或接码 URL。

协议执行复用本机的 GPT-Register-Tool 引擎。默认自动查找同级 `GPT-Register-Tool` 目录；发布包或其他目录可在 `.env` 中设置 `REG_FACTORY_PROTOCOL_PAYMENT_ROOT`。Plus 提链默认跟随 ChatGPT 的 Clash 出口；需要其他出口时，通过 `REG_FACTORY_PLUS_LINK_ROUTE`、`REG_FACTORY_PLUS_BIND_ROUTE` 或对应的显式 proxy override 配置。

## 号池状态扫描

扫描任务在 WebUI 后台运行，不阻塞其他 API，也不是默认领取的前置条件。支持的平台是 `outlook`、`chatgpt`、`claude`、`grok`、`kiro`。安全扫描模式按小批次并行同平台账号，`account_concurrency` 默认 4、上限 8；不同平台并行数由 `concurrency` 控制，默认 1、上限 2。WebUI 默认只做健康扫描，设置 `include_plus_trial=true` 才额外请求 ChatGPT Plus 资格接口。每个账号请求前仍随机等待 3–6 秒，默认复用 6 小时内的结果；出现 429 会暂停下一批，连续两次 403/挑战页或网络异常也会暂停，剩余记录标记为 `unknown`。扫描确认 `banned`、`expired` 或 `invalid` 时默认把源记录移动到 `runtime/assets/quarantine/`；隔离归档在线程中执行，不阻塞 WebUI。`restricted`、`error`、`unknown` 和 `unlock` 不会自动移动。

```bash
# 读取当前号池明细、上次结果和正在运行的扫描进度
curl http://127.0.0.1:8799/api/assets/scan

# 一键扫描全部号池
curl -X POST http://127.0.0.1:8799/api/assets/scan \
  -H "Content-Type: application/json" \
  -d '{"platforms":["outlook","chatgpt","claude","grok","kiro"],"concurrency":1,"account_concurrency":4,"quarantine_bad":true,"include_plus_trial":false,"timeout":15}'

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
