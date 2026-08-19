# -*- coding: utf-8 -*-
"""
config.py — 全局配置。

所有密钥/凭据都从环境变量读取（默认空），不在仓库里留明文。
支持把变量写进同目录的 .env 文件（见 .env.example）；.env 只在对应环境
变量尚未设置时生效，不会覆盖真实的进程环境变量。
"""

import os


# ---------------------------------------------------------------- .env 加载
def _load_dotenv(path=None):
    """零依赖 .env 读取器：解析 KEY=VALUE，忽略空行与 # 注释。
    只在 os.environ 里尚未设置该 KEY 时填入（真实环境变量优先）。"""
    if path is None:
        path = os.environ.get("REG_FACTORY_ENV_FILE") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), ".env"
        )
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


_load_dotenv()


def _env(name, default=""):
    return os.environ.get(name, default)


def _env_int(name, default):
    try:
        return int(_env(name, str(default)) or default)
    except (TypeError, ValueError):
        return int(default)


# ---------------------------------------------------------------- 本地基建
# Fingerprint browser provider: bitbrowser / bundled / custom / adspower / custom_api
FINGERPRINT_BROWSER = _env("FINGERPRINT_BROWSER", "bitbrowser").strip().lower()

# BitBrowser 本地 API 地址
BITBROWSER_API = _env("BITBROWSER_API", "http://127.0.0.1:54345")

# AdsPower 本地 API 地址
ADSPOWER_API = _env("ADSPOWER_API", "http://127.0.0.1:50325")
ADSPOWER_API_KEY = _env("ADSPOWER_API_KEY", "")
ADSPOWER_GROUP_ID = _env("ADSPOWER_GROUP_ID", "0")

# Custom fingerprint-browser API. `auto` preserves the legacy BitBrowser
# payload; `generic` supports common REST APIs with configurable paths.
CUSTOM_BROWSER_API = _env("CUSTOM_BROWSER_API", "")
CUSTOM_BROWSER_API_MODE = _env("CUSTOM_BROWSER_API_MODE", "auto").strip().lower() or "auto"
CUSTOM_BROWSER_API_KEY = _env("CUSTOM_BROWSER_API_KEY", "")
CUSTOM_BROWSER_API_AUTH_HEADER = _env("CUSTOM_BROWSER_API_AUTH_HEADER", "Authorization")
CUSTOM_BROWSER_API_AUTH_PREFIX = _env("CUSTOM_BROWSER_API_AUTH_PREFIX", "Bearer ")
CUSTOM_BROWSER_API_HEADERS = _env("CUSTOM_BROWSER_API_HEADERS", "")
CUSTOM_BROWSER_API_TIMEOUT = _env_int("CUSTOM_BROWSER_API_TIMEOUT", 60)
CUSTOM_BROWSER_API_VERIFY_TLS = _env("CUSTOM_BROWSER_API_VERIFY_TLS", "true").strip().lower() in (
    "1", "true", "yes", "on"
)
CUSTOM_BROWSER_API_ID_FIELD = _env("CUSTOM_BROWSER_API_ID_FIELD", "id")
CUSTOM_BROWSER_API_HEALTH_PATH = _env("CUSTOM_BROWSER_API_HEALTH_PATH", "/health")
CUSTOM_BROWSER_API_CREATE_PATH = _env("CUSTOM_BROWSER_API_CREATE_PATH", "/browser/update")
CUSTOM_BROWSER_API_LIST_PATH = _env("CUSTOM_BROWSER_API_LIST_PATH", "/browser/list")
CUSTOM_BROWSER_API_OPEN_PATH = _env("CUSTOM_BROWSER_API_OPEN_PATH", "/browser/open")
CUSTOM_BROWSER_API_CLOSE_PATH = _env("CUSTOM_BROWSER_API_CLOSE_PATH", "/browser/close")
CUSTOM_BROWSER_API_DELETE_PATH = _env("CUSTOM_BROWSER_API_DELETE_PATH", "/browser/delete")
CUSTOM_BROWSER_API_UPDATE_PATH = _env("CUSTOM_BROWSER_API_UPDATE_PATH", "/browser/update/partial")
CUSTOM_BROWSER_API_CREATE_METHOD = _env("CUSTOM_BROWSER_API_CREATE_METHOD", "POST").upper()
CUSTOM_BROWSER_API_LIST_METHOD = _env("CUSTOM_BROWSER_API_LIST_METHOD", "POST").upper()
CUSTOM_BROWSER_API_OPEN_METHOD = _env("CUSTOM_BROWSER_API_OPEN_METHOD", "POST").upper()
CUSTOM_BROWSER_API_CLOSE_METHOD = _env("CUSTOM_BROWSER_API_CLOSE_METHOD", "POST").upper()
CUSTOM_BROWSER_API_DELETE_METHOD = _env("CUSTOM_BROWSER_API_DELETE_METHOD", "POST").upper()
CUSTOM_BROWSER_API_UPDATE_METHOD = _env("CUSTOM_BROWSER_API_UPDATE_METHOD", "POST").upper()
CUSTOM_BROWSER_API_FORWARD_FIELDS = _env("CUSTOM_BROWSER_API_FORWARD_FIELDS", "false").strip().lower() in (
    "1", "true", "yes", "on"
)

