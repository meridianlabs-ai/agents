# Caller fixtures for the codex provisioning recipe

Representative projects for the four callers whose `claude-setup` action
does more than the generic codex recipe (`uv venv && uv pip install -e
.[dev]`), each with the `codex_provision` recipe its stub would set and the
checks the hosted canary (`.github/workflows/engine-isolation-canary.yml`,
job `caller-recipes`) runs after provisioning as the codex user on the
stock `ubuntu-latest` image. Agents-owned stand-ins, not the callers' trees:
the canary copies `project/` into the workspace root, runs this revision's
`create-codex-user`, `provision-fallback` with `user: codex` and the recipe,
then `create-codex-user` in `reset-home` mode with the composite's `bin`
output (as the reusable workflows do: it becomes the PATH of codex's
commands in `~codex/.codex/config.toml`), discovers the tools the way the
compose steps do (the same `bin` directories) and runs each as the codex
user under codex-action's launch shape (`sudo -u codex` with a reset PATH)
by absolute path, then by name under `codex sandbox` (the codex CLI's own
command environment and the workflows' `workspace_net` profile) with a
`bwrap` planted in each bin directory that codex must not run from any of
eight working directories, the pins' own trees included (the pinned system
bubblewrap comes first; with only the link pin from `/usr`, or without the
pins, it does run one), then the
fixture's `gate` there if it has one (a project script whose tools find one
another on PATH), then `check.sh`.

| Fixture | Stands in for | Recipe | Checks |
| --- | --- | --- | --- |
| `inspect-ai-like` | the inspect_ai fork's `meridian` `claude-setup` (`uv venv --python 3.11 && uv pip install -e ".[dev]"`) | the same | Python 3.11 interpreter; `pytest`/`ruff` from the `dev` extra runnable as codex |
| `inspect-flow-like` | inspect_flow's `setup` action (Python 3.11, `uv sync --dev`, lockfile) | `uv venv --python 3.11 && uv sync --dev` | Python 3.11; `pytest`/`pyright` installed; `uv.lock` unchanged by the sync (locked resolution) |
| `inspect-harbor-like` | inspect_harbor's `claude-setup` (Python 3.12, `uv sync`, `default-groups = ["dev", "doc"]`, relative `exclude-newer = "7 days"` with an `[tool.uv.exclude-newer-package]` override, whose lock a uv before 0.9.25 cannot parse) | `uv venv --python 3.12 && uv sync` | Python 3.12; `pytest` (dev) and `markdown` (doc) importable; lock unchanged |
| `ts-mono-like` | ts-mono's `claude-setup` (Node 22, `pnpm install --frozen-lockfile`, `packageManager: pnpm@11.22.0`, a pnpm workspace whose `check` is `turbo run check`) | `corepack enable --install-directory ~/.local/bin && pnpm install --frozen-lockfile` | `node` ≥ 22.13 (the image's); `pnpm` in `~codex/.local/bin`; `prettier` in `node_modules/.bin`, runnable as codex; the `gate`, `pnpm check`, passes under `codex sandbox` and fails under sudo's reset PATH alone ("Unable to find package manager binary", ts-mono's failure before option B); no `pyproject.toml`, so this case is the recipe-without-Python route |

Regenerate a lockfile after editing a fixture's manifest: `uv lock` in the
Python projects (with the uv version `provision-fallback` bootstraps),
`COREPACK_ENABLE_DOWNLOAD_PROMPT=0 corepack pnpm install --lockfile-only` in
the Node one.
