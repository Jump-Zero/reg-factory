# 更新日志

## 2026-08-09 - 1.2.27

**资产 API 一次性领取与 Plus 资格标注**
- 资产 API 每次输出前在线扫描，只允许本次状态为 `normal` 的账号进入可领取范围；封禁、过期、受限、凭据异常、未知和检测异常账号不再输出。
- 新增按“平台 + 账号”持久化的一次性领取账本；同一账号被 API 返回后，切换 Cookie、session、SUB2API、CPA 等格式也不会再次返回，并支持手动重置领取记录。
- ChatGPT 健康扫描增加 Plus 免费试用资格标注，区分可试用、暂无试用、已有套餐和资格未知；资格检测失败不改变账号健康状态。
- WebUI 新手教程新增 Outlook Graph 辅助邮箱配置步骤，环境配置中的辅助邮箱开关改为默认勾选的复选框。
- 资产页更新一次性领取说明、剩余账号提示和 ChatGPT Plus 试用资格列。

---

## 2026-08-08 - 1.2.26

**Outlook Graph 辅助邮箱与 RT 提取提醒**
- Outlook Graph `proofs/Add` 安全信息页支持使用用户自有 Outlook 辅助邮箱。
- 新增 `email----password----refresh_token----client_id` 配置格式；提交前通过 Graph API 验证 refresh token，并从 Inbox/Junk 轮询 Microsoft 安全码。
- `yyds`、`custom`、`outlook` 支持按逗号顺序配置故障转移；WebUI 增加醒目的 RT 前置配置提醒和辅助邮箱 API 验证按钮。
- 注册浏览器 profile 在 Graph 授权完成前保持复用，避免注册成功后提前关闭窗口。

---

## 2026-08-05 - 1.2.25

**Plus 提链协议更新**
- 对齐 `shi-YangYang/plus-extractor` 的两阶段协议：先创建不带活动字段的基线 Checkout，再对同一会话应用优惠。
- Checkout 响应同时包含 `cs_live_*` 与嵌套 `oaics_*` 时优先使用 OAICS，避免误报短链类型并打开错误提供方页面。
- 活动更新请求收紧为当前接口所需字段，并补充离线回归测试与固定上游提交记录。

---

## 2026-08-05 - 1.2.24

**Plus 批次轮换修复**
- 批次执行跳过已完成或待提交账号，避免重复占用首批并让剩余账号继续轮换。
- 本轮完成后自动取消终态账号选择，保留失败账号用于重试；状态栏显示当前批次账号区间和剩余数量。

---

## 2026-08-05 - 1.2.23

**ChatGPT 注册后 Plus 订阅模式**
- ChatGPT 单平台注册、已有邮箱注册和端到端全流程新增 `--plus-subscription`，成功取得会话后加入本地订阅队列。
- 主 WebUI 新增同源 Plus 订阅视图并托管本地执行服务；注册成功后自动入队，用户无需打开独立页面。
- 集成 `alexan0618/zkky` 固定提交的 MIT 代码并内置到主 WebUI；支持最多 27 个账号同步批处理、批量导入 AT 和一次录入卡片后自动应用。
- Plus 工作台优先使用主程序已配置的住宅 IP，没有住宅代理时回退到 Clash，并移除单独代理池配置；卡片字段仅在当前页面和请求内存中使用，不写入队列或浏览器存储。
- 批量导入 AT 上限提升到 100 条，执行按 1-27 可调并发自动轮换批次，单批仍保持 27 条上限。
- 批次执行新增“自动轮换 / 手动轮换”模式；手动模式每完成一批暂停，点击“执行下一批”后继续。
- Plus 提链与绑卡/支付改为阶段独立网络出口：提链优先住宅 IP，绑卡/支付优先 Clash；运行时会分别注入两条代理通道，缺少出口时自动回退。
- 网络页新增 Plus 阶段出口下拉框，可选住宅 IP、Clash 当前节点或具体 Clash 节点；具体节点模式增加并发切换保护。
- Plus 工作台自动生成成套的随机美国账单地址并应用到当前批次，操作者只需填写卡号、有效期和 CVC；地址不写入浏览器存储，发卡行拒绝后停止当前账号且不自动换号重试。
- Plus 预检增加并发进度、60 秒前端超时与固定 Clash 节点连通性诊断；预检和正式批次复用已解析的账号身份，避免每批逐条重复请求账号信息。
- 修复自动/手动批次在重复账号解析期间看似不推进的问题，并显示总批次数、单批规模和手动执行下一批的等待状态。

---

## 2026-08-04 - 1.2.22

**便携版本地配置目录修复**
- 修复从仓库 `dist` 目录双击新版便携包时数据目录回落到 AppData，导致原 `.env` 配置在页面中看似丢失的问题。
- 启动器会向上查找带 `.env` 和现有资产的项目目录，并自动沿用该目录；已有 AppData 配置不会被删除或覆盖。

---

## 2026-08-04 - 1.2.21

**ChatGPT 新版生日组件适配**
- 适配 `Let's confirm your age` 页面中不暴露普通 input 的 React ARIA 月、日、年可编辑日期段，并使用真实键盘事件填写成人生日。
- 同时支持单框生日、原生日期框、月日年三段数字输入、原生下拉和自定义下拉布局。
- 收紧年龄框识别，不再把生日页面中的任意 number 输入误当年龄；新版页面的姓名和生日均只填写一次。
- onboarding 诊断日志新增日期段 role、data-type、aria-label 和 aria-valuenow，便于后续页面变体定位。

---

## 2026-08-04 - 1.2.20

**Codex 手机验证码提交修复**
- Codex 手机验证码改用真实键盘事件写入 React 表单并回读校验，避免浏览器页面看似填值但框架状态仍为空。
- `/phone-verification` 现在始终视为手机验证流程，只有进入授权同意页或 OAuth 回调后才记录验证成功，不会因误报而重复租号。

---

## 2026-08-04 - 1.2.19

**RuyiPage 默认浏览器和 ChatGPT 网页端修复**
- 新增 RuyiPage Firefox WebDriver BiDi 浏览器，并设为共享网页流程的默认浏览器；首次使用可从 WebUI 一键安装 runtime，也可配置自定义 Firefox 路径。
- ChatGPT iCloud 注册修复邮箱验证码提交后的 HTML Route Error，页面会点击 Retry 并按实际表单状态继续，不再盲目重复提交旧验证码。
- WebUI 的 ChatGPT 注册表单突出 iCloud 邮箱来源和注册后直接导入 SUB2API 选项，补充目标分组配置。
- Codex OAuth 在 ChatGPT cookie 未传递到新授权域时自动完成 iCloud 邮箱重新登录，并支持 RuyiPage 的 localhost 回调捕获、cookie 字段转换和导航硬超时。
- Codex 手机验证支持在 Hero SMS、SMS-Man、firefox.fun 和自动切换之间明确选择；无回码时释放号码并重新登录后继续换号。
- firefox.fun 配置补充 APIName 字段，一键测试改用官方 myInfo 动作验证 token；取号继续按官方要求使用 token 和项目 ID。
- Chromium 专用的旧注册流程继续自动使用内置浏览器，避免将不兼容流程强行切换到 Firefox。