# Claude.ai 注册相关 URL
CLAUDE_LOGIN_URL = "https://claude.ai/login"
CLAUDE_CHALLENGE_WAIT_SECONDS = _env_int("CLAUDE_CHALLENGE_WAIT_SECONDS", 45)
CLAUDE_CHALLENGE_NODE_RETRIES = _env_int("CLAUDE_CHALLENGE_NODE_RETRIES", 3)
CLAUDE_CAPTCHA_MANUAL_TIMEOUT = _env_int("CLAUDE_CAPTCHA_MANUAL_TIMEOUT", 0)
CLAUDE_HCAPTCHA_SOLVE_RETRIES = _env_int("CLAUDE_HCAPTCHA_SOLVE_RETRIES", 2)
CLAUDE_VISION_API_BASE = _env("CLAUDE_VISION_API_BASE", "")
CLAUDE_VISION_API_KEY = _env("CLAUDE_VISION_API_KEY", "")
CLAUDE_VISION_MODEL = _env("CLAUDE_VISION_MODEL", "gemini-3.6-flash")
CLAUDE_NODE_PROBE_LIMIT = _env_int("CLAUDE_NODE_PROBE_LIMIT", 6)
CLAUDE_NODE_PROBE_TIMEOUT_SECONDS = _env_int("CLAUDE_NODE_PROBE_TIMEOUT_SECONDS", 8)

# Cookie 输出目录
COOKIE_OUTPUT_DIR = "cookies"

# ---------------------------------------------------------------- 域名邮箱（备用）
MAIL_DOMAIN = _env("MAIL_DOMAIN", "")
MAIL_API_BASE = _env("MAIL_API_BASE", "")
MAIL_ADMIN_USER = _env("MAIL_ADMIN_USER", "admin")
MAIL_ADMIN_PASS = _env("MAIL_ADMIN_PASS", "")
# JWT token（从浏览器抓取，可能会过期需要更新）
MAIL_AUTH_TOKEN = _env("MAIL_AUTH_TOKEN", "")
# 新建邮箱统一密码
MAIL_NEW_PASS = _env("MAIL_NEW_PASS", "")

# ---------------------------------------------------------------- 临时邮箱（纯 HTTP API 取码，Grok/Claude 注册用）
# 参考 grokcli-2api：用临时邮箱 HTTP API 直接拉验证码，免去 Outlook 浏览器登录/轮询的重开销。
# GROK_USE_TEMP_EMAIL=true 时 register_grok.py 走临时邮箱；创建失败自动回退 emails.txt Outlook。
GROK_USE_TEMP_EMAIL = _env("GROK_USE_TEMP_EMAIL", "false").strip().lower() in ("1", "true", "yes", "on")
# CLAUDE_USE_TEMP_EMAIL=true 时 register.py 走临时邮箱取 magic link，免去 Outlook 注册/轮询。
CLAUDE_USE_TEMP_EMAIL = _env("CLAUDE_USE_TEMP_EMAIL", "false").strip().lower() in ("1", "true", "yes", "on")
# provider: moemail | yyds | gptmail | cfmail | icloud | remail（默认 gptmail）
TEMP_EMAIL_PROVIDER = _env("TEMP_EMAIL_PROVIDER", "gptmail").strip().lower() or "gptmail"

