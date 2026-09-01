# 架构与目录约定

## 分层

```text
WebUI / CLI 入口
        |
        v
平台流程与编排器
        |
        v
common/ 共享能力
        |
        v
浏览器、邮箱、代理、验证码与下游服务
```

- 根目录入口负责解析参数和编排，不新增通用实现。
- `common/` 放可被两个及以上流程复用的 Python 模块。
- `tools/` 放人工触发的导出、校验、迁移和补传命令。
- `runtime/` 统一收纳根目录外的日志、状态和临时凭据。
- `webui/` 只负责 schema、任务进程和本地界面，不复制业务逻辑。
- 独立 K12 适配器位于 `k12/`，拥有自己的服务、前端、任务和数据目录；`gmail_android/`、`vision_solver/` 等仍属于按需独立模块。

## 主要入口

| 文件 | 职责 |
|---|---|
| `run_full_flow.py` | Outlook 到多平台注册的端到端编排 |
| `register_three_platforms.py` | 使用已有邮箱注册多个平台 |
| `register.py` | Claude 注册 |
| `register_chatgpt.py` | ChatGPT 注册 |
| `register_grok.py` | Grok 指纹浏览器注册主流程 |
| `register_kiro.py` | AWS Builder ID / Kiro 注册与长期凭据导出 |
| `register_grok_http.py` | Grok 浏览器流程复用的内部协议辅助模块，不作为 UI 入口 |
| `outlook_reg_loop.py` | Outlook 常驻注册与入池 |
| `oauth_codex.py` | Codex OAuth 与下游导入 |
| `mailbox_broker.py` | 并发流程共享邮箱取码 |

## 共享模块

| 路径 | 职责 |
|---|---|
| `common/browser.py` | 指纹浏览器连接和页面操作 |
| `common/mailbox.py`、`common/emails.py` | 邮箱取码和邮箱池 |
| `common/proxy_switch.py` | 统一出口模式、Clash 节点约束与轮换 |
| `common/direct_proxy.py` | 住宅代理解析、代理池与轮换状态 |
| `common/session_export.py` | 登录态转标准下游格式 |
| `common/uploaders.py` | CPA、SUB2API 等上传器 |
| `common/agent_captcha.py` | Arkose 视觉投票求解 |
| `vision_solver/` | 通用验证码 schema、视觉投票和 driver |

## 文件放置规则

新增代码时按以下顺序判断：

1. 用户直接运行的核心流程才放根目录。
2. 可复用业务能力放 `common/`。
3. 一次性维护命令放 `tools/`。
4. 测试放 `tests/`，不要在根目录创建 `test_*.py`。
5. 运行输出只能写入已登记的数据目录，不要生成新的根目录文件类型。

## 运行数据边界

运行数据可能包含个人信息或凭据，必须与源码分开：

| 类型 | 路径 |
|---|---|
| 邮箱池 | `emails.txt`、`outlook_accounts/`、`_outlook_pool/` |
| 会话与凭据 | `cookies/`、`tokens/`、`sso_output/` |
| 日志 | `runtime/logs/`、`tri_register_logs/` |
| 本地状态 | `runtime/state/` |
| 临时凭据 | `runtime/secrets/` |
| 截图与录屏 | `screenshots*/`、`recordings/` |
| 解锁结果 | `unlock_results/` |

这些路径必须保留在 `.gitignore`。目录需要进入仓库时只提交 `.gitkeep`，不能提交样本凭据。

## WebUI 任务约定

`webui/scripts.py` 是 CLI 到界面的 schema。新增任务时：

- `file` 指向仓库内真实入口。
- 参数 flag、类型和默认值必须与 CLI parser 一致。
- 低频参数仍应进入 schema，前端会自动收进“更多设置”。
- 为 schema 和命令构建补充测试。