---

## 2026-08-04 - 1.2.18

**新手网络引导和健康资产 API**
- 新手指南补充 Clash Verge 的 External Controller 开启路径、监听地址、控制密码、mixed-port、代理组与面板字段对应关系。
- 增加动态住宅 IP 的单代理、代理池、换 IP 接口说明，并说明平台出口覆盖中的继承全局选择方式。
- 环境配置路线说明 .env 功能分组、未保存配置的一键连通测试和保存生效范围。
- 新手指南新增资产 API 章节，介绍扫描状态、访问密钥、顺序或 index 取用、下游格式和响应中的 verification 信息。
- 资产 API 每次输出前在线扫描对应平台，只从本次检测正常的健康资产池返回邮箱、Cookie 或凭据；封禁、过期、受限、凭据异常和未验证资产不会被返回。
- 号池扫描和资产 API 增加 Kiro 支持。在线检测只证明检测时刻可用，后续状态仍需以目标服务的实际响应为准。

---

## 2026-08-04 - 1.2.17

**WebUI 新手指南**
- 首次打开控制面板时启动交互式新手指南，按实际界面依次介绍任务库、网络出口、浏览器、邮箱接码、短信接码和任务运行。
- 增加 Clash API 专项配置说明，覆盖 External Controller、Secret、mixed-port、代理组和出口测试。
- 教程可跳过单个配置阶段或整套指南，完成状态仅保存在当前浏览器，并可从顶栏随时重新打开。
- 任务步骤说明平台选择、SUB2API 和 chatgpt2api 相关勾选项、现成邮箱用法、dry-run 预览和日志结果判断。
- 桌面与移动端均使用响应式遮罩和目标高亮，教程展示过程不会保存配置或自动启动任务。

---

## 2026-08-04 - 1.2.16

**ChatGPT iCloud 邮箱支持**
- 新增 `icloud` 邮箱 provider，支持 `icloud-code + service=openai` 申请地址，并通过 `/api/user/mail` 轮询 ChatGPT 验证码。
- ChatGPT 新增 `--email-provider icloud`；WebUI 增加邮箱来源、API 地址、API key、类型和 service 配置项。
- 正确处理 iCloud API 的空邮件响应，不会把 `{code: 0, message: success}` 误当成验证码邮件。
- API key 仅从 `.env` 或进程环境读取，示例配置不包含真实密钥；文档站 `email.manageh.shop` 与实际 API 主机 `mail.no-replyca.xyz` 已明确区分。

---

## 2026-08-02 - 1.2.15

**WebUI Grok OAuth 路径修复**
- WebUI 子任务不再让已完成注册的 Grok 浏览器跳转到可选的 Device Flow 同意页；该页面当前可能不渲染授权按钮，造成网页任务看似卡住。
- WebUI 直接使用已验证的本机 OAuth 导入回退，三平台注册等二次派生的 Grok 子进程同样生效。
- 直接命令行运行保持原来的浏览器 Device Flow 行为，不改变已跑通的 1.2.13 Grok 流程。

---

## 2026-08-02 - 1.2.14

**Grok Device Flow 页面路由修复**
- 修复 xAI 返回的 `accounts.x.ai/oauth2/device` 浏览器地址当前实际渲染 404，导致 Grok 页面无按钮并卡住的问题。
- 浏览器授权自动改用当前可用的 `grok.com/oauth2/device` 页面；token 交换仍使用 `auth.x.ai` 协议端点。
- 增加线上设备授权地址转换测试，保留本机 OAuth 回退，避免授权页面异常时影响 SUB2API 导入。

---

## 2026-08-02 - 1.2.13

**Outlook Graph 日文保持登录页修复**
- 修复 Microsoft Graph Device Flow 的“サインインの状態を維持しますか?”提示位于 iframe 时，页面文字可见但“はい”按钮无法点击的问题。
- Graph 授权动作现在同时扫描顶层页面和所有 iframe，避免网页任务持续等待并最终超时。
- 增加日文 iframe 场景回归测试；`check_outlook_status unavailable` 仍仅表示可选的注册后校验模块未安装，不影响 Graph 授权。

---

## 2026-08-02 - 1.2.12

**Grok OAuth 与 SUB2API 导入修复**
- 修复 xAI 注册页 Next.js chunk URL 带 `?dpl=` 参数时无法发现动态 Server Action 的问题，并兼容 40-44 位 action ID。
- OAuth 授权与 consent 统一使用 `plan=generic`、`referrer=grok-build`，对齐当前 xAI 授权参数。
- 注册浏览器保持登录态完成 Device Flow，取得 refresh token 后直接创建或修复 SUB2API Grok OAuth 账号。
- 导入前解析 Grok 注册风控状态；`policy=deny,event=$registration` 的账号明确早停，不再触发无效远端转换或创建脏账号。
- 补齐波兰语、西班牙语邮箱注册入口和 Cookie 横幅文案，并增加通用邮箱入口兜底。
- 真实流程验证：Outlook 邮箱完成注册、OAuth 和 SUB2API 导入；临时邮箱注册成功后被 xAI 注册风控拒绝并正确跳过导入。

---

## 2026-07-31 - 1.2.11

**Outlook 日文注册与 WebUI 网络配置修复**
- 修复 Outlook 日文注册后的 `サインインの状態を維持しますか?` 页面识别，自动点击“はい”并继续 Graph 授权。
- 补齐日文隐私同意页、Graph 应用授权页的“許可/同意/続行”正向按钮和“拒否/許可しない”负向按钮，避免误点拒绝。
- 使用日文界面和住宅代理完成真实 Outlook 注册，浏览器会话直接获取 Graph refresh token 并写入账号池。
- 修复 WebUI 网络配置接口返回非 JSON 错误时前端解析失败，服务端统一返回结构化错误并脱敏代理凭据。
- 全量测试通过，共 255 项。

---

## 2026-07-30 - 1.2.10

**Kiro 真实流程修复**
- 修复 Kiro Signup 重定向位于 URL 片段时无法提取 `workflowID` 的问题。
- 注册前校验 Outlook Graph refresh token，自动跳过 `service_abuse` 等失效邮箱资产。
- 补齐 SSO 完成后的回跳、CSRF 跨域 Cookie 处理和临时 401 重试。
- 已完成真实 Kiro 注册验证，成功导出 Builder ID refresh token、access token 和 Kiro IDE 凭据。

---

## 2026-07-30 - 1.2.9