# Outlook Graph OAuth 遇到安全信息页时，绑定临时辅助邮箱并自动轮询验证码。
# provider 可设为 yyds | custom | outlook，也支持 yyds,outlook 这种指定备援的写法。
OUTLOOK_GRAPH_RECOVERY_EMAIL = _env(
    "OUTLOOK_GRAPH_RECOVERY_EMAIL", "true"
).strip().lower() in ("1", "true", "yes", "on")
OUTLOOK_GRAPH_RECOVERY_PROVIDER = _env(
    "OUTLOOK_GRAPH_RECOVERY_PROVIDER", "yyds"
).strip().lower() or "yyds"
# provider=outlook 时必填，格式：email----password----refresh_token----client_id。
# 密码仅保留作邮箱记录兼容；验证码读取和凭据校验均通过 Graph API。
OUTLOOK_GRAPH_RECOVERY_OUTLOOK_MAILBOX = _env(
    "OUTLOOK_GRAPH_RECOVERY_OUTLOOK_MAILBOX", ""
).strip()
OUTLOOK_GRAPH_RECOVERY_TIMEOUT = _env_int(
    "OUTLOOK_GRAPH_RECOVERY_TIMEOUT", 120
)
OUTLOOK_GRAPH_RECOVERY_POLL_INTERVAL = _env_int(
    "OUTLOOK_GRAPH_RECOVERY_POLL_INTERVAL", 5
)

# ChatGPT 邮箱来源：pool=emails.txt Outlook 池；icloud/Remail=自动申请邮箱并通过 API 取码。
CHATGPT_EMAIL_PROVIDER = _env("CHATGPT_EMAIL_PROVIDER", "pool").strip().lower() or "pool"
CHATGPT_ENABLE_2FA = _env("CHATGPT_ENABLE_2FA", "true").strip().lower() in (
    "1", "true", "yes", "on"
)

# MoeMail（beilunyang/moemail，需自部署）
MOEMAIL_BASE_URL = _env("MOEMAIL_BASE_URL", "https://moemail.example.com")
MOEMAIL_API_KEY = _env("MOEMAIL_API_KEY", "")
MOEMAIL_DOMAIN = _env("MOEMAIL_DOMAIN", "")  # 留空则运行时从已有邮箱推断
MOEMAIL_EXPIRY_MS = int(_env("MOEMAIL_EXPIRY_MS", "3600000") or "3600000")  # 1h|1d(86400000)|3d(259200000)|0永久

# YYDS Mail（vip.215.im / maliapi.215.im）
YYDS_BASE_URL = _env("YYDS_BASE_URL", "https://maliapi.215.im")
YYDS_API_KEY = _env("YYDS_API_KEY", "")  # AC-... 格式，profile 页获取

# GPTMail（mail.chatgpt.org.uk），支持公共测试 key "gpt-test"
GPTMAIL_BASE_URL = _env("GPTMAIL_BASE_URL", "https://mail.chatgpt.org.uk")
GPTMAIL_API_KEY = _env("GPTMAIL_API_KEY", "gpt-test")

# Remail 开放 API（Bearer rk-...；短效验证码订单）
REMAIL_BASE_URL = _env("REMAIL_BASE_URL", "https://remail.aishop6.com")
REMAIL_API_KEY = _env("REMAIL_API_KEY", "")
REMAIL_PROJECT_ID = _env_int("REMAIL_PROJECT_ID", 0)
REMAIL_EMAIL_SUFFIX = _env("REMAIL_EMAIL_SUFFIX", "outlook.com")
REMAIL_SUPPLY = _env("REMAIL_SUPPLY", "private_first")

# iCloud Mail API（API 主机，不是文档站 email.manageh.shop）
ICLOUD_MAIL_API_BASE = _env("ICLOUD_MAIL_API_BASE", "https://mail.no-replyca.xyz")
ICLOUD_MAIL_API_KEY = _env("ICLOUD_MAIL_API_KEY", "")
ICLOUD_MAIL_TYPE = _env("ICLOUD_MAIL_TYPE", "icloud-code").strip().lower() or "icloud-code"
ICLOUD_MAIL_SERVICE = _env("ICLOUD_MAIL_SERVICE", "openai").strip().lower() or "openai"

# Cloudflare Temp Email（dreamhunter2333/cloudflare_temp_email，建议自部署 Workers）
CFMAIL_BASE_URL = _env("CFMAIL_BASE_URL", "https://temp-email-api.awsl.uk")
CFMAIL_ADMIN_PASSWORD = _env("CFMAIL_ADMIN_PASSWORD", "")  # x-admin-auth header
CFMAIL_SITE_PASSWORD = _env("CFMAIL_SITE_PASSWORD", "")   # x-custom-auth header（可选）

