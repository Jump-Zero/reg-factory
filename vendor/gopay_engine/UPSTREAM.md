# GoPay Engine

This directory contains the GoPay-only protocol sources imported from the
local `GoPay主程序精简分享版-123修复版` package on 2026-08-25.

reg-factory loads these files through `common/gopay_service.py`. Runtime
credentials and task state must be stored below
`REG_FACTORY_DATA_DIR/runtime/gopay/`; no live account data belongs here.

The compatibility `gpt_register_bridge.py` is intentionally a no-op. The main
reg-factory ChatGPT, mailbox, OAuth, and Plus implementations remain separate.
