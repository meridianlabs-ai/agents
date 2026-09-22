set -euo pipefail
v=$($AS_CODEX .venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])')
[ "$v" = "3.11" ] || { echo "::error::expected Python 3.11 in the venv, got $v"; exit 1; }
$AS_CODEX .venv/bin/pytest --version
$AS_CODEX .venv/bin/pyright --version
[ "$(sha256sum <uv.lock)" = "$LOCK_BEFORE" ] || { echo "::error::uv sync changed uv.lock — the resolution was not the locked one"; exit 1; }
echo "inspect-flow-like: Python $v, dev group synced from the unchanged lockfile, tools run as codex"
