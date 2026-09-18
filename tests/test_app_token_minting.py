"""Phase 2 of the credential separation: the trusted jobs mint the machine
account's token (design/architecture.md → Landing job), and the PAT is retired
(2026-09-18).

The reusable workflows accept the GitHub App's two secrets and nothing else
for the machine account; each `gate` and `land` job mints a one-hour
installation token scoped to the caller repo and to that job's writes, as its
first step. These are structural checks on the workflow text (PyYAML is not a
test dependency), one per rule the contract relies on:

- both app secrets are declared `workflow_call` secrets, and the retired PAT
  is declared by none;
- every trusted job mints first, for `meridianlabs-ai` and exactly the caller
  repo, with exactly the permissions the design table lists for that job —
  unconditionally where the job cannot work without the machine account, and
  gated on the job-level `HAS_APP_SECRETS` boolean (a step `if:` cannot read
  `secrets`) only in `claude.yml` (the marvin-less degradation); the loops'
  gates carry no presence check any more (decision: Ransom, 2026-09-18);
- every token read in a trusted job is `steps.mint.outputs.token`, with the
  job-token fallback (`|| github.token`) in `claude.yml` alone;
- the job that runs the agent names neither the app secrets, nor the mint
  step's token, nor the PAT;
- the commit identity follows the token: the gate publishes it, the agent
  job passes it to `sync-branch` and its codex prep, and no workflow hardcodes
  the User's identity in a `git config` any more;
- the hourly Atlas sync mints for the inspect_ai fork, plus a second,
  read-only token for ts-mono that the script's ts-mono calls run under, and
  its preflight is real reads only (an installation token reports no OAuth
  scopes);
- this repo's own stubs and the examples pass the two app secrets explicitly,
  never `secrets: inherit`, and no tracked file outside `design/` (history)
  names the retired PAT.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
EXAMPLES = ROOT / "examples"

MINT_ACTION = "actions/create-github-app-token@v2"
TOKEN_EXPR = "steps.mint.outputs.token"
# The Atlas sync's second token (ts-mono, read-only), the same shape.
TS_MONO_TOKEN_EXPR = "steps.mint_ts_mono.outputs.token"
HAS_APP_SECRETS = "      HAS_APP_SECRETS: ${{ secrets.MARVIN_APP_CLIENT_ID != '' }}"
MINT_IF = "        if: env.HAS_APP_SECRETS == 'true'\n"
CALLER_REPO = "${{ github.event.repository.name }}"
# Split so this file is not itself a hit for the retired secret's name.
PAT = "MARVIN_" + "TOKEN"

# The design table (design/architecture.md → Landing job → "What each job
# mints"): trusted job → (repositories, {permission: level}). A change here
# is a change to what a confused-deputy write in that job could reach, and
# must be made in both places.
WRITE_SETS = {
    ("claude.yml", "gate"): (CALLER_REPO, {"issues": "write", "pull-requests": "write",
                                           "organization-projects": "write"}),
    ("claude.yml", "land"): (CALLER_REPO, {"contents": "write", "issues": "write",
                                           "pull-requests": "write", "organization-projects": "write"}),
    ("claude-review.yml", "gate"): (CALLER_REPO, {"issues": "write", "pull-requests": "read",
                                                  "organization-projects": "write"}),
    ("claude-review.yml", "land"): (CALLER_REPO, {"issues": "write", "pull-requests": "write",
                                                  "organization-projects": "write"}),
    ("claude-auto.yml", "gate"): (CALLER_REPO, {"issues": "write", "pull-requests": "write",
                                                "organization-projects": "write"}),
    ("claude-auto.yml", "land"): (CALLER_REPO, {"contents": "write", "issues": "write",
                                                "pull-requests": "write", "organization-projects": "write"}),
    # contents: read is the closed-PR continuation's live-branch-tip read
    # (`repos/<repo>/branches/<head>`), a 404 on a private caller without it.
    ("claude-auto-review.yml", "gate"): (CALLER_REPO, {"contents": "read", "issues": "write",
                                                       "pull-requests": "write", "organization-projects": "write"}),
    ("claude-auto-review.yml", "land"): (CALLER_REPO, {"contents": "write", "issues": "write",
                                                       "pull-requests": "write", "organization-projects": "write"}),
    ("atlas-sync.yml", "sync"): ("inspect_ai", {"issues": "write", "pull-requests": "write",
                                                "actions": "read", "organization-projects": "write"}),
}

# The jobs whose mint is gated on the caller's app secrets because the
# workflow has a documented behaviour without the machine account: the dev
# agent's job-token degradation. Every other trusted job — the loops' gates
# included (decision: Ransom, 2026-09-18) — mints unconditionally and fails
# at the mint step when the secrets are absent.
CONDITIONAL_MINT = {("claude.yml", "gate"), ("claude.yml", "land")}
# The one workflow that keeps `|| github.token` (the marvin-less degradation).
JOB_TOKEN_FALLBACK = {"claude.yml"}

# Reusable workflow → the job that runs the agent (untrusted).
AGENT_JOBS = {"claude.yml": "agent", "claude-review.yml": "review",
              "claude-auto.yml": "fix", "claude-auto-review.yml": "fix"}
REUSABLE = sorted(AGENT_JOBS)
# The three that create runner-side commits (the base merge, the codex commit).
COMMITTING = ["claude.yml", "claude-auto.yml", "claude-auto-review.yml"]


def jobs(text: str) -> dict:
    """Top-level jobs of a workflow, by name, as the text of each job block
    (from its `  <name>:` line to the next top-level job)."""
    body = text[text.index("\njobs:\n") + len("\njobs:\n"):]
    starts = [(m.start(), m.group(1)) for m in re.finditer(r"^  ([a-z_-]+):$", body, re.M)]
    return {name: body[s:(starts[i + 1][0] if i + 1 < len(starts) else len(body))]
            for i, (s, name) in enumerate(starts)}


def code_lines(block: str) -> list:
    """The block without comment lines (a comment may quote an expression)."""
    return [line for line in block.splitlines() if not line.lstrip().startswith("#")]


def steps(job: str) -> list:
    """The job's steps, as text blocks, in order."""
    body = job[job.index("    steps:\n") + len("    steps:\n"):]
    starts = [m.start() for m in re.finditer(r"^      - ", body, re.M)]
    return [body[s:(starts[i + 1] if i + 1 < len(starts) else len(body))] for i, s in enumerate(starts)]


