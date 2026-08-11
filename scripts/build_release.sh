#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-}"
SKIP_TESTS=0
SKIP_INSTALL=0
for arg in "${@:2}"; do
  case "$arg" in
    --skip-tests) SKIP_TESTS=1 ;;
    --skip-install) SKIP_INSTALL=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Usage: scripts/build_release.sh VERSION [--skip-tests] [--skip-install]" >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
PYTHON="$REPO_ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || { echo "Python virtual environment not found. Run ./install.sh first." >&2; exit 1; }
[[ "$(tr -d '[:space:]' < VERSION)" == "$VERSION" ]] || { echo "VERSION does not match $VERSION" >&2; exit 1; }

ARCH="$(uname -m)"
[[ "$ARCH" == "arm64" ]] || { echo "Apple Silicon build requires arm64, got $ARCH" >&2; exit 1; }
PACKAGE_NAME="reg-factory-macos-arm64-$VERSION"
DIST_ROOT="$REPO_ROOT/dist"
BUILD_ROOT="$REPO_ROOT/build"
PYINSTALLER_OUTPUT="$DIST_ROOT/reg-factory"
PACKAGE_ROOT="$DIST_ROOT/$PACKAGE_NAME"
ARCHIVE="$DIST_ROOT/$PACKAGE_NAME.tar.gz"
CHECKSUM="$ARCHIVE.sha256.txt"

if [[ "$SKIP_INSTALL" -eq 0 ]]; then
  "$PYTHON" -m pip install -r requirements-build.txt
fi
if [[ "$SKIP_TESTS" -eq 0 ]]; then
  "$PYTHON" -m unittest discover -s tests
  node --check webui/static/app.js
fi

rm -rf "$BUILD_ROOT" "$PYINSTALLER_OUTPUT" "$PACKAGE_ROOT"
rm -f "$ARCHIVE" "$CHECKSUM"
"$PYTHON" -m PyInstaller --noconfirm --clean --distpath "$DIST_ROOT" --workpath "$BUILD_ROOT" packaging/reg-factory.spec
mv "$PYINSTALLER_OUTPUT" "$PACKAGE_ROOT"
cp README.md CHANGELOG.md .env.example VERSION "$PACKAGE_ROOT/"
cp -R docs "$PACKAGE_ROOT/"

find "$PACKAGE_ROOT" -type d -name __pycache__ -prune -exec rm -rf {} +
if find "$PACKAGE_ROOT" -type f \( -name .env -o -name emails.txt -o -name '*.log' \) -print -quit | grep -q .; then
  echo "Sensitive or runtime files entered the package" >&2
  exit 1
fi
if find "$PACKAGE_ROOT" -type f | sed "s#^$PACKAGE_ROOT/##" | grep -Eq '(^|_internal/)(cookies|tokens|runtime|outlook_accounts|unlock_results|codex_k12)/|^_internal/vendor/chatgpt_plus/'; then
  echo "Sensitive or runtime directories entered the package" >&2
  exit 1
fi

chmod +x "$PACKAGE_ROOT/reg-factory"
COPYFILE_DISABLE=1 tar -C "$DIST_ROOT" -czf "$ARCHIVE" "$PACKAGE_NAME"
HASH="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
printf '%s  %s\n' "$HASH" "$PACKAGE_NAME.tar.gz" > "$CHECKSUM"
ls -lh "$ARCHIVE" "$CHECKSUM"