# ---- 自定义临时邮箱（配置驱动，接任意 REST 风格 API，不写代码）----
# TEMP_EMAIL_PROVIDER=custom 时启用。JSON 路径支持点号+数组下标（如 data.address / data.items[0].id）。
# URL 与 body 模板可用占位符：{email} {id} {token} {name} {domain} {msg_id}
CUSTOM_MAIL_BASE_URL = _env("CUSTOM_MAIL_BASE_URL", "")
CUSTOM_MAIL_AUTH_HEADER = _env("CUSTOM_MAIL_AUTH_HEADER", "")   # 鉴权头名，空=不加鉴权头
CUSTOM_MAIL_API_KEY = _env("CUSTOM_MAIL_API_KEY", "")           # 鉴权头的值本体
CUSTOM_MAIL_AUTH_PREFIX = _env("CUSTOM_MAIL_AUTH_PREFIX", "")   # 值前缀（如 "Bearer "）
# 建号
CUSTOM_MAIL_CREATE_METHOD = _env("CUSTOM_MAIL_CREATE_METHOD", "POST")
CUSTOM_MAIL_CREATE_PATH = _env("CUSTOM_MAIL_CREATE_PATH", "")
CUSTOM_MAIL_CREATE_BODY = _env("CUSTOM_MAIL_CREATE_BODY", "")   # POST body 模板（JSON 串，占位符替换）
CUSTOM_MAIL_EMAIL_PATH = _env("CUSTOM_MAIL_EMAIL_PATH", "email")  # 响应里 email 的 JSON 路径
CUSTOM_MAIL_ID_PATH = _env("CUSTOM_MAIL_ID_PATH", "")           # 邮箱 id 路径（空=拿 email 当 id）
CUSTOM_MAIL_TOKEN_PATH = _env("CUSTOM_MAIL_TOKEN_PATH", "")     # 邮箱 token 路径（可选）
# 取信
CUSTOM_MAIL_FETCH_METHOD = _env("CUSTOM_MAIL_FETCH_METHOD", "GET")
CUSTOM_MAIL_FETCH_PATH = _env("CUSTOM_MAIL_FETCH_PATH", "")     # 占位符替换，如 /api/emails/{id}
CUSTOM_MAIL_FETCH_AUTH = _env("CUSTOM_MAIL_FETCH_AUTH", "key").strip().lower()  # key | token
CUSTOM_MAIL_LIST_PATH = _env("CUSTOM_MAIL_LIST_PATH", "")       # 消息数组的 JSON 路径（空=响应本身是数组）
CUSTOM_MAIL_DETAIL_PATH = _env("CUSTOM_MAIL_DETAIL_PATH", "")   # 单封详情路径（可选）
CUSTOM_MAIL_MSG_ID_PATH = _env("CUSTOM_MAIL_MSG_ID_PATH", "id")  # 列表项里 msgid 路径（配合 detail）
CUSTOM_MAIL_MSG_PATH = _env("CUSTOM_MAIL_MSG_PATH", "")         # detail 响应里单封 msg 的 JSON 路径

# ---------------------------------------------------------------- 短信接码平台 (firefox.fun)
SMS_API_BASE = _env("SMS_API_BASE", "http://www.firefox.fun/yhapi.ashx")
SMS_API_NAME = _env("SMS_API_NAME", "")  # firefox.fun APIName（仅标识账号）
SMS_TOKEN = _env("SMS_TOKEN", "")  # firefox.fun 持久 token；取号还需平台项目 iid
SMS_PROJECT_ID = _env("SMS_PROJECT_ID", "2313")  # claude 项目
# 优先国家列表，按顺序尝试，""=任意(排除黑名单)
SMS_COUNTRY_PREFER = ["60", "56", "57", "44", ""]  # 60=马来西亚 56=智利 57=哥伦比亚 44=英国 ""=任意
SMS_COUNTRY_BLACKLIST = ["63"]  # 菲律宾

# ---------------------------------------------------------------- 备用短信平台 (hero-sms.com)
HERO_SMS_API_BASE = _env("HERO_SMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php")
HERO_SMS_API_KEY = _env("HERO_SMS_API_KEY", "")  # 备用接码 api_key
HERO_SMS_SERVICE = _env("HERO_SMS_SERVICE", "acz")  # Claude 专用服务
# 优先国家: 7=马来西亚 52=泰国 16=英国 56=西班牙 39=阿根廷 86=意大利 34=爱沙尼亚 49=立陶宛 36=中国
HERO_SMS_COUNTRY_PREFER = [7, 52, 16, 56, 39, 86, 34, 49, 36]

