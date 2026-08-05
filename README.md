<div align="center">

# 🏭 reg-factory

### Outlook · Gmail · ChatGPT · Grok · Claude · Gemini · GitHub · Google One

**邮箱注册、平台授权、凭据导出与下游导入的一体化本地控制台**

<p>
  <img src="https://img.shields.io/badge/Outlook-0078D4?style=for-the-badge&logo=microsoftoutlook&logoColor=white" alt="Outlook" height="34" />
  &nbsp;
  <img src="https://img.shields.io/badge/Gmail-EA4335?style=for-the-badge&logo=gmail&logoColor=white" alt="Gmail" height="34" />
  &nbsp;
  <img src="https://img.shields.io/badge/ChatGPT-10A37F?style=for-the-badge&logo=openai&logoColor=white" alt="ChatGPT" height="34" />
  &nbsp;
  <img src="https://img.shields.io/badge/Grok-000000?style=for-the-badge&logo=x&logoColor=white" alt="Grok" height="34" />
  &nbsp;
  <img src="https://img.shields.io/badge/Claude-D97757?style=for-the-badge&logo=anthropic&logoColor=white" alt="Claude" height="34" />
  &nbsp;
  <img src="https://img.shields.io/badge/Gemini-886FBF?style=for-the-badge&logo=googlegemini&logoColor=white" alt="Gemini" height="34" />
</p>

<p>
  <img src="https://img.shields.io/badge/QQ%E7%BE%A4-1048143135-12B7F5?style=for-the-badge&logo=qq&logoColor=white" alt="QQ 交流群 1048143135" />
  &nbsp;
  <a href="https://t.me/TIANTIANAIPRO">
    <img src="https://img.shields.io/badge/Telegram-@TIANTIANAIPRO-26A5E4?style=for-the-badge&logo=telegram&logoColor=white" alt="Telegram @TIANTIANAIPRO" />
  </a>
  &nbsp;
  <a href="https://linux.do">
    <img src="https://img.shields.io/badge/Linux.do-Community-F5C400?style=for-the-badge&logo=linux&logoColor=black" alt="Linux.do Community" />
  </a>
</p>

<p>
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/Playwright-自动化-2EAD33?style=flat-square" alt="Playwright" />
  <img src="https://img.shields.io/badge/BitBrowser%20%2F%20AdsPower-指纹隔离-5A4FCF?style=flat-square" alt="Fingerprint Browser" />
  <img src="https://img.shields.io/badge/Clash%20Verge-节点切换-1F8FFF?style=flat-square" alt="Clash Verge" />
  <img src="https://img.shields.io/badge/license-educational-lightgrey?style=flat-square" alt="license" />
</p>

</div>

---

项目将 Outlook 邮箱、ChatGPT、Grok、Claude、Kiro 注册，Codex OAuth、账号导出和下游导入整合到同一个 Web 控制台，同时保留可组合的命令行入口。

> 仅用于学习、开发和经授权的测试。密钥、账号、Cookie、Token 和运行日志均应保留在本机，不要提交到仓库。

## 快速开始

### Windows 便携安装包