**Kiro 渠道与 WebUI 更新**
- 新增 Kiro Builder ID 注册渠道，支持设备授权、邮箱验证码、密码加密、SSO 和长期凭据导出。
- Kiro 账号保存为 `tokens/kiro/*.account.json`，资产 API 支持按顺序或 index 读取账号凭据。
- CLI、全流程编排、WebUI 任务库、独立代理出口和 Windows 打包入口全部支持 Kiro。
- WebUI 新增一键更新入口，运行状态和失败重试提示更完整。
- 新增 Kiro 协议、加密、资产和编排测试；全量测试通过。

---

## 2026-07-29 — 1.2.8

**Grok 导入 sub2api 修复**
- 修复 Grok SSO 转 OAuth 的 scope、Cookie、Device Flow 和 CLI 请求头配置，降低导入后 401 问题。
- 导入流程优先生成可刷新的 OAuth 凭据，失败时自动回退到服务端转换。
- 已存在但状态异常或返回 401 的同名账号会自动更新凭据并恢复正常状态。
- 新增 `python tools/upload_tokens.py grok --force`，支持重新导入历史 Grok 账号。
- 增加 OAuth、sub2api 修复及强制重传测试，完整测试 235 项通过。

---

## 2026-07-29 — 1.2.7

**全流程启动与代理修复**
- Stage A 固定 Clash 节点会修复旧配置中的重复乱码，并在节点已下线时返回可操作的错误，不再直接冒出 `HTTP 400`。
- 冻结版 EXE 的子任务启动失败后立即退出，不再等待控制台输入并残留孤儿进程。
- 空的 Outlook 内核回退配置恢复默认 `130`，不再受模块导入顺序影响。

**Claude 注册修复**
- 适配当前 Cloudflare 文案、可见复选框、magic-link client attestation 与生日数字输入，只有完成 onboarding 并进入聊天路由才记为成功。
- 住宅代理启动时检查跨连接 IP 连续性；拒绝按请求换 IP 的 endpoint，并在提交邮箱前被拦截时轮换 Sticky 代理池后重建浏览器会话。
- 临时邮箱支持指定域名，并修正 YYDS 域名健康检查与已消费 magic-link 过滤。

**ChatGPT 注册稳定性**
- 加固认证步骤跳转、Turnstile 检测和 onboarding 失败判定；不增加临时邮箱入口。

---

## 2026-07-29 — 1.2.6

**BitBrowser 内核恢复**
- Claude 与 Outlook 自注册遇到首选内核安装失败时，自动将本次临时 profile 回退到已安装的 130 内核后重开，不修改其他浏览器资料。
- 空的 Claude、Grok、Outlook 内核配置恢复为默认 146，避免把空字符串传给 BitBrowser；Grok 保持现代内核，不降级到会触发 xAI 发码 403 的旧内核。
- 内核回退仍失败时给出明确的 BitBrowser 修复提示，并补充局部指纹更新与平台回退回归测试。

---

## 2026-07-29 — 1.2.5

**Windows EXE 实时日志**
- 修复冻结版 EXE 子任务的标准输出被全缓冲、任务结束后日志才一次性显示的问题。
- 所有 WebUI 子任务强制使用逐行直写输出，SSE 日志响应显式禁用中间缓冲。

**Outlook 恢复流程**
- 将 Outlook 解锁与 Graph RT 提取合并为单个任务；恢复登录后立即提取并回写邮箱池。
- 解锁与注册改为调用同一个按住验证入口，统一目标定位、拟人轨迹和按住时序。

---

## 2026-07-29 — 1.2.4

**资产恢复、Cookie 导出与平台出口**
- Outlook 解锁和 Graph RT 提取改为读取号池扫描状态；默认处理待解锁/过期资产，成功后回写 `emails.txt`、清除对应 RT 错误并刷新号池状态。
- 资产 API 新增 `format=cookies`，支持 Claude、ChatGPT、Grok 按顺序或指定 `index` 输出标准浏览器 Cookie JSON；导出工具也支持指定平台和账号。
- 网络出口支持 Outlook、Claude、ChatGPT、Grok 独立覆盖全局模式，可实现 Outlook 走 Clash、其他平台走动态住宅 IP，并按平台轮换和测试公网出口。
- 浏览器新增自定义 Chrome/Chromium 和 BitBrowser 兼容 API 模式，保留内置 Chromium、BitBrowser 与 AdsPower。
- Claude 临时邮箱来源提升到任务首屏；Grok 注册 UI 和三平台编排只保留浏览器流程。

---

## 2026-07-28 — 1.2.3

**任务停止与 Claude 邮箱池修复**
- 运行日志区新增“停止全部”，可结束当前 WebUI 任务树，并清理旧版本遗留的 reg-factory 注册任务；不会关闭 WebUI、BitBrowser 或 Clash。
- 单个“停止当前”也改为终止完整进程树，避免无限循环注册脚本留下子进程。
- Claude 最新 RT 模式会隔离 `service_abuse`、租户不匹配等永久失效的 Graph token，后续运行不再重复检测同一批坏邮箱。
- 临时网络错误不会污染邮箱错误池；没有可用邮箱或注册失败时返回非零退出码。
- 资产扫描将 Microsoft `service abuse mode` 准确显示为封禁状态。

---

## 2026-07-28 — 1.2.2

**BitBrowser 住宅代理修复**
- Outlook 邮箱注册在住宅模式下将代理类型、主机、端口及认证直接写入新建 BitBrowser profile，不再依赖 Clash TUN 出口。
- Clash 模式仍使用原有 TUN 行为；住宅模式只影响新建窗口，已打开窗口不会被中途改 IP。
- Outlook 循环日志按实际模式显示代理类型，并隐藏代理用户名和密码。

---

## 2026-07-28 — 1.2.1

**启动与网络出口修复**
- Windows 启动器只复用相同版本的后台服务；旧版本占用 8799 时，新版会自动选择空闲端口并打开正确页面。
- 重复双击同一个新版时会查找并复用已运行的新版本，避免不断启动重复进程。
- 从源码版切换到便携包时，可从正在运行的旧服务识别并沿用已有资产目录，避免新版页面误显示空号池。
- 全局配置加载统一遵守 `REG_FACTORY_ENV_FILE`，沿用资产目录时同步加载住宅代理、API Key 等原有配置。
- 网络出口“应用并测试 IP”会先保存并应用页面当前配置，再检测实际出口，避免误测上一次 Clash 配置。
- 动态住宅出口检测最多建立 3 次独立连接，自动跳过单次轮换到的超时节点，并对错误中的认证地址脱敏。

---

## 2026-07-28 — 1.2.0

**资产号池扫描**
- 资产 API 页面新增 Outlook、ChatGPT、Claude、Grok 一键在线扫描，后台执行并实时显示进度。
- 状态细分为正常、待解锁、封禁、过期、受限、凭据异常、未知和检测异常；HTTP 403、限流、Cloudflare 或网络故障不会误计为封禁。
- 扫描结果持久化到 `runtime/state/asset_pool_scan.json`，只保存账号、状态、判定依据和时间，不保存密码、Cookie、Token 或 SSO。
- 新增号池状态 API、平台/状态筛选、分页明细及按当前类型扫描。