def mint_step(job: str) -> str:
    mints = [s for s in steps(job) if "\n        id: mint\n" in s]
    assert len(mints) == 1, "exactly one mint step"
    return mints[0]


def workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text()


# --- the secrets -------------------------------------------------------------


@pytest.mark.parametrize("name", REUSABLE)
def test_app_secrets_are_declared_and_the_pat_is_not(name):
    text = workflow(name)
    secrets = text[text.index("    secrets:\n"):text.index("\njobs:\n")]
    for s in ("MARVIN_APP_CLIENT_ID", "MARVIN_APP_PRIVATE_KEY"):
        m = re.search(rf"\n      {s}:\n((?:        .*\n)+)", secrets)
        assert m, s
        assert "        required: false\n" in m.group(1), s
    assert f"      {PAT}:\n" not in secrets
    # The client id's description records the retirement and the date.
    client = secrets[secrets.index("      MARVIN_APP_CLIENT_ID:"):secrets.index("      MARVIN_APP_PRIVATE_KEY:")]
    assert "the PAT was retired 2026-09-18" in client


# --- the mint step -----------------------------------------------------------


@pytest.mark.parametrize("name,job", sorted(WRITE_SETS))
def test_trusted_job_mints_first_for_exactly_its_write_set(name, job):
    text = workflow(name)
    block = jobs(text)[job]
    first, *_ = steps(block)
    mint = mint_step(block)
    assert first == mint, "the mint step is the job's first step"
    assert f"        uses: {MINT_ACTION}\n" in mint
    if (name, job) in CONDITIONAL_MINT:
        # Gated on the job-level boolean, which is the only `secrets` read in
        # an `if`-adjacent position (a step `if:` cannot read the context).
        assert HAS_APP_SECRETS in block
        assert MINT_IF in mint
    else:
        # The job cannot work without the machine account: no guard, so a
        # caller without the app secrets fails here, first and loudly.
        assert not [line for line in code_lines(block) if "HAS_APP_SECRETS" in line]
        assert not re.search(r"^        if:", mint, re.M)
    # The v2 action has no `client-id` input; `app-id` takes the Client ID.
    assert "          app-id: ${{ secrets.MARVIN_APP_CLIENT_ID }}\n" in mint
    assert "client-id" not in "\n".join(code_lines(mint))
    assert "          private-key: ${{ secrets.MARVIN_APP_PRIVATE_KEY }}\n" in mint
    assert "          owner: meridianlabs-ai\n" in mint
    repos, perms = WRITE_SETS[(name, job)]
    assert f"          repositories: {repos}\n" in mint
    got = dict(re.findall(r"^          permission-([a-z-]+): (\w+)$", mint, re.M))
    assert got == perms
    # The action revokes the token in its post step; nothing here keeps it.
    assert "skip-token-revoke" not in mint


