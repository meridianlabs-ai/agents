#!/usr/bin/env bash
# The provision-fallback recipe (rationale in action.yml's description).
# Run by the composite's one step, either directly as the runner or — for
# the codex jobs — via `sudo -u codex -H`, where sudo's env_reset has
# dropped every GITHUB_* variable: the venv is created in the current
# directory (the checkout) either way, and the two GITHUB_PATH appends
# below run only when the file is there to append to (the runner case,
# where the Claude engine's bare `pytest`/`ruff`/`mypy` need it; codex
# starts under its own reset PATH and is handed the venv's absolute paths
# by the compose steps instead). One optional argument: a caller's own
# recipe file (the reusable workflows' `codex_provision` input, written to
# $RUNNER_TEMP by the composite), run in place of the default venv +
# dev-install block after the uv bootstrap — the caller's trusted counterpart
# of its `claude-setup` action for codex runs, where a composite action (which
# can only run as the runner) is never run.
set -euo pipefail
recipe="${1:-}"
for attempt in 1 2 3; do
  curl -LsSf https://astral.sh/uv/0.9.4/install.sh | sh && break
  if [ "$attempt" = 3 ]; then
    echo "uv install failed after 3 attempts" >&2; exit 1
  fi
  echo "uv install attempt $attempt failed; retrying..." >&2
  sleep $((attempt * 10))
done
export PATH="$HOME/.local/bin:$PATH"
printf '%s\n' .venv/ '*.egg-info/' >>.git/info/exclude
if [ -n "$recipe" ]; then
  # The caller's recipe: uv is on PATH, the cwd is the checkout, and the
  # environment is whatever the composite's caller gave this script (sudo's
  # reset environment under `user`). Run with the options a `shell: bash`
  # workflow step gets (`bash --noprofile --norc -eo pipefail`): a failing
  # command or pipeline fails the recipe and so the provisioning step,
  # instead of a later successful command hiding it (review round 2 of the
  # fix: `false` then `printf` returned 0 through a plain `bash "$recipe"`).
  # A caller that expects a failure handles it explicitly (`cmd || true`).
  echo "running the caller's codex provisioning recipe"
  bash --noprofile --norc -eo pipefail "$recipe"
else
  uv venv
  if python3 -c 'import tomllib,sys; sys.exit(0 if "dependency-groups" in tomllib.load(open("pyproject.toml","rb")) else 1)'; then
    uv pip install -e ".[dev]" --group dev
  else
    uv pip install -e ".[dev]"
  fi
fi
if [ -n "${GITHUB_PATH:-}" ]; then
  echo "$HOME/.local/bin" >>"$GITHUB_PATH"
  echo "$PWD/.venv/bin" >>"$GITHUB_PATH"
fi
# Warm network-fetched caches the agents' tests need, from the runner
# (which has network) into a place the agent reads from. Evidence-
# driven list — extend it when a run reports another blocked
# download. tiktoken: inspect_ai's token-counting tests fetch BPE
# files on first use and the codex reviewer on inspect_ai#428
# (2026-09-09) stopped "on an unavailable tokenizer download"; the
# cache dir is tiktoken's default (TIKTOKEN_CACHE_DIR unset in the
# agents' env → tempfile.gettempdir()/data-gym-cache, i.e. /tmp on
# the runner and under codex, which codex-action launches via `sudo
# -u codex` — sudo's env_reset drops TMPDIR, which is not in the
# default env_keep — and whose bubblewrap sandbox binds the host
# /tmp). The Python resolves the dir exactly as tiktoken does —
# TIKTOKEN_CACHE_DIR, then DATA_GYM_CACHE_DIR, each honored when SET
# even if empty (an empty value means tiktoken caches nothing, so
# there is nothing to warm), then the tempdir default — and prints
# it, so a runner whose TMPDIR diverges from codex's /tmp is visible
# in the log rather than a silent no-op; it then makes the files
# world-readable (the codex user reads what the runner wrote) and the
# dir sticky-writable like /tmp itself, so codex can still cache an
# encoding outside this list once it has egress (tiktoken hash-checks
# known encodings on read, so a shared cache is not a poisoning
# vector). Best-effort and bounded: tiktoken downloads with no
# request timeout, so without the `timeout` a stalled blob host would
# hold the step to the caller's timeout-minutes and FAIL
# provisioning; with it, a stall (rc 124) lands in the warning branch
# like any other failure — a failed warm is a slower first test, never
# a failed provisioning.
if .venv/bin/python -c 'import tiktoken' 2>/dev/null; then
  if ! timeout 120 .venv/bin/python - <<'PY'
import os, tempfile
from pathlib import Path
import tiktoken
cache_dir = os.environ.get(
    "TIKTOKEN_CACHE_DIR",
    os.environ.get(
        "DATA_GYM_CACHE_DIR", os.path.join(tempfile.gettempdir(), "data-gym-cache")
    ),
)
if cache_dir == "":
    print("tiktoken caching disabled by env; nothing to warm")
    raise SystemExit(0)
names = ("cl100k_base", "o200k_base")
for name in names:
    tiktoken.get_encoding(name)
cache = Path(cache_dir)
for f in cache.iterdir():
    f.chmod(0o644)
cache.chmod(0o1777)
print(f"warmed tiktoken encodings into {cache}: {' '.join(names)}")
PY
  then echo "::warning::tiktoken cache warm failed or timed out (tests needing it will try to download at run time)"; fi
fi
