#!/usr/bin/env bash
# reg-factory 一键安装 (mac/linux)
set -e
cd "$(dirname "$0")"

echo "============================================================"
echo "  reg-factory 一键安装 (Python + Codex K12)"
echo "============================================================"

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
command -v "$PY" >/dev/null 2>&1 || { echo "[错误] 没找到 Python。请先安装 Python 3.10+"; exit 1; }
echo "[1/6] 使用 Python: $($PY --version)"

if [ -x ".venv/bin/python" ]; then
  echo "[2/6] 虚拟环境已存在,跳过创建。"
else
  echo "[2/6] 创建虚拟环境 .venv ..."
  "$PY" -m venv .venv
fi
VENV_PY=".venv/bin/python"

echo "[3/6] 安装依赖 ..."
"$VENV_PY" -m pip install --upgrade pip >/dev/null
"$VENV_PY" -m pip install -r requirements.txt

echo "[4/6] 安装 RuyiPage Firefox 与 Playwright Chromium 内核 ..."
"$VENV_PY" -m ruyipage install || echo "[警告] RuyiPage Firefox 安装失败，可稍后运行: .venv/bin/python -m ruyipage install"
"$VENV_PY" -m playwright install chromium || echo "[警告] 内核安装失败,可稍后手动跑: .venv/bin/playwright install chromium"

echo "[5/6] 准备 Codex K12 控制台 ..."
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 && [ -f "codex_k12/package.json" ]; then
  if (cd codex_k12 && npm install && npm run build); then
    echo "Codex K12 构建完成。"
  else
    echo "[警告] Codex K12 安装或构建失败，可稍后在 codex_k12 下重试 npm install && npm run build。"
  fi
else
  echo "[警告] 未找到 Node.js/npm。主面板仍可用；安装 Node.js 20+ 后可启用 Codex K12。"
fi

if [ -f ".env" ]; then
  echo "[6/6] .env 已存在,保留你的配置。"
elif [ -f ".env.example" ]; then
  cp .env.example .env
  echo "[6/6] 已从模板生成 .env,稍后在面板配置页填写密钥。"
fi

echo ""
echo "============================================================"
echo "  安装完成! RuyiPage Firefox 为默认浏览器，请确保 Clash Verge 已打开,"
echo "  然后运行: ./start.sh  打开控制面板"
echo "============================================================"
