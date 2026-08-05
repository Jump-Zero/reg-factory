# Direct Bind Standalone / 直绑独立服务

一个仅监听本机回环地址的 Checkout 调试与状态验证工具。项目包含独立 HTTP 服务、静态控制台、代理角色隔离、任务状态记录和离线测试，不依赖外部项目数据库。

> 当前版本：`0.1.0`

## 特性

- 默认监听 `127.0.0.1:5601`，不对局域网或公网暴露端口。
- 最多 27 个账号同步批处理，支持粘贴/文件/注册队列批量导入 AT。
- 卡片一次录入后自动应用到当前账号批次；卡号和 CVC 只在当前页面与请求内存中使用。
- 网络出口由主 reg-factory 的 ChatGPT 配置注入，工作台不提供单独代理池设置。
- 区分请求成功、待确认、3DS/next action、订阅最终生效等状态。
- 完整离线契约测试，不需要真实账号、真实卡片或真实交易。

## 环境要求

- Windows 10/11
- Python 3.10+
- 网络访问继承主 reg-factory 的 ChatGPT 出口配置

## 快速开始

```bat
git clone <REPOSITORY_URL>
cd direct-bind-standalone
copy standalone_config.example.json standalone_config.json
start_standalone.cmd
```

首次启动会创建 `.venv` 并安装 `requirements.txt`。打开：

```text
http://127.0.0.1:5601/
```

后台启动和重启：

```bat
run_direct.vbs
restart_standalone.cmd
```

## 配置

复制 `standalone_config.example.json` 为 `standalone_config.json` 后修改。本地配置已被 `.gitignore` 排除。

| 字段 | 说明 |
| --- | --- |
| `country` / `currency` | Checkout 区域和币种 |
| `bind_country` / `bind_currency` | 支付方式区域和币种 |
| `promo_campaign` | 活动标识 |
| `timeout` | 请求超时秒数 |
| `preflight_cache_ttl` | 预检缓存秒数 |
| `billing` | 可选账单资料；公开仓库中保持为空 |

账号列表保存在浏览器 localStorage；卡片和账单字段不会写入 localStorage。发布、录屏或提交 issue 前，应使用页面清理功能并检查浏览器存储。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `GET` | `/api/standalone/info` | 服务能力与指纹信息 |
| `POST` | `/api/card-bind/session` | 创建浏览器侧安全卡片会话 |
| `POST` | `/api/standalone-flow/preflight` | Checkout 只读预检 |
| `POST` | `/api/standalone-flow/quick-checkout` | 创建任务 |
| `GET` | `/api/standalone-flow/task/{task_id}` | 查询任务状态 |
| `POST` | `/api/standalone-flow/tasks/clear` | 清理任务记录 |

## 测试与发布检查

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe scripts\release_check.py
```

测试全部使用本地 fake session。不要在测试、issue、截图或提交记录中放入真实令牌、Cookie、代理凭据、邮箱、账单信息或支付数据。

## 项目结构

```text
.
├── server.py                    # HTTP API 与任务状态
├── standalone_flow.py           # 编排、预检和代理池轮换
├── standalone_core/             # Checkout、支付与指纹核心
├── static/                      # 浏览器端界面
├── tests/                       # 离线契约测试
├── scripts/release_check.py     # 发布前敏感信息检查
└── standalone_config.example.json
```

架构与数据边界见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)，漏洞报告流程见 [`SECURITY.md`](SECURITY.md)。

## 贡献

提交补丁前请阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md)，运行完整测试与发布检查，并确保新网络请求具备超时、脱敏日志和不可重放保护。

## 许可证

代码采用 [MIT License](LICENSE)。第三方服务、接口、商标和内容仍受各自条款约束。

## 使用边界

项目用于本地接口调试、集成验证和个人开发。使用者负责确认账号、支付方式、代理、访问凭证和网络环境的使用范围，并遵守适用规则。不要用于未许可交易、盗刷、欺诈、批量滥用或规避平台控制。

软件按“现状”提供，不承诺功能完整性、稳定性或持续可用性。
