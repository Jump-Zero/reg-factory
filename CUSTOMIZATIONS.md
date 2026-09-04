# 本仓库定制功能（Customizations）

本仓库基于上游 [tiantianGPU/reg-factory](https://github.com/tiantianGPU/reg-factory) 维护，在同步上游版本的同时叠加了以下本地定制功能。上游每次更新通过 `git merge upstream/main` 合并，本文档记录仅存在于本仓库的能力，便于合并时识别哪些是本地逻辑。

## 目录

- [LIYE 卡密式接码平台](#liye-卡密式接码平台)
- [账号封禁自动检测与隔离](#账号封禁自动检测与隔离)
- [资产账号查询导入 SUB2API](#资产账号查询导入-sub2api)
- [邮箱池管理增强](#邮箱池管理增强)
- [SUB2API 与 OmniRoute 集成增强](#sub2api-与-omniroute-集成增强)
- [接码平台扩展](#接码平台扩展)
- [Grok 定制](#grok-定制)
- [WebUI 定制](#webui-定制)
- [代理与批量任务控制](#代理与批量任务控制)

---

## LIYE 卡密式接码平台

接入 [liye.5x20.cn](https://liye.5x20.cn) 卡密式接码（无账号、纯卡密），新增 `common/liye_sms.py`：

- **一卡一次**：一张卡密取一次号收一次码；取消成功退回次数；平台内换号（replace）不耗卡密次数。
- **卡密双来源自动合并**：`.env` 中 `LIYE_CARDS`（逗号分隔）+ `runtime/state/liye_cards.txt`（每行一张，追加无需重启）。
- **卡密前缀识别服务**：`GPT-`/`CZ-` → chatai（OpenAI），`GOO-` → google（Gmail）。ChatGPT 取号固定 `service=chatai` 并自动跳过 `GOO-` 卡，避免错拿 Gmail 卡导致登录失败。
- **会话自动重登**：平台登录会话 10 分钟不活动过期，客户端自动重新登录。
- **国家随机分配 + 号段黑名单**：平台随机分配国家（整体成功率约 40%）；`LIYE_COUNTRY_BLACKLIST` 过滤黑名单号段，命中黑名单优先 replace 换号（不耗卡密）而非取消退卡。
- **卡池状态持久化**：`runtime/state/liye_cards.json`，状态流转 `available → in_use → exhausted`（收到码）/ `available`（取消退回）；崩溃遗留的 `in_use` 卡在租期（`LIYE_LEASE_SECONDS`，默认 1200s）后懒回收。
- **错误码语义化处理**：`CARD_ALREADY_USED` = 卡已用尽；`CANCEL_TOO_EARLY`/`REPLACE_TOO_EARLY` = 冷却（读 `order.cancelAvailableAt`）；`CONCURRENCY_LIMIT_REACHED` = 平台并发满；`CARD_LOGIN_REQUIRED`/`CARD_SESSION_INVALID` = 会话过期重登。
- **CLI 管理**：`python -m common.liye_sms status|stats|reset`（卡池概览 / 各国实时成功率 / 强制回收卡密）。

**接入位置**：

- `common/sms.py` 统一入口：`provider=liye` 指定使用；`provider=auto` 时配了卡密即纳入轮换（默认排最后兜底，`LIYE_AUTO_POSITION=first` 改为优先）；pkey 前缀 `liye_<order_id>` 路由。
- 覆盖全部 ChatGPT 接码入口：`register_chatgpt.py` / `run_full_flow.py` / `register_three_platforms.py` / `oauth_codex.py` / `tools/import_plus_codex.py` 的 CLI choices，以及 `webui/scripts.py` 5 处接码平台选项。
- WebUI「短信接码」配置组新增 LIYE 配置项与「测试 LIYE 卡池」按钮（只读统计，不耗卡密）。
- `gmail_android/config.py` 镜像 `common/sms.py` 与 `liye_sms.py` 的 LIYE 配置键，保证配置一致。

> 本仓库决策：LIYE 仅用于 ChatGPT 接码。gmail_android 未接入 liye，保持 firefox / smsman / hero 原链路。

## 账号封禁自动检测与隔离

在 ChatGPT OAuth 提链链路中增加封禁探测与处置（`common/oauth_codex.py`、`oauth_codex.py`、`tools/import_plus_codex.py`）：

- **三路探测**：页面 URL、页面正文、`/api/auth/session` 响应体；命中封禁标记（`account deactivated` / `deactivated` / `account disabled` / `account suspended` 等）即判定封禁。
- **处置动作**：打印 `[BAN]` 标记日志 → 将 cookie/token 移入 quarantine 隔离区（`runtime/lifecycle/quarantine/`）→ 单账号 CLI 返回退出码 `3`；批量导入结果标记 `banned: true` 并新增封禁计数。
- **新增工具**：`tools/confirm_codex_banned.py` 浏览器封禁确认工具（配合「账号健康」页的「浏览器确认并隔离」流程）。
- **重试保护**：ChatGPT 注册重试机制排除永久性错误（`account_deactivated` / `account_suspended` / `account_banned`），避免反复消耗验证码配额。

## 资产账号查询导入 SUB2API

WebUI「已开通 Plus 导入 SUB2API」页新增「资产账号查询」区块：

- `GET /api/chatgpt-plus/pending-accounts`：从 `asset_scanner.get_report()` 过滤 chatgpt 平台且 `sub2api_uploaded=false` 的账号，复用 Sub2API 数据库 API 的 60 秒缓存；服务端探测每账号可用凭据。
- **凭据解析优先级**：`oauth-*.session.json` → `*.session.json` → cookie 记录 → 邮箱池四段行（`_resolve_plus_account_record`）。
- `POST /api/chatgpt-plus/import-codex` 新增 `emails` 参数：与现有 `accounts` 文本合并去重；无法解析的账号跳过并在响应 `skipped` 中说明原因。
- 前端支持全选/清空、凭据类型/账号状态/Plus 资格标签展示，无凭据账号复选框禁用防误选。

## 邮箱池管理增强

- **子邮箱元数据**：`emails_meta.json` 独立于 `emails.txt` 存储邮箱分类（自主导入 / 自主注册）。
- **子邮箱分裂**：Outlook 母邮箱分裂子邮箱后写入 `emails.txt`，由 `next_email()` 按顺序读取未占用邮箱；`latest_email()` 在 `USE_LATEST_RT=True` 时选择最新的、有有效 refresh token 的未占用子邮箱。
- **WebUI 子邮箱展示**：子邮箱不独立占行，收纳在母邮箱行下点击展开；邮箱列表计数、分页、状态汇总仅计算顶层母邮箱行。
- **导入覆盖逻辑**：邮箱已存在且任一字段（密码/RT/client_id）不同则覆盖旧记录，完全一致则跳过；支持 `邮箱@outlook.com----密码----client_id----refresh_token` 反序格式。
- **Outlook 坏号自动隔离**：WebUI 发起的扫描自动隔离坏号（`quarantine_bad=true`），将相关邮箱行或凭据文件移入 `runtime/lifecycle/quarantine/outlook/<时间戳>/`。坏号 = 状态属于 `QUARANTINE_STATUSES=("banned","expired","invalid","unknown")`，其中 `unknown` 仅在 `local:missing_refresh_token` 或 `claude_account:no_membership` 时隔离；`unlock`/`restricted`/`error`/临时性 `unknown` 不隔离。
- **reserved 邮箱回收**：一键释放各平台注册未完成而锁死在 reserved 状态的邮箱（保留已成功 ok 记录）。
- **平台级删除**：chatgpt/claude/grok 平台下删除仅移除该平台记录，outlook 分类下删除为完全删除。

## SUB2API 与 OmniRoute 集成增强

- **导入状态查询**：优先调用 Sub2API 数据库 API `GET /api/v1/admin/accounts?platform={platform}`（60 秒缓存），不可达时回退到本地 `uploaded_sub2api.txt` 标记文件。
- **平台查询映射**：chatgpt 平台同时查询 `chatgpt` 与 `openai` 两个平台并合并结果；grok、claude 仅查询同名平台。
- **Grok Sub2API 导入**：支持 Grok 账号导入 SUB2API（含本地 sso 探测、重新授权导入、直接隔离）。
- **OmniRoute 导入**：号池视图支持勾选账号批量导入 OmniRoute。
- **GitHub 受限邮箱过滤**：邮箱池凭据查找时过滤 GitHub 受限邮箱。

## 接码平台扩展

在 sms-man → firefox.fun → hero-sms → liye 轮换链路之上：

- **sms-man 优先**：配置 `SMSMAN_TOKEN`、`SMSMAN_APP_ID_OPENAI` 等环境变量后优先使用。
- **hero-sms 增强**：价格上限（`HERO_SMS_MAXPRICE_OPENAI`，默认 1.0 美元，0 表示不限价）、价格下限配置；国家选择支持中文名 / 英文名 / ISO 码。
- **超时与重试控制**：接码超时 `CODEX_SMS_TIMEOUT`（默认 150 秒）、接码尝试次数 `CODEX_ADDPHONE_ATTEMPTS`。

## Grok 定制

- **已注册账号接管模式**：`feat(grok): 已注册账号接管`，复用已注册账号完成 OAuth 授权。
- **浏览器链路**：Grok 使用 Chromium CDP，需配置 `REG_FACTORY_BROWSER_PATH`；内置 Chromium 模式（`FINGERPRINT_BROWSER=bundled`）自动查找该路径，为空时依次尝试 `~/.cache/reg-factory/chromium/`、仓库 `chromium/` 目录、系统 PATH 中的 `chrome.exe`/`msedge.exe`。
- **验证码处理**：注册必须配置打码平台 API Key，按 YesCaptcha → CapSolver → EZ-Captcha 顺序尝试；验证码正则优化与邮箱日志。
- **临时邮箱支持**：`GROK_USE_TEMP_EMAIL=true` + `TEMP_EMAIL_PROVIDER=yyds` + `YYDS_API_KEY`，失败时自动回退到 emails.txt 邮箱池。

## WebUI 定制

- **悬浮通知**：`rf-toast-stack` 全局 toast 组件（注意：`index.html` 尾部该元素为本地独有，合并冲突时保留）。
- **账号健康页**：多平台 401 处置（GPT 免浏览器一键修复、Grok 本地 sso 探测、Claude 本地清点），定时巡检、平台/分类/状态筛选、批量修复/隔离/重新授权。
- **扫描增强**：扫描选中、平台并行 + 账号并行双层并发控制、扫描进度条、选中扫描进度展示。
- **健康检查模块**：`webui/health.py`，浏览器 / 网络出口 / Codex K12 / Plus 工作台状态独立轮询。
- **环境配置标签**：`webui/scripts.py` 的 `_ENV_LABELS` 为全部本地定制配置键补齐中文标签（新增配置项必须补标签，否则 KeyError 崩溃）。
- **静态资源版本**：本地改动通过 `?v=` 参数刷新缓存，合并后统一递增版本号。

## 代理与批量任务控制

- **动态住宅 IP**：`REG_FACTORY_PROXY` / `REG_FACTORY_PROXY_POOL`（格式 `socks5://用户名:密码@网关地址:端口`），需设置 `PROXY_MODE=residential` 并重启 WebUI；多任务并发时代理池条目使用不同会话 ID（sid）避免出口 IP 关联。
- **多端口代理模式**：`REG_FACTORY_PROXY_MODE=multiple` + `PORT_START`/`PORT_END`。
- **指纹浏览器选择**：`FINGERPRINT_BROWSER` 可选 `bitbrowser`（默认）/ `bundled` / `ruyipage`。
- **批量管理三维度**：`GLOBAL_TASKS`（全局并发）/ `SUCCESS_TASKS`（成功数）/ `BATCH_SUCCESS_LIMIT`（批量成功上限）。

---

## 维护约定

1. **合并上游**：`git fetch upstream && git merge upstream/main --no-edit`；冲突时优先融合两侧逻辑（保留上游新功能 + 保留本地定制），特别是 `webui/static/index.html` 的 `rf-toast-stack` 元素与静态资源版本号。
2. **新增配置项**：同步更新 `webui/scripts.py` 的 `_ENV_LABELS`、`docs/configuration.md`（如涉及）与本文档。
3. **项目路径**：保持不含非 ASCII 字符（避免 curl_cffi SSL 证书校验失败导致的假性 refresh token 过期）。
4. 本文档随定制功能演进持续更新，新增定制在对应章节追加条目并注明涉及的模块与配置键。
