# -*- coding: utf-8 -*-
"""
webui/scripts.py — GUI 的数据核心：把每个入口脚本的命令行参数、以及 .env 配置项
结构化成 schema，前端据此自动生成表单。新增脚本/配置项只改这里。

参数类型(type)对应前端控件：
  bool   -> 勾选框(store_true，勾上才追加 --flag)
  int    -> 数字输入
  str    -> 文本输入
  choice -> 下拉(配 choices)
  multi  -> 多选(nargs="+"，选中的值空格分隔追加在 --flag 后)
positional=True 表示位置参数(不带 --，直接拼值)。
"""

# ============================================================ 入口脚本 schema
SCRIPTS = [
    # ---------------------------------------------------------------- 主流程
    {
        "id": "run_full_flow",
        "file": "run_full_flow.py",
        "category": "主流程",
        "title": "端到端全流程",
        "desc": "注册 Outlook 邮箱 → 在所选平台注册账号。最常用入口。",
        "args": [
            {"flag": "--platforms", "type": "multi", "choices": ["claude", "chatgpt", "grok", "kiro"],
             "default": ["claude"], "help": "要注册的平台(可多选)"},
            {"flag": "--rounds", "type": "int", "default": 1, "help": "循环注册轮数(0=无限循环)"},
            {"flag": "--codex", "type": "bool", "default": False, "help": "chatgpt 注册后走 Codex OAuth 导入 SUB2API(会接码过手机)"},
            {"flag": "--grok-sub2api", "type": "bool", "default": False, "help": "grok 注册后把 SSO 转成 SUB2API Grok OAuth 账号"},
            {"flag": "--import-c2a", "type": "bool", "default": False, "help": "chatgpt 注册后即时导入 chatgpt2api(需配 CHATGPT2API_*)"},
            {"flag": "--codex-manual-phone", "type": "bool", "default": False, "help": "Codex add-phone 手动模式(自己在浏览器填号收码)"},
            {"flag": "--codex-group", "type": "str", "default": "", "help": "SUB2API 目标分组名(默认取配置)"},
            {"flag": "--grok-sub2api-group", "type": "str", "default": "", "help": "SUB2API Grok 分组名(默认取配置)"},
            {"flag": "--skip-email", "type": "bool", "default": False, "help": "跳过邮箱注册，直接用下面指定的邮箱"},
            {"flag": "--email", "type": "str", "default": "", "help": "配合 --skip-email：现成邮箱"},
            {"flag": "--password", "type": "str", "default": "", "help": "配合 --email 的密码"},
            {"flag": "--node", "type": "str", "default": "auto", "help": "claude/chatgpt/grok 走的 Clash 节点"},
            {"flag": "--email-attempts", "type": "int", "default": 30, "help": "邮箱注册最多尝试次数"},
            {"flag": "--platform-timeout", "type": "int", "default": 600, "help": "平台注册单号超时(秒)"},
            {"flag": "--email-confirm-before-register", "type": "bool", "default": False,
             "help": "邮箱注册页打开后自动点确认，再开始填写"},
            {"flag": "--dry-run", "type": "bool", "default": False, "help": "只打印将执行的命令，不真注册(安全预览)"},
        ],
    },
    {
        "id": "register_three_platforms",
        "file": "register_three_platforms.py",
        "category": "主流程",
        "title": "三平台注册(已有邮箱)",
        "desc": "用现成邮箱(或从 emails.txt 池)在 Claude/ChatGPT/Grok 注册。",
        "args": [
            {"flag": "--from-pool", "type": "bool", "default": False, "help": "从 emails.txt 池取一个邮箱"},
            {"flag": "--email", "type": "str", "default": "", "help": "指定邮箱(不从池取时)"},
            {"flag": "--password", "type": "str", "default": "", "help": "邮箱密码"},
            {"flag": "--token", "type": "str", "default": "", "help": "Outlook refresh_token(走 Graph 取码)"},
            {"flag": "--client-id", "type": "str", "default": "", "help": "Outlook OAuth client_id"},
            {"flag": "--platforms", "type": "multi", "choices": ["claude", "chatgpt", "grok", "kiro"],
             "default": ["claude", "chatgpt", "grok"], "help": "要注册的平台"},
            {"flag": "--parallel", "type": "bool", "default": False, "help": "并行跑各平台(默认顺序)"},
            {"flag": "--loop", "type": "bool", "default": False, "help": "持续从池取号循环注册(常驻)"},
            {"flag": "--codex", "type": "bool", "default": False, "help": "chatgpt 后走 Codex OAuth"},
            {"flag": "--grok-sub2api", "type": "bool", "default": False, "help": "grok 后导入 SUB2API Grok 渠道"},
            {"flag": "--grok-sub2api-group", "type": "str", "default": "", "help": "SUB2API Grok 分组名(默认取配置)"},
            {"flag": "--import-c2a", "type": "bool", "default": False, "help": "chatgpt 后导入 chatgpt2api"},
            {"flag": "--node", "type": "str", "default": "auto", "help": "Claude/ChatGPT/Grok Clash 节点"},
            {"flag": "--timeout", "type": "int", "default": 600, "help": "单平台超时(秒)"},
        ],
    },
    {
        "id": "oauth_codex",
        "file": "oauth_codex.py",
        "platform": "chatgpt",
        "category": "主流程",
        "title": "Codex OAuth 授权 → SUB2API",
        "desc": "用已存 cookie 重登 ChatGPT，走 OAuth 拿带 refresh_token 的凭据建到 SUB2API/CPA。默认直接接码过 add-phone。",
        "args": [
            {"flag": "--cookie", "type": "str", "default": "", "help": "cookie 文件路径(默认最新 full_*.json)"},
            {"flag": "--phone-skip", "type": "int", "default": 0, "help": "先赌免手机直连次数(0=直接接码不赌)"},
            {"flag": "--sms-provider", "type": "choice", "choices": ["auto", "hero", "smsman", "firefox"],
             "labels": {"auto": "自动切换", "hero": "Hero SMS", "smsman": "SMS-Man", "firefox": "firefox.fun"},
             "default": "auto", "help": "add-phone 使用的接码平台；可强制指定 Hero SMS"},
            {"flag": "--node", "type": "str", "default": "auto", "help": "固定 Clash 出口；auto 自动探测"},
            {"flag": "--manual-phone", "type": "bool", "default": False, "help": "手动模式：自己在浏览器填号+输码"},
            {"flag": "--phone", "type": "str", "default": "", "help": "半自动：脚本填该号(E.164)+选WhatsApp，你只手输码"},
            {"flag": "--group", "type": "str", "default": "", "help": "SUB2API 目标分组(默认取配置)"},
            {"flag": "--skip-cpa", "type": "bool", "default": False, "help": "不推 CPA(默认配好就推)"},
            {"flag": "--keep", "type": "bool", "default": False, "help": "失败保留窗口便于排查"},
        ],
    },
    # ---------------------------------------------------------------- 单平台注册
    {
        "id": "register_chatgpt",
        "file": "register_chatgpt.py",
        "platform": "chatgpt",
        "category": "单平台注册",
        "title": "ChatGPT 注册",
        "desc": "ChatGPT 单平台注册，支持 iCloud 接码，并可在成功后直接导入 SUB2API。",
        "args": [
            {"flag": "--count", "type": "int", "default": 1, "help": "注册数量"},
            {"flag": "--timeout", "type": "int", "default": 480, "help": "单号超时(秒)"},
            {"flag": "--node", "type": "str", "default": "auto",
             "help": "固定 Clash 出口；auto 自动探测，none 直连"},
            {"flag": "--email-provider", "type": "choice", "choices": ["", "pool", "icloud"],
             "labels": {"": "按 .env 配置", "pool": "Outlook 邮箱池", "icloud": "iCloud API 接码"},
             "default": "", "help": "邮箱来源；留空使用 CHATGPT_EMAIL_PROVIDER 配置"},
            {"flag": "--codex", "type": "bool", "default": False,
             "help": "注册成功后直接导入 SUB2API；需配置 SUB2API_*，OAuth 期间可能需要手机验证"},
            {"flag": "--import-c2a", "type": "bool", "default": False, "help": "注册后导入 chatgpt2api"},
            {"flag": "--concurrency", "type": "int", "default": 1, "help": "并发数；ChatGPT 当前按单窗口运行"},
            {"flag": "--email", "type": "str", "default": "", "help": "指定邮箱(绕过池)"},
            {"flag": "--password", "type": "str", "default": "", "help": "邮箱密码"},
            {"flag": "--refresh-token", "type": "str", "default": "", "help": "Outlook refresh_token"},
            {"flag": "--client-id", "type": "str", "default": "", "help": "Outlook OAuth client_id"},
            {"flag": "--codex-group", "type": "str", "default": "", "help": "SUB2API 目标分组名；留空使用配置值"},
            {"flag": "--codex-sms-provider", "type": "choice", "choices": ["auto", "hero", "smsman", "firefox"],
             "labels": {"auto": "自动切换", "hero": "Hero SMS", "smsman": "SMS-Man", "firefox": "firefox.fun"},
             "default": "auto", "help": "直接导入 SUB2API 时的手机接码平台"},
            {"flag": "--codex-manual-phone", "type": "bool", "default": False, "help": "Codex 手动填号收码"},
            {"flag": "--keep-on-fail", "type": "bool", "default": False, "help": "失败保留窗口"},
        ],
    },
    {
        "id": "register_grok",
        "file": "register_grok.py",
        "platform": "grok",
        "category": "单平台注册",
        "title": "Grok 注册",
        "desc": "使用指纹浏览器注册 Grok，支持 Outlook 或配置的临时邮箱，成功后可导入 SUB2API。",
        "args": [
            {"flag": "--count", "type": "int", "default": 1, "help": "注册数量"},
            {"flag": "--concurrency", "type": "int", "default": 1, "help": "并发数"},
            {"flag": "--timeout", "type": "int", "default": 600, "help": "单号超时(秒)"},
            {"flag": "--node", "type": "str", "default": "auto",
             "help": "Clash 模式下的出口节点；auto 自动探测，住宅模式下忽略"},
            {"flag": "--latest-rt", "type": "bool", "default": False,
             "help": "使用 emails.txt 中最新未占用且带 RT/client_id 的 Outlook，Graph API 取码"},
            {"flag": "--sub2api", "type": "bool", "default": False,
             "help": "注册成功后直接导入 SUB2API Grok 渠道"},
            {"flag": "--sub2api-group", "type": "str", "default": "",
             "help": "目标 Grok 分组名，留空使用 SUB2API_GROK_GROUP"},
            {"flag": "--mailbox-attempts", "type": "int", "default": 6,
             "help": "发码未进入验证码页时自动更换临时邮箱的次数"},
            {"flag": "--keep-on-fail", "type": "bool", "default": False,
             "help": "失败时保留测试窗口"},
        ],
    },
    {
        "id": "register_claude",
        "file": "register.py",
        "platform": "claude",
        "category": "单平台注册",
        "title": "Claude 注册",
        "desc": "Claude 单平台注册；支持 YYDS 临时邮箱，自动处理节点预检、magic-link 原生验证和 hCaptcha，成功后保存 sessionKey。",
        "warning": "运行前必须在“配置 (.env) → Claude 注册与验证”中填写视觉 API 地址和 key，否则无法自动通过图形验证。",
        "args": [
            {"flag": "--count", "type": "int", "default": 1, "help": "注册数量"},
            {"flag": "--concurrency", "type": "int", "default": 1, "help": "并发数"},
            {"flag": "--timeout", "type": "int", "default": 480, "help": "单号超时(秒)"},
            {"flag": "--provider", "type": "choice",
             "choices": ["", "yyds", "gptmail", "cfmail", "moemail", "custom"], "default": "",
             "labels": {"": "Outlook 资产池", "yyds": "YYDS 临时邮箱", "gptmail": "GPTMail 临时邮箱", "cfmail": "CFMail 临时邮箱", "moemail": "MoeMail 临时邮箱", "custom": "自定义临时邮箱"},
             "help": "邮箱来源；选择临时邮箱后忽略 Outlook/最新 RT"},
            {"flag": "--domain", "type": "str", "default": "",
             "help": "临时邮箱域名；留空由 provider 自动选择"},
            {"flag": "--latest-rt", "type": "bool", "default": True,
             "help": "Outlook 模式默认使用 emails.txt 中最新未占用且可读 Graph 的 RT 邮箱"},
            {"flag": "--node", "type": "str", "default": "auto", "help": "Clash 模式下自动轮换；住宅模式下忽略"},
            {"flag": "--email", "type": "str", "default": "", "help": "指定邮箱(调试)"},
            {"flag": "--password", "type": "str", "default": "", "help": "邮箱密码"},
            {"flag": "--token", "type": "str", "default": "", "help": "refresh token"},
            {"flag": "--client-id", "type": "str", "default": "", "help": "与 refresh token 配套的 Outlook OAuth client_id"},
            {"flag": "--challenge-wait", "type": "int", "default": 45,
             "help": "每个节点等待 Cloudflare 自动验证的秒数"},
            {"flag": "--challenge-node-retries", "type": "int", "default": 3,
             "help": "提交邮箱前最多轮换节点次数"},
            {"flag": "--captcha-manual-timeout", "type": "int", "default": 0,
             "help": "等待手动图形验证秒数(0=关闭)"},
        ],
    },
    {
        "id": "register_kiro",
        "file": "register_kiro.py",
        "platform": "kiro",
        "category": "单平台注册",
        "title": "Kiro 注册",
        "desc": "使用 Outlook 资产池注册 AWS Builder ID，并导出 Kiro 所需的长期凭据。",
        "args": [
            {"flag": "--count", "type": "int", "default": 1, "help": "注册数量"},
            {"flag": "--timeout", "type": "int", "default": 600, "help": "单号超时(秒)"},
            {"flag": "--email", "type": "str", "default": "", "help": "指定邮箱(留空从 Outlook 资产池取)"},
            {"flag": "--password", "type": "str", "default": "", "help": "Outlook 邮箱密码(与邮箱资产一并记录)"},
            {"flag": "--refresh-token", "type": "str", "default": "", "help": "Outlook Graph refresh token"},
            {"flag": "--client-id", "type": "str", "default": "", "help": "Outlook OAuth client_id"},
            {"flag": "--account-password", "type": "str", "default": "", "help": "Kiro 账号密码(留空随机生成)"},
            {"flag": "--full-name", "type": "str", "default": "Test User", "help": "Builder ID 显示名称"},
        ],
    },
    {
        "id": "register_github",
        "file": "register_github.py",
        "category": "单平台注册",
        "title": "GitHub 注册",
        "desc": "GitHub 注册(含 Arkose 验证视觉求解，需配 VISION_*/VOTE_*)。",
        "args": [
            {"flag": "--auto", "type": "bool", "default": False, "help": "走完整流程(含取 launch code)"},
            {"flag": "--email", "type": "str", "default": "", "help": "指定邮箱(默认从 _outlook_pool 取)"},
            {"flag": "--password", "type": "str", "default": "", "help": "邮箱密码"},
            {"flag": "--no-keep", "type": "bool", "default": False, "help": "结束后删窗口(默认保留)"},
            {"flag": "--timeout", "type": "int", "default": 600, "help": "超时(秒)"},
        ],
    },
    # ---------------------------------------------------------------- 邮箱注册
    {
        "id": "outlook_reg_loop",
        "file": "outlook_reg_loop.py",
        "platform": "outlook",
        "category": "邮箱注册",
        "title": "Outlook 邮箱注册",
        "desc": "持续自注册 Outlook，产出到 _outlook_pool/ 与 emails.txt。count=0 为无限循环。",
        "args": [
            {"flag": "--count", "type": "int", "default": 0, "help": "注册数量(0=无限循环)"},
            {"flag": "--target-pool", "type": "int", "default": 0, "help": "池达到此数量就停(0=不限)"},
            {"flag": "--max-press", "type": "str", "default": "5", "help": "人机验证按住次数上限"},
            {"flag": "--confirm-before-register", "type": "bool", "default": False,
             "help": "注册页打开后仅在出现数据许可页时才点确认(正常表单不点)"},
            {"flag": "--timeout", "type": "int", "default": 180, "help": "单号注册超时(秒)"},
            {"flag": "--sleep", "type": "int", "default": 5, "help": "每次注册间隔(秒)"},
            {"flag": "--sleep-when-full", "type": "int", "default": 60, "help": "池达标时的休眠间隔(秒)"},
            {"flag": "--no-rotate", "type": "bool", "default": False,
             "help": "不轮换 Clash 节点，固定用当前节点(也可用 OUTLOOK_NO_ROTATE=1)"},
        ],
    },
    {
        "id": "unlock_outlook",
        "file": "unlock_outlook.py",
        "platform": "outlook",
        "category": "邮箱注册",
        "title": "解锁 Outlook + 提取 Graph",
        "desc": "一次处理待解锁、RT 过期或缺少 RT 的 Outlook；恢复登录后提取 Graph token 并回写邮箱池。",
        "args": [
            {"flag": "--input", "type": "str", "default": "", "help": "账号文件(每行 email----password；留空读取资产状态)"},
            {"flag": "--proxy-file", "type": "str", "default": "", "help": "住宅代理池文件"},
            {"flag": "--concurrency", "type": "int", "default": 1, "help": "并发数"},
            {"flag": "--statuses", "type": "multi",
             "choices": ["unlock", "expired", "unknown", "banned"],
             "default": ["unlock", "expired", "unknown"],
             "help": "自动模式处理的资产状态；unknown 仅包含缺少 RT 的邮箱，封禁账号需手动勾选"},
        ],
    },
    {
        "id": "mailbox_broker",
        "file": "mailbox_broker.py",
        "category": "邮箱注册",
        "title": "共享取码服务(常驻)",
        "desc": "并行流水线时起共享取码服务，避免三窗口并发登录同一邮箱。常驻运行。",
        "args": [
            {"flag": "--host", "type": "str", "default": "127.0.0.1", "help": "监听地址"},
            {"flag": "--port", "type": "int", "default": 8765, "help": "监听端口"},
            {"flag": "--idle", "type": "int", "default": 480, "help": "空闲会话回收秒数"},
        ],
    },
    # ---------------------------------------------------------------- 导出 / 上传
    {
        "id": "upload_tokens",
        "file": "tools/upload_tokens.py",
        "category": "导出/上传",
        "title": "上传标准 token",
        "desc": "把 tokens/ 下标准 token 上传到 CPA/SUB2API/webchat2api；Grok 会导入 SUB2API Grok 渠道。",
        "args": [
            {"flag": "target", "type": "choice", "choices": ["all", "chatgpt", "grok"],
             "default": "all", "positional": True, "help": "上传目标"},
        ],
    },
    {
        "id": "export_chatgpt2api",
        "file": "tools/export_chatgpt2api.py",
        "category": "导出/上传",
        "title": "导出/上传 chatgpt2api",
        "desc": "聚合普通网页号导入 chatgpt2api(--post 直传 / 默认导出 txt)。",
        "args": [
            {"flag": "--post", "type": "str", "default": "", "help": "直接 POST 到的 host(留空只导出文件)"},
            {"flag": "--key", "type": "str", "default": "", "help": "chatgpt2api admin key(配合 --post)"},
            {"flag": "--json", "type": "bool", "default": False, "help": "导出 JSON(默认一行一个 access_token)"},
            {"flag": "--out", "type": "str", "default": "", "help": "输出文件路径"},
        ],
    },
    {
        "id": "export_accounts",
        "file": "tools/export_accounts.py",
        "category": "导出/上传",
        "title": "导出平台 Cookie",
        "desc": "按平台和 index 导出标准浏览器 Cookie，或生成直登扩展账号包。",
        "args": [
            {"flag": "--platform", "type": "choice", "choices": ["all", "claude", "chatgpt", "grok"],
             "default": "all", "help": "cookies 格式必须指定一个平台"},
            {"flag": "--format", "type": "choice", "choices": ["accounts", "cookies"],
             "default": "accounts", "help": "cookies 为标准浏览器导入数组"},
            {"flag": "--index", "type": "int", "default": -1,
             "help": "-1=账号包全部/标准 Cookie 第一条；其他值精确指定账号"},
            {"flag": "--out", "type": "str", "default": "", "help": "可选输出 JSON 路径"},
        ],
    },
    {
        "id": "install_ruyipage",
        "file": "tools/install_ruyipage.py",
        "category": "配置工具",
        "title": "安装 RuyiPage Firefox",
        "desc": "下载并安装 RuyiPage 配套的 Firefox 指纹浏览器 runtime。首次使用默认浏览器时运行一次。",
        "args": [],
    },
]