# ---------------------------------------------------------------- 打码平台
# CapSolver 验证码打码平台
CAPSOLVER_API_KEY = _env("CAPSOLVER_API_KEY", "")

# EZ-Captcha 验证码打码平台
EZCAPTCHA_API_KEY = _env("EZCAPTCHA_API_KEY", "")
EZCAPTCHA_API_BASE = _env("EZCAPTCHA_API_BASE", "https://api.ez-captcha.com")

# YesCaptcha 打码平台（Grok Turnstile + GitHub Arkose）。API 与 CapSolver 兼容。
YESCAPTCHA_API_KEY = _env("YESCAPTCHA_API_KEY", "")
YESCAPTCHA_API_BASE = _env("YESCAPTCHA_API_BASE", "https://api.yescaptcha.com")

# ---------------------------------------------------------------- agent-captcha 视觉投票求解器
# GitHub Arkose 拼图用多模态大模型「投票」求解（common/agent_captcha.py）。
# 各家网关 OpenAI 兼容(/v1/chat/completions)；claude/opus 走 Anthropic 原生(/v1/messages)。
# 主视觉网关（gpt-5.x，图像增强 gpt-image-2 也在此）
VISION_API_BASE = _env("VISION_API_BASE", "")
VISION_API_KEY = _env("VISION_API_KEY", "")
# 图像增强兜底网关（gpt-image-2 images/edits）
IMAGE_EDIT_BASE2 = _env("IMAGE_EDIT_BASE2", "")
IMAGE_EDIT_KEY2 = _env("IMAGE_EDIT_KEY2", "")
# 投票池：中转网关(gemini/gpt) + claude 专用网关。逗号分隔的 key 留空则该模型不参与。
VOTE_ZZ_BASE = _env("VOTE_ZZ_BASE", "")          # 中转网关(gemini-3.5-flash / gemini-3.1-pro / gpt-5.5)
VOTE_ZZ_KEY = _env("VOTE_ZZ_KEY", "")            # 上面网关里 gemini 用的 key
VOTE_GPT_KEY = _env("VOTE_GPT_KEY", "")          # 同网关里 gpt-5.5 用的 key（可与 ZZ_KEY 不同）
VOTE_OPUS_BASE = _env("VOTE_OPUS_BASE", "")      # claude opus 专用网关（Anthropic /v1/messages）
VOTE_OPUS_KEY = _env("VOTE_OPUS_KEY", "")
# gemma 免费兜底文本网关（可选）
GEMMA_API_BASE = _env("GEMMA_API_BASE", "")
GEMMA_API_KEY = _env("GEMMA_API_KEY", "")

# ---------------------------------------------------------------- 标准 token 导出/上传
# 注册成功后落地的标准格式 token 目录（CPA codex / SUB2API content / grok sso）
TOKEN_OUTPUT_DIR = _env("TOKEN_OUTPUT_DIR", "tokens")

# CPA 管理接口（ChatGPT codex 授权文件导入）
CPA_URL = _env("CPA_URL", "")
CPA_MGMT_KEY = _env("CPA_MGMT_KEY", "")
# Codex 授权地址来源：sub2 保持旧流程；cpa 由 CPA 生成 PKCE 并接收 callback。
CODEX_AUTH_URL_SOURCE = _env("CODEX_AUTH_URL_SOURCE", "sub2").strip().lower() or "sub2"
CODEX_CPA_CALLBACK_RETRIES = int(_env("CODEX_CPA_CALLBACK_RETRIES", "5") or "5")
CODEX_CPA_CALLBACK_RETRY_DELAY = float(_env("CODEX_CPA_CALLBACK_RETRY_DELAY", "3") or "3")

# SUB2API 管理接口（ChatGPT codex-session / Grok SSO 转 OAuth 导入）
SUB2API_URL = _env("SUB2API_URL", "")
SUB2API_EMAIL = _env("SUB2API_EMAIL", "")
SUB2API_PASSWORD = _env("SUB2API_PASSWORD", "")
SUB2API_GROUP = _env("SUB2API_GROUP", "codex")  # 目标分组名，需先在 SUB2API 后台建好
SUB2API_GROK_GROUP = _env("SUB2API_GROK_GROUP", "grok")  # platform=grok 的目标分组
SUB2API_GROK_PROXY_ID = int(_env("SUB2API_GROK_PROXY_ID", "0") or "0")  # 0=不指定

# webchat2api（Grok sso 注入）
WEBCHAT2API_URL = _env("WEBCHAT2API_URL", "")
WEBCHAT2API_KEY = _env("WEBCHAT2API_KEY", "")

