# Standalone Codex K12

这是基于 reg-factory 公共能力重新开发的独立 K12 适配器，不复用旧 `codex_k12/` 应用代码。

## 启动

```powershell
python -m uvicorn k12.server:app --host 127.0.0.1 --port 8806
```

打开 <http://127.0.0.1:8806/>。运行数据写入 `runtime/k12/`，任务通过主项目的邮箱解析、浏览器、OAuth 和 Sub2API 能力执行。

控制台提供摘要、主邮箱池同步、邮箱删除/拆分、数据备份恢复、Workspace 操作、AT 测活、任务重试/取消/清理和日志查看。