def script_by_id(sid):
    for s in SCRIPTS:
        if s["id"] == sid:
            return s
    return None


# ============================================================ 外部工具链接
# 不在本机跑的 web 服务/工具，面板上以"打开链接"卡片呈现(新标签打开)。
EXTERNAL_LINKS = [
    {
        "id": "download_clash_verge",
        "title": "下载 Clash Verge 2.5.2",
        "url": "https://github.com/clash-verge-rev/clash-verge-rev/releases/download/v2.5.2/Clash.Verge_2.5.2_x64-setup.exe",
        "desc": "Windows x64 安装包",
    },
    {
        "id": "download_bitbrowser",
        "title": "下载 BitBrowser",
        "url": "https://www.bitbrowser.cn/download",
        "desc": "BitBrowser 官方下载页",
    },
    {
        "id": "ruyipage_home",
        "title": "RuyiPage 项目主页",
        "url": "https://github.com/LoseNine/ruyipage",
        "desc": "Firefox WebDriver BiDi 指纹浏览器",
    },
]


# ============================================================ 内嵌功能页
# 直接在面板里 iframe 内嵌的外部页面 + 可选 sms-man 接码助手。
# 空列表时前端不渲染「功能」导航区(app.js: if(EMBEDS.length))，内嵌视图/接码助手一并隐藏。
# Gmail 注册已下线；如需重新内嵌某页，按下面注释里的字段格式往列表里加即可。
#   {"id": "xxx", "title": "标题", "url": "https://...", "desc": "说明",
#    "sms_helper": True, "sms_service_default": "google"}
EMBED_PAGES = []


