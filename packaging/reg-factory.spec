from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


ROOT = Path.cwd().resolve()

TASK_FILES = [
    "run_full_flow.py",
    "register_three_platforms.py",
    "oauth_codex.py",
    "register_chatgpt.py",
    "register_grok_http.py",
    "register_grok.py",
    "register_kiro.py",
    "register.py",
    "register_github.py",
    "outlook_reg_loop.py",
    "unlock_outlook.py",
    "mailbox_broker.py",
    "register_outlook_standalone.py",
    "tools/extract_graph_tokens.py",
    "tools/upload_tokens.py",
    "tools/export_chatgpt2api.py",
    "tools/export_accounts.py",
    "tools/install_ruyipage.py",
]

datas = [
    (str(ROOT / ".env.example"), "."),
    (str(ROOT / "VERSION"), "."),
    (str(ROOT / "update-portable.ps1"), "."),
    (str(ROOT / "webui" / "static"), "webui/static"),
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "common" / "bundled_browser_helper.py"), "common"),
    (str(ROOT / "vendor" / "chatgpt_plus"), "vendor/chatgpt_plus"),
]
datas.extend((str(ROOT / item), str(Path(item).parent)) for item in TASK_FILES)

playwright_datas, playwright_binaries, playwright_hidden = collect_all("playwright")
datas.extend(playwright_datas)
ruyipage_datas, ruyipage_binaries, ruyipage_hidden = collect_all("ruyipage")
datas.extend(ruyipage_datas)

hiddenimports = playwright_hidden + ruyipage_hidden + [
    "webui.server",
    "webui.scripts",
    "run_full_flow",
    "register_three_platforms",
    "oauth_codex",
    "register_chatgpt",
    "register_grok_http",
    "register_grok",
    "register_kiro",
    "register",
    "register_github",
    "outlook_reg_loop",
    "unlock_outlook",
    "mailbox_broker",
    "register_outlook_standalone",
]
for package in ("common", "vision_solver", "xconsole_client"):
    hiddenimports.extend(collect_submodules(package))

a = Analysis(
    [str(ROOT / "scripts" / "reg-factory-server.py")],
    pathex=[str(ROOT)],
    binaries=playwright_binaries + ruyipage_binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="reg-factory",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="reg-factory",
)