---

## 2026-07-28 — 1.1.1

**资产 API WebUI**
- 控制台新增“资产 API”入口，可配置 API Key、查看邮箱与平台资产统计，并在线选择平台和输出格式。
- 支持顺序取用或指定 `index`，实时生成请求地址与 `curl` 示例，可查看响应并重置当前或全部游标。

---

## 2026-07-28 — 1.1.0

**WebUI 与项目结构**
- 精简控制台导航与任务表单，重新整理项目目录、README、配置文档和维护工具入口。
- README 恢复平台图标和技术栈徽章展示，统一使用“邮箱注册”等明确表述。

**网络出口**
- 新增 Clash 自动轮换、固定节点与动态住宅 IP 三种模式，并支持立即切换和出口检测。
- BitBrowser 新建窗口时可写入住宅代理类型、地址、端口、用户名和密码。

**本地资产 API**
- 支持顺序读取或通过 `index` 精确读取邮箱和 Claude/ChatGPT/Grok 登录资产。
- ChatGPT 可输出原始 Cookie、Cookie Header、session、SUB2API、CPA 和 chatgpt2api 格式；Grok 可输出 SUB2API SSO 请求体。
- 顺序游标独立持久化且可重置；未配置 API key 时仅允许本机调用。

**Windows 发布包**
- 新增 PyInstaller 便携版构建入口，配置与运行数据持久化到 `%LOCALAPPDATA%\RegFactory`。
- Release 包使用文件白名单生成，不包含 `.env`、邮箱池、Cookie、Token、日志和调试截图。
- Codex K12 继续保留在源码仓库，但不打入 Windows 主安装包，避免引入整套 Node 依赖。
- Windows 启动器会自动打开控制台、复用已运行实例或避让被占用端口，并在启动失败时保留错误窗口。

---

## 2026-07-24 — Claude / ChatGPT 注册稳定性、Outlook Graph 与 WebUI

**Claude**
- `--node auto` 改为限量快速探测并优先回退最近可用节点；发送 magic-link、原生 nonce 验证和浏览器 API 验证均先检查路由，地区限制或 Cloudflare 页面不会提前消耗打码任务。
- hCaptcha 优先走本地视觉求解，兼容 DOM tile、canvas 点击和拖拽；新增多语言题干识别、珠链长度本地检测、两条线端点点击及多模型投票回退，YesCaptcha 保留为备用并支持瞬时失败重试。
- magic-link 原生验证遇到 403 会保留同一浏览器会话重试，HTTP 200 后把登录 cookie 注入页面并完成 onboarding；成功产物保存为 `tokens/claude/<email>.sessionKey.json`。
- sessionKey 校验复用 Clash 出口和现代 Chromium 指纹，避免注册成功后被旧内核或直连出口误判失效。

**ChatGPT**
- 邮箱提交、验证码、密码与 onboarding 状态判断改为基于可见控件和认证请求；收不到验证码时在进入 onboarding 前明确失败。
- about-you 页面提交前自动勾选必选同意项，兼容多语言完成按钮与 React 表单兜底提交，修复按钮始终 disabled 的卡死。
- 注册期间固定 Clash 出口，自动探测节点失败时给出明确错误，避免认证中途换 IP。

**Outlook / Graph**
- Outlook 注册成功后优先在当前登录浏览器上下文完成 Graph OAuth，并为每条 refresh token 保存实际签发的 `client_id`；失败时才回退纯 HTTP 补抽。
- 注册与 Graph 授权改为稳定控件 ID、字段元数据优先，兼容常见欧洲及亚洲语言；HTTP 回退支持不同属性顺序、引号和相对表单地址。
- 单次超时关闭 BitBrowser 时忽略预期的 Playwright `TargetClosedError` 后台噪声，其他异步异常仍正常上报。

**WebUI**
- Claude 任务页补齐 `client-id`、节点轮换和人工接管参数；配置页新增节点探测、hCaptcha 重试、视觉网关、模型和浏览器内核设置。
- Claude 任务页增加醒目的视觉 API 必填警示，配置页将 `CLAUDE_VISION_API_BASE` / `CLAUDE_VISION_API_KEY` 标为必填，避免未配置解码服务时直接进入必然失败的图形验证流程。
- 宽屏使用参数/日志双栏，移动端保持单栏抽屉导航；日志区显示待运行、运行中、成功、失败和停止状态，并从 SSE 结束事件读取真实退出码。

**验证**
- 新账号实测 Claude 注册 `1/1`，成功保存并校验 sessionKey；ChatGPT 新账号注册完成 onboarding 并取得有效 session。
- 自动化回归：Claude / ChatGPT / WebUI 91 项通过，Outlook Graph 20 项通过；桌面 1440×900 与移动端 390×844 无横向溢出或控件重叠。

---

## 2026-07-14 — Grok 纯 HTTP 协议注册（不开浏览器） + WebUI 接入 + 移除 ruyi 版