# ============================================================ .env 配置 schema
# group: 分组标题；key: 变量名；required: 是否必填(运行对应功能时)；help: 说明；
# secret: True 时前端用密码框；default: 模板默认值(仅展示)。
ENV_SCHEMA = [
    {"group": "Codex K12 控制台", "tests": [{"target": "k12", "label": "测试 K12 控制台"}], "items": [
        {"key": "K12_CONSOLE_URL", "default": "http://127.0.0.1:8806",
         "help": "主 WebUI 内嵌的 Codex K12 地址。本地地址会由主 WebUI 自动管理进程。"},
        {"key": "K12_AUTO_START", "type": "choice", "choices": ["1", "0"], "default": "1",
         "help": "启动主 WebUI 时是否自动拉起本地 Codex K12 服务。"},
    ]},
    {"group": "本地资产 API", "items": [
        {"key": "REG_FACTORY_ASSET_API_KEY", "secret": True,
         "help": "可选；配置后调用邮箱/Cookie 读取 API 必须提供 X-API-Key 或 Bearer Token。留空时仅允许本机访问。"},
    ]},
    {"group": "网络出口", "tests": [{"target": "clash", "label": "测试 Clash 连通"}], "items": [
        {"key": "PROXY_MODE", "type": "choice", "choices": ["clash_auto", "clash_fixed", "residential"],
         "default": "clash_auto", "help": "Clash 自动轮换、固定节点，或动态住宅代理。建议在左侧“网络出口”页配置。"},
        {"key": "OUTLOOK_PROXY_MODE", "type": "choice", "choices": ["inherit", "clash_auto", "clash_fixed", "residential"],
         "default": "inherit", "help": "Outlook 出口覆盖；inherit 使用全局模式。"},
        {"key": "CLAUDE_PROXY_MODE", "type": "choice", "choices": ["inherit", "clash_auto", "clash_fixed", "residential"],
         "default": "inherit", "help": "Claude 出口覆盖；可与 Outlook 使用不同出口。"},
        {"key": "CHATGPT_PROXY_MODE", "type": "choice", "choices": ["inherit", "clash_auto", "clash_fixed", "residential"],
         "default": "inherit", "help": "ChatGPT 出口覆盖；可与 Outlook 使用不同出口。"},
        {"key": "GROK_PROXY_MODE", "type": "choice", "choices": ["inherit", "clash_auto", "clash_fixed", "residential"],
         "default": "inherit", "help": "Grok 出口覆盖；可与 Outlook 使用不同出口。"},
        {"key": "KIRO_PROXY_MODE", "type": "choice", "choices": ["inherit", "clash_auto", "clash_fixed", "residential"],
         "default": "inherit", "help": "Kiro 出口覆盖；可与 Outlook 使用不同出口。"},
        {"key": "CLASH_SECRET", "required": True, "secret": True,
         "help": "Clash Verge → 设置 → 外部控制器(External Controller) 里设的 secret/密钥。"
                 "若该处留空,这里也留空。设了密钥不填会连不上控制器(节点切换失效)。"},
        {"key": "CLASH_API", "default": "http://127.0.0.1:9097",
         "help": "Clash 控制器地址。Clash Verge 默认端口 9097,mihomo 内核默认 9090。在 外部控制器 页可看到。"},
        {"key": "CLASH_PROXY", "default": "http://127.0.0.1:7897",
         "help": "Clash 混合代理端口(mixed-port),脚本走它出网。Verge 默认 7897。"},
        {"key": "CLASH_GROUP", "default": "GLOBAL",
         "help": "决定出口的代理组名。global 模式下填 GLOBAL;规则模式填你的节点选择组名。"},
        {"key": "CLASH_FIXED_NODE", "help": "固定节点模式使用的 Clash 节点全名。"},
        {"key": "REG_FACTORY_PROXY", "secret": True,
         "help": "住宅代理 URL，例如 http://user:pass@host:port 或 socks5://..."},
        {"key": "REG_FACTORY_PROXY_POOL", "secret": True,
         "help": "住宅代理池；.env 中使用逗号分隔，专用网络页支持逐行填写。"},
        {"key": "REG_FACTORY_PROXY_ROTATE_URL", "secret": True,
         "help": "可选的住宅代理供应商换 IP 接口。"},
        {"key": "REG_FACTORY_PROXY_ROTATE_METHOD", "type": "choice", "choices": ["GET", "POST"],
         "default": "GET", "help": "调用换 IP 接口时使用的方法。"},
        {"key": "CHATGPT_RESIDENTIAL_ROTATE_RETRIES", "default": "3",
         "help": "ChatGPT 遇到 Cloudflare 拦截时最多轮换住宅 IP 的次数。"},
    ]},
    {"group": "指纹浏览器", "tests": [{"target": "bitbrowser", "label": "测试 指纹浏览器连通"}], "items": [
        {"key": "FINGERPRINT_BROWSER", "type": "choice", "choices": ["ruyipage", "bundled", "custom", "bitbrowser", "adspower", "custom_api"],
         "default": "ruyipage", "help": "ruyipage=默认 Firefox BiDi；旧 Chromium 专用流程会自动使用 bundled。"},
        {"key": "RUYIPAGE_BROWSER_PATH", "help": "可选的 RuyiPage Firefox 可执行文件；留空使用一键安装的 runtime。"},
        {"key": "REG_FACTORY_BROWSER_PATH", "help": "内置 Chromium 可执行文件路径"},
        {"key": "CUSTOM_BROWSER_PATH", "help": "custom 模式的 Chrome/Chromium 可执行文件；留空会尝试自动查找。"},
        {"key": "CUSTOM_BROWSER_API", "help": "custom_api 模式的本地 API 地址，必须兼容 BitBrowser 的 browser/* 接口。"},
        {"key": "REG_FACTORY_BROWSER_HELPER", "help": "内置 Chromium 启动器路径"},
        {"key": "BITBROWSER_API", "default": "http://127.0.0.1:54345", "help": "比特浏览器本地 API"},
        {"key": "BB_CORE_VERSION", "default": "146",
         "help": "Outlook 自注册等共享 BitBrowser 流程的首选 Chromium 内核"},
        {"key": "OUTLOOK_BROWSER_FALLBACK_CORE_VERSION", "default": "130",
         "help": "Outlook 首选内核安装失败时自动回退的内核；留空禁用"},
        {"key": "GROK_BROWSER_CORE_VERSION", "default": "146",
         "help": "Grok 浏览器使用的 BitBrowser Chromium 内核；旧 130 会触发 xAI 发码 403"},
        {"key": "ADSPOWER_API", "default": "http://127.0.0.1:50325", "help": "AdsPower 本地 API"},
        {"key": "ADSPOWER_API_KEY", "secret": True, "help": "AdsPower API key，未启用鉴权时留空"},
        {"key": "ADSPOWER_GROUP_ID", "default": "0", "help": "AdsPower 新建 profile 的分组 ID"},
    ]},
    {"group": "Claude 注册与验证", "items": [
        {"key": "CLAUDE_CHALLENGE_WAIT_SECONDS", "default": "45",
         "help": "每个 Clash 节点等待 Cloudflare 自动放行的秒数"},
        {"key": "CLAUDE_CHALLENGE_NODE_RETRIES", "default": "3",
         "help": "auto 模式提交邮箱前最多轮换节点次数"},
        {"key": "CLAUDE_RESIDENTIAL_PROFILE_RETRIES", "default": "3",
         "help": "住宅出口在提交邮箱前被拒绝时，新建浏览器会话的总尝试次数"},
        {"key": "CLAUDE_ALLOW_ROTATING_PROXY", "type": "bool", "default": False,
         "help": "跳过 Claude 住宅代理粘性检测；仅用于已确认出口连续性的特殊代理"},
        {"key": "CLAUDE_NODE_PROBE_LIMIT", "default": "6",
         "help": "启动时最多快速探测的候选节点数"},
        {"key": "CLAUDE_NODE_PROBE_TIMEOUT_SECONDS", "default": "8",
         "help": "单个候选节点的快速探测超时秒数"},
        {"key": "CLAUDE_CAPTCHA_MANUAL_TIMEOUT", "default": "0",
         "help": "自动求解失败后等待人工接管秒数；0 表示关闭"},
        {"key": "CLAUDE_HCAPTCHA_SOLVE_RETRIES", "default": "2",
         "help": "YesCaptcha 备用求解器的瞬时失败重试次数"},
        {"key": "CLAUDE_VISION_API_BASE", "required": True,
         "help": "必填：Claude hCaptcha 视觉解码 API 地址；未配置无法自动通过图形验证"},
        {"key": "CLAUDE_VISION_API_KEY", "required": True, "secret": True,
         "help": "必填：Claude hCaptcha 视觉解码 API key"},
        {"key": "CLAUDE_VISION_MODEL", "default": "gemini-3.6-flash",
         "help": "Claude hCaptcha 主视觉模型"},
        {"key": "CLAUDE_BROWSER_CORE_VERSION", "default": "146",
         "help": "Claude 注册和 sessionKey 校验使用的 Chromium 指纹版本"},
        {"key": "CLAUDE_BROWSER_FALLBACK_CORE_VERSION", "default": "130",
         "help": "首选内核安装失败时，本次 Claude 注册自动回退的 BitBrowser 内核；留空禁用"},
    ]},
    {"group": "短信接码", "tests": [{"target": "smsman", "label": "测试 sms-man"}, {"target": "firefox", "label": "测试 firefox.fun"}], "items": [
        {"key": "SMS_API_NAME", "help": "firefox.fun APIName；在 API 对接页面查看，通常为账号名加两位随机数字"},
        {"key": "SMS_TOKEN", "secret": True, "help": "firefox.fun 持久 token；取号时还会使用 OpenAI 项目 ID"},
        {"key": "HERO_SMS_API_KEY", "secret": True, "help": "hero-sms.com 备用接码 key"},
        {"key": "SMSMAN_TOKEN", "secret": True, "help": "sms-man.com 接码 key(Codex add-phone 主用)"},
        {"key": "SMSMAN_APP_ID_OPENAI", "default": "openai", "help": "sms-man OpenAI 服务 id(openai 自动解析为 2754)"},
        {"key": "SMSMAN_APP_ID_GMAIL", "default": "google", "help": "sms-man Gmail/Google 服务 id(google 自动解析;接码助手默认用它)"},
        {"key": "SMSMAN_COUNTRY_ID_OPENAI", "default": "0", "help": "国家 id(0=随机/按价格)"},
    ]},
    {"group": "hero-sms 价格与服务", "items": [
        {"key": "HERO_SMS_SERVICE_OPENAI", "default": "dr", "help": "hero-sms OpenAI 服务码(dr=sms-activate 兼容)"},
        {"key": "HERO_SMS_MAXPRICE_OPENAI", "default": "1.0", "help": "hero-sms 价格上限(USD)；0或留空=不限"},
        {"key": "SMS_MAXPRICE_OPENAI", "default": "20", "help": "firefox.fun 价格上限(0=只取最便宜)"},
    ]},
    {"group": "打码平台(可选)", "items": [
        {"key": "CAPSOLVER_API_KEY", "secret": True, "help": "CapSolver 打码 key"},
        {"key": "EZCAPTCHA_API_KEY", "secret": True, "help": "EZ-Captcha 打码 key(解锁 Outlook 用)"},
        {"key": "YESCAPTCHA_API_KEY", "secret": True, "help": "YesCaptcha key (Claude hCaptcha / Grok Turnstile / GitHub Arkose)"},
    ]},
    {"group": "Outlook 自注册", "items": [
        {"key": "OUTLOOK_PROXIES", "help": "Outlook 自注册住宅代理池(换行/逗号分隔)"},
    ]},
    {"group": "ChatGPT 邮箱", "items": [
        {"key": "CHATGPT_EMAIL_PROVIDER", "type": "choice", "choices": ["pool", "icloud"],
         "default": "pool", "help": "ChatGPT 默认邮箱来源；icloud 使用下方 API 自动申请并轮询验证码"},
        {"key": "ICLOUD_MAIL_API_BASE", "default": "https://mail.no-replyca.xyz",
         "help": "iCloud 邮箱 API 请求地址；email.manageh.shop 是文档站，不是接口地址"},
        {"key": "ICLOUD_MAIL_API_KEY", "secret": True, "help": "iCloud 邮箱 API key"},
        {"key": "ICLOUD_MAIL_TYPE", "type": "choice", "choices": ["icloud-code", "icloud"],
         "default": "icloud-code", "help": "icloud-code=ChatGPT 接码类型；icloud=普通 iCloud 子邮箱"},
        {"key": "ICLOUD_MAIL_SERVICE", "default": "openai", "help": "接码服务名，ICLOUD_MAIL_TYPE=icloud-code 时使用"},
    ]},
    {"group": "临时邮箱(Claude/Grok 注册取码)", "tests": [{"target": "yyds", "label": "测试 YYDS"}], "items": [
        {"key": "CLAUDE_USE_TEMP_EMAIL", "type": "choice", "choices": ["false", "true"], "default": "false",
         "help": "Claude 命令行未指定邮箱时默认使用下方临时邮箱；任务表单选择 provider 时无需打开"},
        {"key": "TEMP_EMAIL_PROVIDER", "type": "choice",
         "choices": ["yyds", "gptmail", "moemail", "cfmail", "icloud", "custom"], "default": "yyds",
         "help": "Claude/Grok 默认临时邮箱 provider(需配好对应 key)，任务表单也可临时指定。"},
        {"key": "YYDS_API_KEY", "secret": True, "help": "YYDS Mail key(profile 页,AC- 开头)"},
        {"key": "YYDS_BASE_URL", "default": "https://maliapi.215.im", "help": "YYDS Mail API 根地址；可粘贴 vip.215.im 或完整 /v1/accounts 地址，程序会自动纠正"},
        {"key": "GPTMAIL_API_KEY", "secret": True, "help": "GPTMail key(mail.chatgpt.org.uk)"},
        {"key": "MOEMAIL_API_KEY", "secret": True, "help": "MoeMail key(自部署)"},
        {"key": "MOEMAIL_BASE_URL", "help": "MoeMail 自部署地址"},
        {"key": "CFMAIL_ADMIN_PASSWORD", "secret": True, "help": "Cloudflare Temp Email admin 密码(自部署)"},
        {"key": "CFMAIL_BASE_URL", "help": "Cloudflare Temp Email 地址"},
    ]},
    {"group": "SUB2API(Codex / Grok 导入)", "items": [
        {"key": "SUB2API_URL", "help": "SUB2API 管理接口地址"},
        {"key": "SUB2API_EMAIL", "help": "SUB2API 登录邮箱"},
        {"key": "SUB2API_PASSWORD", "secret": True, "help": "SUB2API 登录密码"},
        {"key": "SUB2API_GROUP", "default": "codex", "help": "Codex 目标分组名(platform=openai)"},
        {"key": "SUB2API_GROK_GROUP", "default": "grok", "help": "Grok 目标分组名(platform=grok，需后台先建好)"},
        {"key": "SUB2API_GROK_PROXY_ID", "type": "int", "default": 0,
         "help": "可选：SUB2API 后台代理 ID，供 Grok SSO 转 OAuth 使用；0=不指定"},
    ]},
    {"group": "CPA(codex 授权文件导入)", "items": [
        {"key": "CPA_URL", "help": "CPA 管理接口地址"},
        {"key": "CPA_MGMT_KEY", "secret": True, "help": "CPA 管理 key"},
    ]},
    {"group": "chatgpt2api(普通网页号)", "items": [
        {"key": "CHATGPT2API_URL", "help": "chatgpt2api host(用 --import-c2a 时必填)"},
        {"key": "CHATGPT2API_KEY", "secret": True, "help": "chatgpt2api admin key"},
    ]},
    {"group": "webchat2api(Grok sso)", "items": [
        {"key": "WEBCHAT2API_URL", "help": "webchat2api 地址(用 Grok 时)"},
        {"key": "WEBCHAT2API_KEY", "secret": True, "help": "webchat2api key"},
    ]},
    {"group": "OmniRoute(Kiro 账号导入)", "items": [
        {"key": "OMNIROUTE_URL", "help": "OmniRoute 管理地址，如 http://localhost:20128"},
        {"key": "OMNIROUTE_PASSWORD", "secret": True, "help": "OmniRoute 管理控制台登录密码"},
    ]},
    {"group": "Codex add-phone 接码调参", "items": [
        {"key": "CODEX_PHONE_SKIP_ATTEMPTS", "default": "0", "help": "先赌免手机次数(0=直接接码)"},
        {"key": "CODEX_ADDPHONE_ATTEMPTS", "default": "2", "help": "接码换号上限次数"},
        {"key": "CODEX_SMS_TIMEOUT", "default": "150", "help": "单号等码超时(秒)"},
        {"key": "SMS_COUNTRY_BLACKLIST_OPENAI", "default": "261,63", "help": "拉黑号段(dialing code)"},
    ]},
    {"group": "视觉模型与投票(可选)", "items": [
        {"key": "VISION_API_BASE", "help": "Claude/GitHub 共用的主视觉网关(OpenAI 兼容)"},
        {"key": "VISION_API_KEY", "secret": True, "help": "主视觉网关 key"},
        {"key": "VOTE_ZZ_BASE", "help": "投票中转网关(gemini/gpt)"},
        {"key": "VOTE_ZZ_KEY", "secret": True, "help": "投票网关 gemini key"},
        {"key": "VOTE_GPT_KEY", "secret": True, "help": "投票网关 gpt key"},
        {"key": "VOTE_OPUS_BASE", "help": "claude opus 专用网关"},
        {"key": "VOTE_OPUS_KEY", "secret": True, "help": "opus 网关 key"},
    ]},
]


# 所有 schema 里出现的 .env key（用于读 .env 时补齐未在模板里的项不丢）
def env_keys():
    keys = []
    for g in ENV_SCHEMA:
        for it in g["items"]:
            keys.append(it["key"])
    return keys
