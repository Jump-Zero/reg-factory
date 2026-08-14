# 新手教程

这篇教程面向第一次使用 reg-factory 的用户。建议按顺序完成，不要一开始就把并发设为 10。先用单并发确认网络、浏览器、邮箱和 Graph 授权全部正常，再逐步提高并发。

项目仅用于学习、开发和经授权的测试。请遵守目标服务规则。`.env`、账号、密码、Cookie、Token、代理凭据、日志和截图都应只保存在本机。

## 1. 下载并启动

### Windows 便携版

1. 打开项目的 [Releases](https://github.com/tiantianGPU/reg-factory/releases/latest)。
2. 下载 `reg-factory-windows-x64-<版本>.zip` 和对应的 `.sha256.txt`。
3. 把 ZIP 完整解压到固定目录，例如 `D:\RegFactory`。不要在压缩包预览窗口中直接双击 EXE。
4. 双击 `reg-factory.exe`。控制台窗口保持打开，浏览器会自动访问 `http://127.0.0.1:8799/`。
5. 如果页面没有自动打开，手动访问上面的地址。端口被旧版本占用时，新版本会自动选择后续空闲端口，实际地址以启动窗口为准。

首次运行的数据默认放在 `%LOCALAPPDATA%\RegFactory`。升级程序不会删除这里的 `.env`、邮箱池、Cookie、Token 或账号记录。

### macOS Apple Silicon

1. 下载 `reg-factory-macos-arm64-<版本>.tar.gz` 并完整解压。
2. 在终端进入解压目录，运行 `./reg-factory`。
3. 如果系统拦截未签名程序，在“系统设置 -> 隐私与安全性”中确认允许，再重新运行。

### 从源码运行

Windows：

```text
1. 双击 install.bat
2. 双击 start.bat
3. 打开 http://127.0.0.1:8799/
```

macOS / Linux：

```bash
./install.sh
./start.sh
```

源码方式需要 Python 3.10+。只有 Codex K12 额外需要 Node.js 20+。

## 2. 认识主界面

首次打开会显示交互式新手指南。可以暂时跳过某一步，但建议至少依次看完：

1. 网络出口
2. 指纹浏览器
3. Outlook Graph 辅助邮箱
4. 邮箱与短信接码
5. 任务库与运行日志
6. 资产 API

顶栏可以重新打开指南。WebUI 保存的配置写入本机 `.env`，新启动的任务会立即读取，一般不需要重启服务。

## 3. 先配置网络出口

打开左侧“网络出口”。三种模式只能选一种。

| 模式 | 适用情况 | 新手建议 |
|---|---|---|
| Clash 自动轮换 | 有多个 Clash 节点，希望自动探测 | 单并发开始；节点选择是全局状态 |
| Clash 固定节点 | 只有一个稳定节点，或明确要共享同一出口 | 可以并发，但共享 IP 更容易关联或限流 |
| 动态住宅 IP | 有单住宅代理或 Sticky 代理池 | 并发时优先使用，每槽固定一个端点 |

### 配置 Clash Verge

1. 启动 Clash Verge。
2. 在 Clash 设置或内核设置中启用 External Controller。
3. 控制器只监听本机，例如 `127.0.0.1:9097`。
4. WebUI 的控制器地址填写 `http://127.0.0.1:9097`。
5. Clash 设置了 Secret 时，在 WebUI 填完全相同的控制密码；没有设置就都留空。
6. 找到 mixed-port，例如 `7897`，代理地址填写 `http://127.0.0.1:7897`。
7. 保存并点击“应用并测试 IP”。成功时会显示实际公网 IP 和当前节点。

如果控制器连接失败，先确认地址、端口和 Secret，不要反复启动注册任务测试。

### 配置住宅代理

单代理填写完整 URL：

```text
http://username:password@host:port
```

代理池在 WebUI 中每行一个。并发槽按顺序领取端点：槽 1 使用第 1 条，槽 2 使用第 2 条。超过池大小时，程序默认降低有效并发，不让多个账号静默共享同一出口。

带 `sid` 的住宅用户名通常表示 Sticky 会话。相同 `sid` 在供应商规定的有效期内应保持同一 IP；十条不同 `sid` 可作为十个独立端点。具体粘性时间和计费规则以代理供应商说明为准。

换 IP API 是可选项。轮换只影响之后创建的新浏览器窗口，不会强制修改正在注册的窗口，以免登录中途换 IP。

### 平台出口覆盖

网络页可以让 Outlook、Claude、ChatGPT、Grok 和 Kiro 继承全局出口，也可以单独覆盖。例如 Outlook 使用住宅代理，ChatGPT 使用固定 Clash。保存后逐个平台执行 IP 测试，确认显示的出口符合预期。

## 4. 配置指纹浏览器

打开“配置 (.env) -> 指纹浏览器”。

- `bundled`：使用程序内置 Chromium，旧的 Chromium/CDP 流程会自动选择它。
- `custom`：使用本机 Chrome/Chromium，可填写可执行文件路径。
- `bitbrowser`：需要先启动 BitBrowser，并保持本地 API `http://127.0.0.1:54345` 可访问。
- `adspower`：需要启动 AdsPower，并按实际情况配置 API 地址和 Key。

保存后点击“测试指纹浏览器连通”。通过标准是 WebUI 显示 API 或 runtime 已就绪。

浏览器选择决定网页任务使用的 Chromium provider。默认 BitBrowser 为每个并发槽创建独立 Profile、Cookie 和指纹；内置或自定义 Chromium 适合本地调试。

Outlook 使用 BitBrowser 时，任务会新建干净的注册标签页并关闭启动时的 IP 环境页或导航页；每个并发槽仍有独立 Profile、Cookie 和指纹种子。

## 5. 配置 Outlook Graph 辅助邮箱

Outlook 注册成功后需要 Graph refresh token 才能稳定给其他平台注册流程取邮件验证码。Microsoft 可能要求在 `proofs/Add` 页面绑定辅助邮箱，因此这一项不能忽略。

打开“配置 (.env) -> Outlook 自注册”：

1. 保持“Graph 辅助邮箱”开启。
2. 新手可先选择 `yyds`，并填写对应 API Key。
3. 使用自定义临时邮箱时选择 `custom`，再配置 `CUSTOM_MAIL_*`。
4. 使用自有 Outlook 辅助邮箱时选择 `outlook`，格式为：

```text
email@outlook.com----password----refresh_token----client_id
```

5. 也可填写 `yyds,outlook`，让第一个来源失败时自动使用第二个。
6. 点击 Outlook 辅助邮箱测试。通过标准是 API 可创建/读取邮箱，或自有 Outlook refresh token 验证成功。

`refresh_token` 必须与签发它的 `client_id` 成对。顺序写反时导入器会尽力识别，但建议始终使用上面的标准格式。

## 6. 第一次运行 Outlook

打开“任务库 -> Outlook 邮箱注册”，第一次使用以下参数：

| 参数 | 建议值 | 说明 |
|---|---:|---|
| 总尝试上限 | `1` | 先只验证一条完整流程 |
| 并发数 | `1` | 排除并发和代理池干扰 |
| 最近窗口最低成功率 | `10` | 第一次只有一条，不会立即触发 |
| 成功率窗口 | `20` | 收集满 20 条结果后才判断 |
| 验证按住次数上限 | `5` | 达到上限仍未完成就放弃本次 |
| 注册超时 | `180` | 只限制注册阶段 |

Graph 授权使用独立的 `OUTLOOK_GRAPH_AUTH_TIMEOUT`，默认 240 秒，不占用注册超时。

点击运行后重点看日志：

- `worker=... slot=... proxy=...`：显示并发槽和脱敏后的出口。
- `registration complete`：注册页已经进入明确的 Microsoft 后续页面。
- `graph token extracted`：Graph refresh token 获取成功。
- `write_record OK`：账号已经写入 `_outlook_pool`。
- `[traffic] ... blocked=...`：本次住宅流量节流统计。

只有出现 Graph token 和 `write_record OK` 才算可供下游使用的完整成功。验证码控件消失但仍停在注册页不会被算作成功。

## 7. 调整住宅流量模式

网络页的“住宅流量模式”有四档。

### 平衡节流 `balanced`

默认模式。拦截普通图片、字体和音视频，保留脚本、样式表、Microsoft、Claude、ChatGPT、Grok、GitHub 登录资源和常见验证资源。第一次运行建议使用此模式。

### 激进节流 `aggressive`

继续拦截非认证页的样式表和常见统计请求；Microsoft、Claude、ChatGPT、Grok、GitHub 的认证页样式仍会保留。适合住宅流量价格较高且平衡模式已经跑通的情况。如果页面结构明显异常、按钮不可见或状态识别异常，先切回平衡模式复测。

### 极限节流 `extreme`

在激进模式基础上，为 BitBrowser 加入后台联网抑制启动参数，并拦截预取、预渲染、清单、源码映射和更多遥测域名。不会强制把窗口启动页设为 `about:blank`；连接后会先安装请求过滤，再创建注册标签页。验证码资源与五家认证页的必要样式仍会放行，图片、字体、媒体和可选遥测继续拦截；页面行为异常时依次回退到 `aggressive` 或 `balanced`。

### 关闭节流 `off`

加载全部资源。主要用于排查兼容问题，不适合长期使用住宅流量。

节流不会减少 HTML、JavaScript、接口请求或验证资源本身的流量，也不会让一个代理端点自动变成多个出口。并发越高、失败重试越多，总流量仍会增加。

## 8. 从 1 并发逐步提高

单并发完整成功后，再按 `2 -> 5 -> 10` 增加。每一级至少观察一个成功率窗口。

有效并发由三个条件共同决定：

```text
有效并发 = 不超过任务输入、不超过 REG_FACTORY_MAX_CONCURRENCY、不超过可隔离出口数量
```

`REG_FACTORY_MAX_CONCURRENCY` 默认是 10，可以在网络页自行修改。它是安全上限，不是强制并发数。输入 10 但只有 5 个住宅端点时，默认只使用 5 个有效并发。

没有住宅代理时仍可并发：

- `clash_fixed` 或任务里指定固定节点可以共享同一公网 IP 并发。
- `REG_FACTORY_ALLOW_SHARED_EGRESS=true` 表示你明确接受共享出口。
- `clash_auto` 需要修改全局节点，为防止并发任务运行中换 IP，会自动降为单并发。

共享公网 IP 可能增加账号关联、验证和限流风险。并发框架只能隔离 Profile、Cookie、Session 和指纹，不能把同一个公网 IP 变成多个 IP。

## 9. 配置自动停止条件

Outlook 常驻任务的“总尝试上限”默认是 `0`，表示不按累计次数停止。推荐使用成功率熔断：

- 最低成功率：默认 `10%`。
- 统计窗口：默认最近 `20` 次已完成尝试。
- 只有收集满窗口后才判断。
- 成功率严格低于阈值才停止，等于阈值不会停止。
- 阈值设为 `0` 可关闭熔断。

例如最近 20 次只有 1 次成功，成功率是 5%，低于 10%，任务会停止派发新尝试；已经运行中的窗口会自然收尾。这样比固定跑满 20 次更适合无人值守，也能减少低通过率阶段的住宅浪费。

## 10. 理解常见 Microsoft 登录提示

以下提示通常出现在注册没有真正落库、账号尚未就绪或短时间重复登录后：

- `We couldn't find a Microsoft account.`
- `Password sign-in isn't available. Try another method.`
- `You've tried to sign in too many times with an incorrect account or password.`

新版会把它们识别为终止状态，邮箱和密码各只提交一次，不再继续浏览器或 HTTP 重试。看到这些日志时不要立即人工反复登录；先停止任务，等待 Microsoft 的临时限流恢复，再检查该条记录是否真的出现过 `registration complete` 和 `write_record OK`。

## 11. 运行其他平台注册任务

Outlook 池中有带 Graph token 的记录后，再运行 ChatGPT、Claude、Grok 或 Kiro。仍然从并发 1 开始，并先完成对应配置测试：

- ChatGPT：选择 Outlook 池或 iCloud 邮箱；需要 Codex 导入时再配置短信和 SUB2API。
- Claude：配置视觉 API；登录、邮件验证和 magic link 必须保持同一出口。
- Grok：先测试网络出口；需要时勾选 SUB2API 导入。
- Kiro：确保 Outlook Graph 能正常读验证码。

每个平台可以使用独立出口。不要因为 Outlook 使用住宅代理，就默认其他平台也已经继承同一配置；以网络页的平台 IP 测试结果为准。

## 12. 查看和领取资产

### 邮箱池

标准格式是：

```text
email----password----refresh_token----client_id
```

WebUI 可以导入 Outlook 各区域域名、Hotmail、Live、MSN、iCloud 和自定义邮箱，也兼容 JSON 与多种分隔符。

### 号池扫描

扫描是按需人工复核。它会同平台低频串行，复用近期结果，并在限流或连续风控响应时暂停。扫描结论只代表检测时刻，不是账号永久状态。

### 资产 API

资产 API 默认直接领取本地尚未领取的记录，不会在每次领取前联网扫描。同一账号领取后，切换输出格式也不会再次返回。

只希望领取最近扫描为正常的邮箱时，使用：

```bash
curl "http://127.0.0.1:8799/api/assets/emails?normal_only=true"
```

这只读取扫描缓存，领取时仍不联网。完整接口见 [本地资产 API](api.md)。

## 13. 停止任务和清理窗口

单个任务可在日志面板点击停止。需要结束全部注册任务时点击“停止全部”，它会结束当前任务树并清理旧 WebUI 遗留的注册子进程，但不会关闭 WebUI、Clash 或 BitBrowser 主程序。

不要直接结束整个 Python 进程树作为日常停止方式，否则可能留下 BitBrowser 临时 Profile。异常结束后先使用“停止全部”，再重启 WebUI。

## 14. 更新版本

便携版使用 WebUI 的“一键更新”，或从 Releases 下载新包完整覆盖程序目录。运行数据在独立数据目录，不会随程序包替换。

源码版：

```text
Windows: 双击 update.bat
macOS/Linux: ./update.sh
```

不要在任务运行时直接 `git pull`。更新脚本会检查任务、更新依赖、重启服务并验证实际版本。

## 15. 排查顺序

任务失败时按这个顺序检查，比直接提高并发或重复运行更有效：

1. 网络页对应平台的公网 IP 测试是否通过。
2. 指纹浏览器测试是否通过。
3. Outlook Graph 辅助邮箱测试是否通过。
4. 单并发、平衡节流能否跑通。
5. 日志是否显示注册完成、Graph token 和写入记录。
6. 住宅池端点数量是否少于输入并发。
7. 是否出现 Microsoft 临时登录限流。
8. 页面异常时切换 `extreme -> aggressive -> balanced -> off` 对比。

仍无法定位时，只提供脱敏后的错误文本和相关日志片段。不要上传 `.env`、完整代理 URL、账号密码、Cookie、Token、邮箱池或原始截图。

更多配置项见 [配置说明](configuration.md)，命令行参数见 [CLI 手册](cli.md)，常见错误见 [常见问题](troubleshooting.md)。