# chatgpt2api（basketikun/chatgpt2api 普通网页号导入，POST <url>/api/accounts）
# register_chatgpt.py --import-c2a 注册成功后逐个上传时用
CHATGPT2API_URL = _env("CHATGPT2API_URL", "")  # 对端 host（见 .env）
CHATGPT2API_KEY = _env("CHATGPT2API_KEY", "")  # 对端 admin key（Authorization: Bearer）

# ---------------------------------------------------------------- 订阅授权入口
# Claude / SuperGrok 订阅入口（激活码 CDK 流程「敬请期待」，后续支持授权到 SUB2API / CPA）
CLAUDE_SUB_URL = _env("CLAUDE_SUB_URL", "https://6661231.xyz/#/claude")
GROK_SUB_URL = _env("GROK_SUB_URL", "https://6661231.xyz/#/grok")
# 激活码 CDK 池（预留，逗号/换行/空格分隔）
CLAUDE_SUB_CDK = [c.strip() for c in _env("CLAUDE_SUB_CDK", "").replace("\n", ",").replace(" ", ",").split(",") if c.strip()]
GROK_SUB_CDK = [c.strip() for c in _env("GROK_SUB_CDK", "").replace("\n", ",").replace(" ", ",").split(",") if c.strip()]

# ---------------------------------------------------------------- ChatGPT OAuth add-phone 接码
# OpenAI/ChatGPT 在接码平台的服务号（按平台分，跟 Claude 的不同）
SMS_PROJECT_ID_OPENAI = _env("SMS_PROJECT_ID_OPENAI", "")  # firefox.fun 的 ChatGPT 项目 iid（待填）
HERO_SMS_SERVICE_OPENAI = _env("HERO_SMS_SERVICE_OPENAI", "dr")  # hero-sms/sms-activate OpenAI 服务码默认 dr
# hero-sms 价格上限(USD)，留空或0=不限(原硬编码1.0仍为兜底默认)
HERO_SMS_MAXPRICE_OPENAI = _env("HERO_SMS_MAXPRICE_OPENAI", "1.0")
# hero-sms 价格下限(USD)，留空或0=不限，过滤掉太便宜的垃圾号段
HERO_SMS_MINPRICE_OPENAI = _env("HERO_SMS_MINPRICE_OPENAI", "0")
# hero-sms 国家(OpenAI)：空=自动按价格优选全部国家；支持中文名(如 美国/印尼)、英文名/ISO码(如 US)、
# 国家ID，逗号分隔多国(如 美国,越南)。名称经 getCountries 动态解析+常用简体别名兜底。
# 指定后仅在这些国家取号(仍按价格升序)，无库存或超价时直接失败不回退。注意: 54=墨西哥 86=意大利 187=美国
HERO_SMS_COUNTRY_OPENAI = _env("HERO_SMS_COUNTRY_OPENAI", "")
# firefox.fun 价格上限：'0' 只取最便宜(垃圾号易被 OpenAI 拒)，给够才摸得到智利等好号
SMS_MAXPRICE_OPENAI = _env("SMS_MAXPRICE_OPENAI", "20")
# OpenAI add-phone 拉黑的号段(dialing code)：261 马达加斯加、63 菲律宾 等 OpenAI 常拒的
SMS_COUNTRY_BLACKLIST_OPENAI = [c.strip() for c in _env("SMS_COUNTRY_BLACKLIST_OPENAI", "261,63").split(",") if c.strip()]

# ---------------------------------------------------------------- 接码平台 (sms-man.com)
# sms-man.com API v2.0：base/control，token 鉴权，JSON 响应。过 Codex add-phone 主用。
# 返回的 number 已含国家码。app_id 支持数字 application_id 或 code/名(运行时查 /applications 解析)。
SMSMAN_API_BASE = _env("SMSMAN_API_BASE", "https://api.sms-man.com/control")
SMSMAN_TOKEN = _env("SMSMAN_TOKEN", "")  # sms-man.com API key（profile 页获取）
SMSMAN_APP_ID_OPENAI = _env("SMSMAN_APP_ID_OPENAI", "openai")  # 数字 application_id 或 code/名(自动解析)
SMSMAN_COUNTRY_ID_OPENAI = _env("SMSMAN_COUNTRY_ID_OPENAI", "0")  # 0=随机国家
SMSMAN_MAXPRICE_OPENAI = _env("SMSMAN_MAXPRICE_OPENAI", "")  # 价格上限（sms-man 币种），空=不限