@pytest.mark.parametrize("name,job", sorted(WRITE_SETS))
def test_every_token_read_in_a_trusted_job_is_the_minted_token(name, job):
    block = jobs(workflow(name))[job]
    allowed = (TOKEN_EXPR, TS_MONO_TOKEN_EXPR) if name == "atlas-sync.yml" else (TOKEN_EXPR,)
    reads = [line for line in code_lines(block) if "steps.mint" in line and "outputs" in line]
    assert reads, "the job reads the token it minted"
    for line in reads:
        assert any(f"${{{{ {e}" in line for e in allowed), line
        assert f"secrets.{PAT}" not in line
        # The job-token fallback survives only where the design kept the
        # marvin-less degradation; everywhere else the minted token is the
        # whole expression.
        if name in JOB_TOKEN_FALLBACK:
            assert re.search(rf"\$\{{\{{ {re.escape(TOKEN_EXPR)}( \|\| github\.token)? \}}\}}", line), line
        else:
            assert "github.token" not in line, line


@pytest.mark.parametrize("name", ["claude-auto.yml", "claude-auto-review.yml"])
def test_loop_gate_has_no_presence_check(name):
    """The loops' gates fail at the mint step without the app secrets; the
    resolve step no longer carries the `HAS_TOKEN` skip path the PAT had."""
    gate = jobs(workflow(name))["gate"]
    assert "HAS_TOKEN" not in gate
    resolve = [s for s in steps(gate) if "\n        id: resolve\n" in s]
    assert len(resolve) == 1
    assert "act skip; exit 0" in resolve[0], "the other skip reasons stay"


@pytest.mark.parametrize("name", REUSABLE)
def test_agent_job_never_sees_the_app_secrets_or_a_minted_token(name):
    text = workflow(name)
    agent = jobs(text)[AGENT_JOBS[name]]
    for needle in ("MARVIN_APP", "steps.mint", PAT, "HAS_APP_SECRETS"):
        assert not [line for line in code_lines(agent) if needle in line], needle
    # And the app secrets are named by no job but the trusted two.
    trusted = {j for (n, j) in WRITE_SETS if n == name}
    for job, block in jobs(text).items():
        named = any("MARVIN_APP" in line for line in code_lines(block))
        assert named == (job in trusted), job


# --- commit identity ---------------------------------------------------------


@pytest.mark.parametrize("name", COMMITTING)
def test_commit_identity_follows_the_gate_token(name):
    text = workflow(name)
    gate = jobs(text)[name and "gate"]
    assert ("      git_user_name: ${{ steps.mint.outcome == 'success' && 'meridian-marvin[bot]' "
            "|| 'i-am-marvin' }}\n") in gate
    assert ("      git_user_email: ${{ steps.mint.outcome == 'success' && "
            "'330132053+meridian-marvin[bot]@users.noreply.github.com' || "
            "'i-am-marvin@users.noreply.github.com' }}\n") in gate
    agent = jobs(text)[AGENT_JOBS[name]]
    sync = [s for s in steps(agent) if "sync-branch@main" in s]
    assert len(sync) == 1
    assert "          user-name: ${{ needs.gate.outputs.git_user_name }}\n" in sync[0]
    assert "          user-email: ${{ needs.gate.outputs.git_user_email }}\n" in sync[0]
    prep = [s for s in steps(agent) if "\n        id: codexprep\n" in s]
    assert len(prep) == 1
    assert "          GIT_USER_NAME: ${{ needs.gate.outputs.git_user_name }}\n" in prep[0]
    assert "          GIT_USER_EMAIL: ${{ needs.gate.outputs.git_user_email }}\n" in prep[0]
    assert 'git config user.name "$GIT_USER_NAME"' in prep[0]
    assert 'git config user.email "$GIT_USER_EMAIL"' in prep[0]


def test_no_workflow_or_composite_hardcodes_the_user_identity_in_git_config():
    files = list(WORKFLOWS.glob("*.yml")) + list((ROOT / ".github" / "actions").glob("*/action.yml"))
    offenders = [str(f.relative_to(ROOT)) for f in files
                 if re.search(r'git config user\.(name|email) "i-am-marvin', f.read_text())]
    assert offenders == []


def test_sync_branch_takes_the_identity_and_defaults_to_the_user():
    text = (ROOT / ".github" / "actions" / "sync-branch" / "action.yml").read_text()
    assert re.search(r"\n  user-name:\n(?:    .*\n)*?    default: i-am-marvin\n", text)
    assert re.search(r"\n  user-email:\n(?:    .*\n)*?    default: i-am-marvin@users\.noreply\.github\.com\n", text)
    assert 'git config user.name "$USER_NAME"' in text and 'git config user.email "$USER_EMAIL"' in text
    assert "        USER_NAME: ${{ inputs.user-name }}\n" in text
    assert "        USER_EMAIL: ${{ inputs.user-email }}\n" in text