从 [Releases](https://github.com/tiantianGPU/reg-factory/releases/latest) 下载 `reg-factory-windows-x64-<版本>.zip`，完整解压后双击 `reg-factory.exe`，程序会自动打开控制台页面。便携包无需安装 Python；浏览器与网络出口按下面的前置条件选择配置。不要直接在 ZIP 压缩包预览窗口中运行 EXE。

默认端口是 `8799`。程序只复用相同版本的现有服务；旧版本占用端口时，新版会自动选择后续空闲端口并打开新版页面。再次双击同一新版会复用已经运行的新版服务。启动失败时错误窗口会保留，便于截图排查。

配置和运行数据默认保存在 `%LOCALAPPDATA%\RegFactory`，升级时直接替换程序目录即可。首次切换到便携包时，如果检测到仍在运行的源码版且其目录包含邮箱、Cookie 或 Token，新版会自动沿用该资产目录。

为控制体积，Windows 便携包不包含可选的 Codex K12 子项目；需要 K12 时请使用源码方式安装，并准备 Node.js 20+。

### 从源码运行

运行前需要：

- Python 3.10+
- 默认使用 [RuyiPage](https://github.com/LoseNine/ruyipage) Firefox WebDriver BiDi 指纹浏览器，也支持内置 Chromium、自定义 Chrome/Chromium、BitBrowser 和 AdsPower
- [Clash Verge 2.5.2 Windows x64](https://github.com/clash-verge-rev/clash-verge-rev/releases/download/v2.5.2/Clash.Verge_2.5.2_x64-setup.exe)（自动/固定节点模式），或一个住宅代理服务
- Node.js 20+（仅 Codex K12 控制台需要）

Windows：

```text
1. 双击 install.bat
2. 首次安装会下载 RuyiPage Firefox；启动 Clash Verge
3. 双击 start.bat
4. 打开 http://127.0.0.1:8799/
```

macOS / Linux：

```bash
./install.sh
./start.sh
```

安装脚本会创建 `.venv`、安装 Python 与 Playwright 依赖，并在缺少时从 `.env.example` 生成 `.env`。详细前置条件和环境变量见 [配置说明](docs/configuration.md)。

## Web 控制台

主控制台默认监听 `http://127.0.0.1:8799/`，提供以下入口：

- 新手指南：首次打开自动引导 Clash External Controller、控制密码、住宅代理、继承全局、浏览器、接码、资产 API 和任务勾选配置；支持按阶段跳过并从顶栏重新打开。
- 任务库：按流程分类选择任务，只展示常用参数，低频参数收进“更多设置”。
- 运行日志：实时查看输出和结果状态；可停止当前任务树，或一键清理新旧版本遗留的全部注册任务。
- 邮箱池：批量导入已有 Outlook 邮箱。
- 资产 API：每次输出前在线校验，只读取本次检测正常的邮箱或平台凭据，并提供顺序/index 取用和下游格式转换。
- 号池扫描：一键校验 Outlook、ChatGPT、Claude、Grok 和 Kiro，查看正常、待解锁、封禁、过期、受限及检测异常明细。
- 网络出口：切换 Clash 自动轮换、固定节点或动态住宅 IP，并测试公网出口。
- 环境配置：分组编辑 `.env` 并测试外部服务连通性。
- Codex K12：管理 K12 workspace、邮箱资产、任务与 Codex 凭据。
- Plus 订阅：ChatGPT 注册任务勾选订阅模式后，成功账号自动进入主 WebUI 的本地 `zkky` 工作台；支持一次导入最多 100 条 AT，按 1-27 的可调并发自动或手动轮换批处理，一次录入卡片后自动应用。提链和绑卡/支付可分别在网络页下拉选择住宅 IP、Clash 当前节点或具体 Clash 节点；缺少其中一种出口时自动回退。也可用 `REG_FACTORY_PLUS_LINK_PROXY_OVERRIDE` 和 `REG_FACTORY_PLUS_BIND_PROXY_OVERRIDE` 显式指定两个固定 Clash 入口。

控制台只监听本机。Codex K12 的独立说明见 [codex_k12/README.md](codex_k12/README.md)。

动态住宅 IP 会在创建新浏览器窗口时写入完整代理认证；轮换后的代理从下一个新窗口开始使用。网络页可分别设置 Outlook、Claude、ChatGPT、Grok 和 Kiro 的出口，例如 Outlook 使用 Clash、其他平台使用住宅代理，并可按平台测试真实公网 IP。

## 本地资产 API

本地接口支持按顺序或指定 `index` 读取邮箱、Claude/ChatGPT/Grok Cookie 和 Kiro Builder ID 账号；`format=cookies` 输出浏览器扩展可导入的标准 JSON，并可把 ChatGPT 会话转换为 SUB2API、CPA 或 chatgpt2api 格式。每个读取请求都会先在线校验对应平台，只从本次检测正常的健康资产池返回数据，并在响应中附带 `verification`。这只证明检测时刻可用，不代表账号之后不会被目标服务限制。控制台左侧打开“资产 API”即可配置访问密钥、生成调用命令和查看状态。默认仅允许本机访问，可配置 `REG_FACTORY_ASSET_API_KEY`。

```bash
# 按顺序取下一个邮箱
curl "http://127.0.0.1:8799/api/assets/emails?format=json"

# 指定第 3 个 ChatGPT 凭据，输出 SUB2API 格式，不推进顺序游标
curl "http://127.0.0.1:8799/api/assets/cookies/chatgpt?format=sub2api&index=2"

# 指定第 1 个 Claude 账号，输出标准浏览器 Cookie JSON
curl "http://127.0.0.1:8799/api/assets/cookies/claude?format=cookies&index=0"

# 配置 API Key 后增加鉴权请求头
curl "http://127.0.0.1:8799/api/assets/cookies/chatgpt?format=cpa" \
  -H "X-API-Key: your-key"
```

省略 `index` 时按独立游标顺序取用，指定 `index` 时只读取该条。完整平台格式、响应字段和游标重置方式见 [本地资产 API](docs/api.md)。

## 常用命令

```bash
# Outlook -> Claude / ChatGPT / Grok / Kiro
python run_full_flow.py --platforms claude chatgpt grok kiro

# 使用已有邮箱池注册三个平台
python register_three_platforms.py --from-pool

# 常驻注册 Outlook
python outlook_reg_loop.py

# Claude 使用最新 Outlook refresh token
python register.py --count 1 --node auto --latest-rt

# Claude 使用 YYDS 临时邮箱
python register.py --count 1 --node auto --provider yyds

# ChatGPT 使用 iCloud 接码邮箱（先在 .env 配置 ICLOUD_MAIL_API_KEY）
python register_chatgpt.py --count 1 --email-provider icloud

# ChatGPT 注册成功后进入主 WebUI 的本地 Plus 批处理工作台
python register_chatgpt.py --count 1 --plus-subscription

# Grok 浏览器注册并导入 SUB2API
python register_grok.py --count 1 --sub2api

# Kiro Builder ID 注册并导出长期凭据
python register_kiro.py --count 1

# Codex OAuth -> SUB2API / CPA
python oauth_codex.py --keep
```

完整参数、导出和补传命令见 [CLI 手册](docs/cli.md)。

## 项目结构

```text
reg-factory/
├─ common/                 # 浏览器、邮箱、代理、验证码、上传等共享能力
├─ tools/                  # 导出、校验、token 补传等维护工具
├─ runtime/                # 本机日志、状态与临时凭据（内容不提交）
├─ webui/                  # FastAPI 服务与原生前端
├─ codex_k12/              # Vue + Node 的 K12 控制台
├─ gmail_android/          # BlueStacks + Appium 的 Gmail 流程
├─ vision_solver/          # 通用视觉验证码求解库
├─ xconsole_client/        # XConsole 客户端
├─ tests/                  # Python 测试
├─ run_full_flow.py        # 端到端主入口
├─ register_*.py           # 各平台注册入口
├─ outlook_reg_loop.py     # Outlook 常驻注册入口
└─ config.py               # 统一配置读取
```

根目录只保留用户直接运行的入口和兼容模块；一次性维护命令放 `tools/`，可复用逻辑放 `common/`。完整依赖关系和新增文件约定见 [架构说明](docs/architecture.md)。

## 运行数据

下列内容由程序生成且默认忽略，不属于源码：

| 路径 | 内容 |
|---|---|
| `.env` | 本机密钥与服务地址 |
| `emails.txt` | 邮箱池 |
| `cookies/` | 平台 Cookie |
| `tokens/` | 标准 Token 与上传状态 |
| `_outlook_pool/` | Outlook 待用账号 |
| `outlook_accounts/` | Outlook 账号与 Graph Token |
| `runtime/logs/`、`tri_register_logs/` | 任务日志 |
| `runtime/state/` | Clash/住宅代理轮换状态 |
| `runtime/secrets/` | 本地临时凭据与测试密钥 |
| `screenshots*/` | 调试截图 |
| `unlock_results/` | Outlook 解锁结果 |

这些路径可能包含敏感信息。排查问题时也不要直接上传完整文件。

## 开发与验证

```bash
python -m unittest discover -s tests
node --check webui/static/app.js
```

更新已安装实例请使用 `update.bat` 或 `./update.sh`。更新脚本会先检查运行中的任务，再更新依赖、重启并验证 WebUI 版本。

## 文档

- [配置说明](docs/configuration.md)
- [本地资产 API](docs/api.md)
- [CLI 手册](docs/cli.md)
- [架构与目录约定](docs/architecture.md)
- [维护工具索引](tools/README.md)
- [Gmail Android 本地环境](docs/gmail-android.md)
- [常见问题](docs/troubleshooting.md)
- [版本记录](CHANGELOG.md)

## 支持

- QQ 群：`1048143135`
- Telegram：[@TIANTIANAIPRO](https://t.me/TIANTIANAIPRO)

## 🔗 Friend Links

- 🐧 [**LinuxDO**](https://linux.do) — A community for tech enthusiasts
