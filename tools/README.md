# Tools

人工触发的维护、导出和调试工具统一放在这里。

## 维护工具

- `export_accounts.py`：导出浏览器扩展可用的账号 Cookie。
- `export_chatgpt2api.py`：导出或上传普通 ChatGPT 网页号。
- `export_kiro_credentials.py`：聚合导出 kiro.rs 兼容的 `credentials.json`。
- `extract_graph_tokens.py`：提取 Outlook Graph refresh token。
- `upload_tokens.py`：补传本地标准 token。
- `validate_keys.py`：校验 Claude sessionKey。
- `upgrade_claude_max.py`：Claude Max 升级工具。

## 本地辅助工具

- `recording/`：录屏运行与停止脚本。
- `debug/`：本地实验脚本，默认不提交。

所有命令均从仓库根目录运行，例如：

```bash
python tools/extract_graph_tokens.py --help
python tools/recording/record_and_run.py --help
```