# --- the hourly Atlas sync ---------------------------------------------------


def test_atlas_sync_preflight_is_the_project_read_only():
    text = workflow("atlas-sync.yml")
    sync = jobs(text)["sync"]
    code = "\n".join(code_lines(sync)).lower()
    assert "oauth" not in code and "scopes" not in code
    preflight = [s for s in steps(sync) if "Preflight" in s]
    assert len(preflight) == 1
    assert 'gh api graphql -f query=\'{node(id:"PVT_kwDOC7YMCM4BU68p"){... on ProjectV2{title}}}\'' in preflight[0]
    # Every step that talks to GitHub reads the two minted tokens: the fork
    # token as GH_TOKEN, the ts-mono read token under the name the script
    # routes ts-mono calls through.
    talking = [s for s in steps(sync) if "GH_TOKEN:" in s]
    assert len(talking) == 2, "the preflight and the sync"
    for s in talking:
        assert f"          GH_TOKEN: ${{{{ {TOKEN_EXPR} }}}}\n" in s, s.splitlines()[0]
        assert f"          GH_TOKEN_TS_MONO: ${{{{ {TS_MONO_TOKEN_EXPR} }}}}\n" in s, s.splitlines()[0]
    assert "gh api repos/meridianlabs-ai/ts-mono" in preflight[0]


def test_atlas_sync_mints_a_read_only_ts_mono_token_second():
    sync = jobs(workflow("atlas-sync.yml"))["sync"]
    first, second, *_ = steps(sync)
    assert "\n        id: mint\n" in first and "\n        id: mint_ts_mono\n" in second
    assert f"        uses: {MINT_ACTION}\n" in second
    # Unconditional, like the first: the sync cannot run without the secrets.
    assert not re.search(r"^        if:", second, re.M)
    assert "          app-id: ${{ secrets.MARVIN_APP_CLIENT_ID }}\n" in second
    assert "          owner: meridianlabs-ai\n" in second
    assert "          repositories: ts-mono\n" in second
    got = dict(re.findall(r"^          permission-([a-z-]+): (\w+)$", second, re.M))
    assert got == {"metadata": "read", "pull-requests": "read"}
    assert "write" not in "\n".join(code_lines(second))


# --- this repo's stubs, the examples, and the retired PAT ---------------------


def secrets_map_passes_only_the_app_secrets(text: str) -> None:
    lines = code_lines(text)
    assert not [line for line in lines if "secrets: inherit" in line]
    n_uses = sum(1 for line in lines if line.startswith("    uses: meridianlabs-ai/agents/.github/workflows/"))
    assert n_uses >= 1
    for entry in ("MARVIN_APP_CLIENT_ID", "MARVIN_APP_PRIVATE_KEY"):
        assert sum(1 for line in lines if line == f"      {entry}: ${{{{ secrets.{entry} }}}}") == n_uses, entry
    assert not [line for line in lines if PAT in line]
    assert "TRANSITION" not in text


@pytest.mark.parametrize("stub", ["claude-stub.yml", "claude-review-stub.yml", "claude-auto-stub.yml"])
def test_own_stubs_pass_the_app_secrets_and_nothing_else(stub):
    secrets_map_passes_only_the_app_secrets(workflow(stub))


@pytest.mark.parametrize("example", ["claude-stub.yml", "claude-review-stub.yml", "claude-auto-stub.yml"])
def test_examples_pass_the_app_secrets_and_nothing_else(example):
    secrets_map_passes_only_the_app_secrets((EXAMPLES / example).read_text())


def test_landing_smoke_mints_unconditionally_like_the_land_jobs():
    land = jobs((EXAMPLES / "landing-smoke.yml").read_text())["land"]
    mint = mint_step(land)
    assert not re.search(r"^        if:", mint, re.M)
    assert not [line for line in code_lines(land) if "HAS_APP_SECRETS" in line]
    assert f"          token: ${{{{ {TOKEN_EXPR} }}}}\n" in land


def test_the_retired_pat_is_named_only_by_design_history():
    """No tracked file outside design/ names the PAT: the workflows stopped
    accepting it on 2026-09-18, and a mention anywhere else would claim it is
    still in use. design/ keeps the history (the transition and the
    retirement itself)."""
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                             capture_output=True).stdout.decode().split("\0")
    # Regular files only: a tracked symlink to a directory is not a text.
    offenders = sorted(path for path in tracked
                       if path and not path.startswith("design/") and (ROOT / path).is_file()
                       and PAT in (ROOT / path).read_text(errors="replace"))
    assert offenders == []
