set -euo pipefail
v=$($AS_CODEX .venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])')
[ "$v" = "3.12" ] || { echo "::error::expected Python 3.12 in the venv, got $v"; exit 1; }
$AS_CODEX .venv/bin/python -c 'import pytest, markdown; print("dev and doc groups:", pytest.__version__, markdown.__version__)'
$AS_CODEX .venv/bin/pytest --version
[ "$(sha256sum <uv.lock)" = "$LOCK_BEFORE" ] || { echo "::error::uv sync changed uv.lock — the resolution was not the locked one"; exit 1; }
echo "inspect-harbor-like: Python $v, default groups dev+doc synced from the unchanged lockfile, tools run as codex"