**新增**
- **`register_grok_http.py` — Grok 纯 HTTP 协议注册**：集成 [HM2899/grokcli-2api](https://github.com/HM2899/grokcli-2api) 的 `xconsole_client` 协议库（已 vendored 到 repo 根目录）。全程不开浏览器，直连 accounts.x.ai 完成发码 / 验码 / 建号 / 取 sso，成功后落标准 grok sso token 到 `tokens/grok/<email>.sso.json`。
  - 复用本项目现有基建：Clash 节点切换、临时邮箱（`common.temp_email`）、CapSolver 打码、`common.session_export` 落盘。
  - 依赖：能过 Cloudflare 的干净节点 + 可用临时邮箱 provider（`TEMP_EMAIL_PROVIDER`）+ `CAPSOLVER_API_KEY`。
  - 用法：`python register_grok_http.py --count 1`，或 `--node "美国 01"` 指定节点。

**改进**
- **WebUI「Grok 注册」改走 HTTP 协议版**：`webui/scripts.py` 的 `register_grok` 任务指向 `register_grok_http.py`，参数精简为 `--count` / `--node`。
- **三平台 / 端到端编排的 grok 分支改走 HTTP 协议版**：`register_three_platforms.py` 不再调用旧的浏览器版。

**移除**
- **删除 `register_grok_ruyi.py`（ruyiPage/Firefox 版）**：验证码掩码输入框在浏览器内无法稳定通过，已被 HTTP 协议版取代。

**测试**
- HTTP 协议版实测 `success: 1/1`：发码 / 验码 / Turnstile / 建号 / 取 sso 全通，token 正常落盘。

---

## 2026-07-13 — Outlook 按住验证拟人化 + 节点探测轮换 + WebUI 精简

**新增**
- **`common/human_mouse.py` 拟人鼠标模块**（纯 stdlib，无新依赖）：借鉴 [LoseNine/ruyipage](https://github.com/LoseNine/ruyipage) 的 WindMouse / human_move 运动算法（其 Firefox 内核级免检测不可移植，但轨迹/抖动算法可移植）。
  - `windmouse_path()`：重力 + 随机风力 + 速度钳制的逼近轨迹，天然变速（中段快、两端慢）带过冲，取代原来的简单二次贝塞尔。
  - `tremor_offsets()`：用 **Ornstein-Uhlenbeck 过程**生成按住期间的**自相关**微抖动（有动量 + 回中，像真人手的生理震颤）。自检 lag-1 自相关 0.98，对照白噪声 0.02。
  - `human_press_and_hold(page, cx, cy, is_done, max_hold, min_hold)`：完整按住序列——WindMouse 逼近 → 落点停顿 → down → OU 抖动循环（每 ~0.5s 轮询 `is_done()`，进度满后加真人反应延迟再 up）。
  - 自检入口 `python -m common.human_mouse`：校验轨迹连续/精确命中/速度非均匀、抖动自相关高于阈值。
- **Clash 节点「探测优先」轮换**（`outlook_reg_loop.py`）：切节点前先用 Clash `/delay` 探测延迟，**跳过超时节点**，在一批候选里挑延迟最低的再切换，不再把整次 attempt（~3min）浪费在死节点上。可调 `CLASH_MAX_LATENCY_MS`（默认 2500）、`CLASH_PROBE_BATCH`（默认 8）。
- **`--no-rotate` / `OUTLOOK_NO_ROTATE=1` 开关**：固定使用当前节点，不探测/不切换，也不连 Clash 控制器。WebUI 邮箱注册面板已同步该开关（及原先漏配的 `--sleep-when-full`）。

**改进**
- **按住验证鼠标运动去机器人特征**：`register_outlook_standalone.py` 原按住期间是纯正弦波漂移（完全周期性）、`register.py` 是 ±2px 均匀随机抖动（白噪声无动量），两者 PerimeterX 行为模型都易判；两处入口均改用 `human_press_and_hold`，复用各自原有的「captcha 消失=已通过」判定作 `is_done` 回调。
- **去掉节点区域亲和（日本优先）**：改为按名称平等轮换 + 探测选优；保留 CN/直连节点排除、会话内去重轮换、IP 变更验证。

**修复**
- **数据确认页误点**：`_maybe_confirm_before_register` 现在先按 body 文本判定是否真出现数据许可/`privacynotice` 页，只有命中才点允许/接受，避免正常表单页误点页脚/cookie 条上的 `OK`/`确定` 链接打乱流程。
- **`TargetClosedError` 崩溃**：按住过程中页面/context 关闭（节点掉线或验证通过后导航销毁上下文）时，fallback 不再对已死页面二次 `mouse.down/up`；识别到 closed/TargetClosed 即标记未过、交外层循环判定。

**移除**
- **WebUI 拿掉 Gmail 注册内嵌页**：清空 `webui/scripts.py` 的 `EMBED_PAGES`（原唯一 Gmail 条目），侧边栏「功能 / 🌐 Gmail 注册」按钮、iframe 视图、接码助手随之隐藏（通用 iframe 基建与 `/api/sms/*` 端点保留备用）。

**测试**
- 拟人按住实测过真 PerimeterX：Outlook 自注册一次跑 5 attempt，2 次成功（`pb74z...` / `hfgcz...`），按住 2~3 次后 `captcha 元素已消失 → passed`，链路走通（注册 → 过验证 → 抽 Graph token → 写池）。
- 节点探测轮换实测：连续跳过 HK 中转 ×3 + SG 死节点（各 ~4s），选中台湾/香港活节点（106~137ms）并确认出口 IP 变更。
- 失败样例（与本次改动无关）：一次 `password input not found`（繁中页/慢节点密码步骤未在 10s 内渲染）、一次 `TargetClosedError`（节点掉线，已由上面的修复覆盖）。

**说明**
- 拟人运动只解决「鼠标行为像不像人」这一可控维度，真实通过率仍受 IP 信誉/会话上下文影响。可调参：`HUMAN_MOUSE_TREMOR_PX`（抖动幅度）、`HUMAN_MOUSE_DEBUG`、`OUTLOOK_REG_MAX_PRESS`。

### 同批提交的 Gmail Android 增量（既有未提交工作，非本次会话作者所写，按代码实况归纳）
- **`gmail_register_local.py` 大幅扩充**（+1400 余行）：新增 ADB accessibility-tree 驱动的注册/手机验证链路（`adb_ui_nodes` / `adb_find_node` / `adb_tap_node` / `adb_fill_node` / `adb_auto_phone_verification` / `adb_complete_post_phone_flow`），Appium 侧姓名/下一步/创建个人账户/手机号录入等步骤函数，注册后**二次登录**（`second_login_flow`）与**手机 2FA**（`enable_phone_2fa`）流程，账号状态断点续跑（`save_account_state` / `load_account_state` / `resume_registration_flow`）、人工接管判定（`manual_handoff_result`），以及 `ensure_appium_server` 自启。
- **`scripts/watch_appium.ps1`**（新增）：单实例 Appium 看门狗，绑定独立 `ANDROID_ADB_SERVER_PORT`，UiAutomator2 崩溃后自动拉起。
- **`gmail_android/tests/test_post_registration.py`**（新增）：注册后流程测试。
- **配置项补全**：根/`gmail_android` 的 `.env.example` 新增 BlueStacks 实例、ADB/Appium 端口、`AUTO_*` 自动化开关、`NODE_*` 节点探测参数、`RECAPTCHA_AUTO_SOLVE`、`SMSMAN_*_GMAIL` 等；`.gitignore` 忽略 `gmail_android/.runstate/` 与 `logs/`。
- ⚠️ 此部分未经本次会话验证，仅据 diff 与函数签名归纳，可能与实际行为有出入。

## 2026-07-11 — Gmail Android 注册优化（reCAPTCHA 自动解 + SMS 国家筛选）

**新增**
- **`gmail_android/recaptcha_android.py` 视觉自动解 reCAPTCHA v2**：通过 ADB accessibility tree 定位 WebView 里的 reCAPTCHA 节点（`recaptcha-anchor` checkbox、`rc-imageselect` 挑战窗口），Appium 点击/截图，调用 `common/agent_captcha.py` 视觉投票识别图块。
  - **WebView 节点等待**：`solve()` 入口加 12s 等待循环，解决"检测到 reCAPTCHA 文字但 accessibility tree 还没暴露节点"的时机问题（之前立即返回 `False`）。
  - **挑战类型自适应**：点 checkbox 后若直接通过（绿勾）则返回 `True`；若弹图片挑战则循环识别提交，最多 `max_rounds` 轮（默认 8）。
  - **二登默认启用**：`RECAPTCHA_AUTO_SOLVE=1` 时自动调用，失败仍回退人工。要求 `VISION_API_KEY` 已配置（否则 `usable()` 返回 `False`）。
- **SMS 国家筛选**（`sms_provider.py` + `config.py`）：
  - **firefox.fun 白名单**：新增 `SMS_COUNTRY_GMAIL`（逗号分隔国家码，如 `"33,44"` = 法国/英国），`_request_firefox_number` 循环传 `country=<code>` 向接码平台请求指定国家号码；空值保持原有"任意国家"行为。
  - **sms-man 多国支持**：`SMSMAN_COUNTRY_GMAIL` 现支持逗号分隔（如 `"155,100"` = 法国 id=155、英国 id=100），`_request_smsman_number` 逐个国家 ID 尝试租号。
  - **三 provider 级联**：`request_number()` 优先 firefox.fun（有库存且过黑名单/白名单）→ sms-man（按配置国家列表）→ hero-sms（兜底）。
- **BlueStacks 自动化**（`bluestacks.py`）：实例启动、ADB 连接、Google 账户清理、Appium UiAutomator2 server 安装的统一封装，支持 `AUTO_PREPARE_EMULATOR=1` 时自动准备干净实例。
- **Clash 节点切换**（`proxy_switch.py`）：mihomo/Clash API 封装，支持节点延迟探测、区域关键词过滤（`NODE_REGION_KEYWORDS`）、Google 连通性探测（`proxy_probe`），配合 `AUTO_SWITCH_NODE=1` 在注册前自动切可用节点。
- **流程协调器**（`coordinator.py`）：封装 Appium session 初始化、模拟器准备、节点切换、SMS provider 配置检查的编排逻辑，供 `gmail_register_local.py` 调用。

**改进**
- **sms-man 优先多次接码号**：`prefer_multi=True` 时优先选 `can_receive_multiple_sms=True` 的号码（Gmail 二登可能再要一次验证码）。
- **配置项补全**：`config.py` 新增 `AUTO_PREPARE_EMULATOR`、`AUTO_START_APPIUM`、`AUTO_SWITCH_NODE`、`AUTO_STOP_EMULATOR`、`KEEP_EMULATOR_ON_MANUAL_HANDOFF`、`BLUESTACKS_INSTANCE`、`BLUESTACKS_ADB_PORT`、`APPIUM_SYSTEM_PORT`、`SECOND_LOGIN_AFTER_SIGNUP`、`ENABLE_2FA_AFTER_LOGIN`、`RECAPTCHA_AUTO_SOLVE`、`RECAPTCHA_SOLVE_ROUNDS` 等，统一从 `.env` 读取。

**测试**
- 实测视觉 reCAPTCHA 解题：点 checkbox 直接过 ✅、3×3 图片挑战（"select all traffic lights"）✅；12s 等待修复后稳定检测到节点。
- 实测 SMS 国家筛选：firefox.fun 法国/英国无库存时回退 sms-man，成功租到 `+447446302327`（英国，multi=True），Google 接受号码（未拒绝"used too many times"），但 sms-man 该号段 180s 内未收到 Google 短信（VoIP 虚拟号限制）。
- 实测默认流程（`SMS_COUNTRY_GMAIL` 空）：firefox.fun 返回菲律宾 +63 号被黑名单过滤，sms-man 返回马来西亚 +60 号被 Google 拒绝（"used too many times"）。

**说明**
- reCAPTCHA 视觉解题需要 `VISION_API_KEY` + `VISION_MODEL`（推荐 `gpt-5.5` 或 `claude-opus-4`），每轮挑战约消耗 1 次视觉 API 调用（截图 + 题干）。
- SMS 国家筛选仅在接码平台有库存且号段未被 Google 风控时有效；虚拟号段（如 sms-man 英国 `+4474xx`）可能收不到 Google 短信，建议测试后再大规模使用。
- 新增模块（`bluestacks.py`、`proxy_switch.py`、`coordinator.py`、`recaptcha_android.py`）为 `gmail_android/` 独立实现，不影响现有 Outlook/ChatGPT 注册流程。

## 2026-07-06 — AdsPower 指纹浏览器适配

**新增**
- 新增 `adspower.py`，把 AdsPower Local API 封装成现有 BitBrowser 兼容接口，支持 profile 创建、启动、关闭、删除、列表，以及旧脚本使用的 `/browser/*` 兼容调用。
- 新增 `FINGERPRINT_BROWSER=bitbrowser|adspower` provider 开关；默认仍为 BitBrowser，设置为 `adspower` 后 `BitBrowser()` 会自动返回 AdsPower 适配器。
- 新增 AdsPower 配置项：`ADSPOWER_API`、`ADSPOWER_API_KEY`、`ADSPOWER_GROUP_ID`。

**适配**
- 通用注册入口、ChatGPT/GitHub/Grok/Codex OAuth、邮箱 broker、Outlook 自注册/解锁等现有浏览器调用路径适配 AdsPower。
- WebUI 配置页新增“指纹浏览器”分组，`FINGERPRINT_BROWSER` 支持下拉切换 BitBrowser / AdsPower；状态灯会按当前 provider 显示 BitBrowser 或 AdsPower。
- README、`.env.example`、安装提示同步为 BitBrowser / AdsPower 双 provider 说明。

**测试**
- 已验证 AdsPower `/status` 连通、provider 工厂切换、WebUI 后端导入、`run_full_flow.py --platforms chatgpt --codex --rounds 100 --import-c2a --dry-run` 编排。
- 真实创建 AdsPower profile 需要填写 `ADSPOWER_API_KEY`；未填写时 AdsPower 返回 `Require api-key`，属于本地配置缺失。

## 2026-06-25 — sms-man 接码过 Codex add-phone + 全自动 OAuth 链路（移除订阅模块）

**新增**
- **`common/sms.py` 接入 sms-man.com（API v2.0）为主用接码平台**：按 pkey 前缀路由（`smsman_<id>` → sms-man，`hero_<id>` → hero-sms，否则 firefox.fun），优先级 **sms-man → firefox.fun → hero-sms**。
  - **OpenAI 服务自动解析**：`_smsman_resolve_app` 按名称（"openai"）在 `/applications` 子串匹配出 application_id（OpenAI/ChatGPT = **2754**），免硬编码；适配 sms-man 返回 **dict-keyed-by-id** 且字段为 `title`（非 list/name）的实际格式。
  - **按便宜的排序**：`_smsman_rank_countries` 经 `/get-prices` 价格升序，`_smsman_get_phone` 最便宜国家优先逐个试租。
  - **账号级错误快速失败**：余额不足 / token 失效返回 `FATAL` 立即中止，不再空刷 170+ 国家。
  - CLI 辅助：`python -m common.sms applications|countries|balance|prices openai`。
- **`common/oauth_codex.py` add-phone 全自动接码**：遇 OpenAI add-phone 自动填号 + 接 SMS 验证码过号。
  - **WhatsApp→SMS 修正**：OpenAI 默认 WhatsApp 投递，`_select_sms_if_present` 在填号前后多语言点选 "Text message" 切到 SMS（sms-man 投 SMS）。
  - **自动换号重试**：手机号大概率被风控拒，`handle_add_phone` 最多重试 `CODEX_ADDPHONE_ATTEMPTS` 次（默认 8），逐号换租。
  - **每次尝试先关窗口重登**：`make_reset_page` 工厂在每次授权尝试前 teardown 旧窗口 / 开新窗口 / 重载 cookie / 重登，避免复用窗口导致 OpenAI 风控决策不重新 roll。
  - **`authorize_with_retry`** 统一编排：`gen_auth_url` 4× 退避重试（SUB2API tiantianai.co 偶发不可达）、`asyncio.wait_for` 硬上限防 `drive_authorize` 卡死、consent 点击用精确 `button[data-dd-action-name="Continue"]` 选择器 + churn-breaker 重 goto。
- **`register_chatgpt.py` Cloudflare Turnstile 过墙**：`_is_cf_blocked`/`_click_turnstile`/`_switch_cf_node` —— AWS 机房 IP 触发整页 managed challenge（转圈无可点元素）时自动切非 AWS 节点；边界 IP 有可点框时先点。邮箱验证码支持重发兜底（`_click_resend_code`/`_renavigate_resend`，含 zh-TW "重新傳送電郵"）。

**移除**
- 删除 Codex 订阅（baxigpt）相关：`activate_plus.py`、`common/plus_baxi.py`，及 `config.py`/`.env.example`/README 中 `BAXI_API`/`BAXI_CARDS` 配置与说明。Codex 进 SUB2API/CPA 的正路统一为 `oauth_codex.py`（带真 `refresh_token`）。

**配置**
- `.env.example` 新增 `SMSMAN_TOKEN` / `SMSMAN_APP_ID_OPENAI`、`CODEX_ADDPHONE_ATTEMPTS=8` / `CODEX_SMS_TIMEOUT=150` / `CODEX_PHONE_SKIP_ATTEMPTS=0`。

**说明**
- 实测全流程（`run_full_flow --codex`）：邮箱注册 → CF 过墙 → ChatGPT 注册 → 邮件验证码（含重发）→ add-phone（sms-man 接 SMS 过号）→ consent → callback → SUB2API 建号（type=oauth）全链路打通。
- 实测 8/8 新号均要求 add-phone（手机要求**绑账号非绑会话**），phone-skip 对新号无效，故默认 `CODEX_PHONE_SKIP_ATTEMPTS=0`。
- sms-man 需 USD 余额 > $13；接码 token 走 `.env`，代码零明文。

## 2026-06-12 — vision_solver 过 hCaptcha（canvas 点击 + 拖拽）

**新增**
- **`vision_solver` 新增 `canvas_grid` 模式**：解新版 hCaptcha。实测发现现代 hCaptcha 把**整个挑战渲染进单个 `<canvas>`（500×470）**，无任何可枚举/可点的 DOM tile，原 `grid_select`（点 DOM 元素）不适用。新 driver `solve_canvas_grid`：
  - **稳定截图**（`_shot_canvas_stable`）：先强制等图加载，再要求连续帧字节一致才采用，避免截到渐入/加载中的半成品。
  - **像素坐标点击**（`_click_canvas_cell`）：按 bbox/截图尺寸比换算 dpr，直接 `click(position=...)` 点 canvas 对应格中心。
  - **网格几何**（`overlay_grid_numbers` 四边内缩）：实测 500×470 上内缩 top0.30/bottom0.036/左右0.164，把编号网格框定到真实图块区，保证点中心对齐。
  - **题型自动判别**（`_infer_layout`）：题干含 "the item/thing"(单数) 且无 "all/each" → 单选（`vote_answer`，只点 1 格、永不空选）；"card/different" → 1×3 卡片单选；"select all" → 多选（`vote_picklist`）。空共识时兜底点最高票一格，避免空提交浪费轮次。
- **`vision_solver` 新增 `canvas_drag` 模式**：解拖拽类挑战（把 piece 拖到 target）。`solve_canvas_drag` + `vote_points`（各模型给 `FROM=(x,y) TO=(x,y)` 归一化坐标、取各点中位数抗离群）+ `_drag_on_canvas`（`page.mouse` down/move 分步带抖动/up 模拟人手）。预置 `presets/hcaptcha_drag.json`。
- 预置 `presets/hcaptcha.json` 改写为 `canvas_grid`（`frame_match=["frame=challenge"]`，题干 `#prompt-question`，提交 `.button-submit`）。

**测试**
- 点击型对 live demo（`https://accounts.hcaptcha.com/demo`）三个测试 sitekey 各跑 3 轮：**8/9 通过**，唯一失败为空选卡死，修复后（单选路由 + 兜底点击）复跑全过。
- 拖拽型 demo 不发拖拽题（三种探针证实该 demo + 测试 key 只发 "Tap the item provides shade" 一种 3×3 点击题），故用本地合成 canvas 谜题（蓝球拖进红框）验证机制：**3/3 命中**，中位数投票纠正了个别模型偏差。

**说明**
- 真实 hCaptcha 拖拽/滑块题需 live 复现后再校准坐标系与题型判别；点击型已可用，拖拽机制已验证、链路就绪。
- 网关/key 复用现有视觉投票池变量（`.env`），代码零明文。`screenshots_vision/` 已入 `.gitignore`。

## 2026-06-08 — GitHub 注册 + Arkose 验证 agent-captcha 视觉求解

**新增**
- **`register_github.py`**：GitHub 注册主流程。单页表单（邮箱/密码/用户名/国家，只认 `Create account` 不误点 `Continue with Google`），提交触发 **Arkose FunCaptcha**（octocaptcha 包裹），过验证后浏览器登录 Outlook 取 launch code → 建号 → 存 cookie。`--auto` 跑完整流程，无参数为探索模式（填到验证停、保留窗口）。
- **`common/agent_captcha.py`**：Arkose 验证**视觉投票求解器**（不依赖传统打码平台）。
  - **变体自动分派**（按拼图题目文本，不硬编码）：`sequence`（4 图标逐环序列匹配）/ `rotate`（3D 物体朝向匹配）/ `character`（小人踩格，模型分歧大、默认跳过换窗口）。轮数从 "x of N" 解析、候选张数从 `.pip` 进度点数。
  - **多模型并发投票**：`vote_answer()` 让 gemini-3.5-flash / gpt-5.5 / gemini-3.1-pro / claude-opus 并发判断、多数表决；平票优先级 gemini-flash > gpt-5.5 > gemini-pro > opus（实测 claude 在拼图上偏弱，权重最低）。整轮 deadline 55s 防慢模型拖垮，空票自动重试。
  - **图像处理**：候选裁剪放大（`shot_element`）+ 拼成带编号网格（`stitch_options_grid`）+ 本地秒级增强 + 控体积 JPEG（`enhance_local`，避免大图传输超时空票）；可选 gpt-image-2 保真增强（`enhance_image`）。
  - **复盘标注**：每轮落 `screenshots_github/REVIEW_rN.png`，红框=最终选择、彩框=各模型投票，便于人工核对。
  - **协议自适应**：OpenAI 兼容网关走 `/v1/chat/completions`；claude/opus 走 Anthropic 原生 `/v1/messages`（base 以 `/claude` 结尾自动识别），图片 base64 按 JPEG/PNG 头自适应 media_type。
- `config.py` 新增 agent-captcha 配置项（全走 `.env`）：`VISION_API_BASE/KEY`、`VISION_MODEL`、`IMAGE_EDIT_BASE2/KEY2`、`VOTE_ZZ_BASE/KEY`、`VOTE_GPT_KEY`、`VOTE_OPUS_BASE/KEY`、`GEMMA_API_BASE/KEY`；`.env.example` 补齐占位与说明。

**说明**
- Arkose 验证关已实测可通过（sequence 变体 10/10、rotate 5 轮通过）；`character` 变体模型间分歧大，默认遇到即换窗口重试（最多 8 次）赌到易解变体。
- GitHub 对批量 Outlook 邮箱有风控（验证后提示 "This email can't be used"），整套自动化流程完整，邮源需配可用邮箱。
- 网关/key 一律走环境变量（`.env`），代码零明文，符合项目约定。

## 2026-06-07 — chatgpt2api 普通网页号导入

**新增**
- **`export_chatgpt2api.py`**：把注册落下的普通 ChatGPT 网页号聚合成 chatgpt2api（basketikun/chatgpt2api）的批量导入格式。`common/session_export.py:build_chatgpt2api_account` 把网页 session 转成导入对象（只认 `access_token`，**不带 `type:"codex"`**，否则会被对端当 codex 源），注册成功时顺手落 `tokens/chatgpt/c2a-*.json`。
- **`register_chatgpt.py --import-c2a`**：注册成功后用刚抓到的 session 即时 `POST <host>/api/accounts` 把 token 导入 chatgpt2api（默认关）。host/key 取 `config.CHATGPT2API_URL` / `CHATGPT2API_KEY`（走 `.env`），也可 `--c2a-url` / `--c2a-key` 覆盖。单号导入失败只告警，不影响注册成功判定。
- `--import-c2a` 逐层透传：`run_full_flow.py` → `register_three_platforms.py` → `register_chatgpt.py`（只对 chatgpt 平台生效，claude/grok 不受影响）。
- `config.py` 新增 `CHATGPT2API_URL` / `CHATGPT2API_KEY`（默认空，从 `.env` 读）。

**优化**
- `export_chatgpt2api.py` 新增 `import_accounts(host, key, accounts)`（不抛异常版，返回 `(ok, msg)`），供注册脚本逐个号上传时调用；命令行 `--post` 仍用原 `post_accounts`。
- `run_full_flow.py` 顺带提交已有的多轮循环（`--rounds` / `--round-sleep`，支持有限轮数与无限循环）。

**说明**
- 普通网页号无真 `refresh_token`，`access_token` 约 10 天过期后对端无法续期，属预期（codex/OAuth 三件套号仍走 `oauth_codex.py` + CPA/SUB2API）。
- 对端 API 路径是 `/api/accounts`（`/accounts` 是网页 UI），需 `Authorization: Bearer <admin key>`；重复 token 对端按 skipped 幂等处理。

## 2026-06-06 - Gmail Android/Appium 本地注册包

**新增**
- 新增 `gmail_android/` 模块，包含 Gmail Android 注册流程、Appium helper API、`.env` 配置加载、SMS provider 骨架和 Windows 安装脚本。
- 新增 BlueStacks 直接安装/配置脚本：`gmail_android/scripts/install_bluestacks.ps1`，支持配置 ADB、`Pie64_12`、`127.0.0.1:5675`、`900x1600 @ 240dpi`。
- 新增一键安装入口：`gmail_android/scripts/install_all_windows.ps1`，用于 GitHub Release 安装包解压后的环境初始化。
- 新增 Release 构建脚本：`gmail_android/scripts/build_release.ps1`，支持可选附带固定版本 BlueStacks 安装器。
- 新增 `gmail_android/offline/bluestacks/.gitkeep`，预留固定版本 BlueStacks 安装器目录；安装器二进制不进 git，后续随 Release 附件打包。

**优化**
- 根 `.env.example` 增加 Gmail Android/Appium 相关环境变量：`APPIUM_SERVER`、`ANDROID_DEVICE`、`GMAIL_USERNAME_PREFIX`、`ACCEPT_TERMS`、`SMS_PROJECT_ID_GMAIL`、`HERO_SMS_SERVICE_GMAIL` 等。
- 根 `requirements.txt` 增加 `Appium-Python-Client` 和 `selenium`。
- 根 README 增加 Gmail Android 安装包的安装、配置、运行和 Release 打包说明。
- README 补充 GitHub Release 安装包上传流程，覆盖网页上传和 `gh release` 命令两种方式。
- 根 README 前置条件补充 Gmail/谷歌邮箱注册所需的 BlueStacks、Android SDK/ADB、Node/Appium 和 Gmail App。

**安全边界**
- Gmail 手机/SMS/CAPTCHA 和 Google 额外安全验证默认由人工完成；脚本支持 `--resume-after-phone` 续跑。
- `--accept-terms` 仅在操作者明确同意 Google Privacy and Terms 后使用。
- `sms_provider.py` 仅作为后续合规内部接码 provider 的环境变量接口骨架，当前不默认接入 Gmail 安全验证自动化。

## 2026-06-04 — Codex 订阅授权 + 上传 SUB2API / CPA

**新增**
- **`oauth_codex.py`**：账号走 Codex OAuth 换取**带 `refresh_token` 的正式凭据**，一步建到
  **SUB2API**（`type=oauth`）并推 **CPA**，解决网页 session 无 refresh_token、下游过期 401。
- **接码支持 WhatsApp**：遇 OpenAI add-phone 手机验证，用 `--manual-phone` 在浏览器手动填号 +
  输码，**推荐 WhatsApp 可接码号段**（普通虚拟号易被拒）。
- 配套：`activate_plus.py` 激活码开通 Plus / Codex 订阅；`upload_tokens.py` 一键上传到
  CPA / SUB2API / webchat2api。
- 订阅地址 / 激活码全部走环境变量（见 `.env.example`）。

**优化**
- README 补全「Codex 订阅授权 & token 上传」「项目结构 / 模块职责」「典型一条龙用法」，适配多人协作。
- 清理冗余代码，半成品路径标注 WIP。



