# Run by the canary as the runner after provisioning + reset; $AS_CODEX runs a
# command as codex under codex-action's launch shape.
set -euo pipefail
v=$($AS_CODEX .venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])')
[ "$v" = "3.11" ] || { echo "::error::expected Python 3.11 in the venv, got $v"; exit 1; }
$AS_CODEX .venv/bin/python -c 'import inspect_ai_like, pytest'
$AS_CODEX .venv/bin/pytest --version
$AS_CODEX .venv/bin/ruff --version
echo "inspect-ai-like: Python $v, editable install with the dev extra, tools run as codex"
