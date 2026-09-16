"""Phase 2 of the credential separation: the trusted jobs mint the machine
account's token (design/architecture.md → Landing job).

The reusable workflows accept the GitHub App's two secrets next to the
`MARVIN_TOKEN` PAT, and each `gate` and `land` job mints a one-hour
installation token scoped to the caller repo and to that job's writes, as its
first step. These are structural checks on the workflow text (PyYAML is not a
test dependency), one per rule the transition relies on:

- both app secrets are declared optional `workflow_call` secrets;
- every trusted job mints first, gated on the job-level `HAS_APP_SECRETS`
  boolean (a step `if:` cannot read `secrets`), for `meridianlabs-ai` and
  exactly the caller repo, with exactly the permissions the design table
  lists for that job;
- every `secrets.MARVIN_TOKEN` read in a trusted job is the ONE expression
  `steps.mint.outputs.token || secrets.MARVIN_TOKEN`, the loops' `HAS_TOKEN`
  presence check included;
- the job that runs the agent names neither the app secrets, nor the mint
  step's token, nor the PAT;
- the commit identity follows the token: the gate publishes it, the agent
  job passes it to `sync-branch` and its codex prep, and no workflow hardcodes
  the User's identity in a `git config` any more;
- the hourly Atlas sync mints for the inspect_ai fork and its preflight is
  the project read only (an installation token reports no OAuth scopes);
- this repo's own stubs pass the app secrets and no PAT (the first caller on
  the app path); the examples pass both, explicitly, never `secrets: inherit`.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
EXAMPLES = ROOT / "examples"

MINT_ACTION = "actions/create-github-app-token@v2"
TOKEN_EXPR = "steps.mint.outputs.token || secrets.MARVIN_TOKEN"
HAS_APP_SECRETS = "      HAS_APP_SECRETS: ${{ secrets.MARVIN_APP_CLIENT_ID != '' }}"
CALLER_REPO = "${{ github.event.repository.name }}"

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
    return [l for l in block.splitlines() if not l.lstrip().startswith("#")]


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
def test_app_secrets_are_declared_optional_next_to_the_pat(name):
    text = workflow(name)
    secrets = text[text.index("    secrets:\n"):text.index("\njobs:\n")]
    for s in ("MARVIN_TOKEN", "MARVIN_APP_CLIENT_ID", "MARVIN_APP_PRIVATE_KEY"):
        m = re.search(rf"\n      {s}:\n((?:        .*\n)+)", secrets)
        assert m, s
        assert "        required: false\n" in m.group(1), s
    # The app secrets say what they replace; the PAT says it is transitional.
    client = secrets[secrets.index("      MARVIN_APP_CLIENT_ID:"):secrets.index("      MARVIN_APP_PRIVATE_KEY:")]
    assert "REPLACES MARVIN_TOKEN" in client
    pat = secrets[secrets.index("      MARVIN_TOKEN:"):secrets.index("      MARVIN_APP_CLIENT_ID:")]
    assert "TRANSITION" in pat


# --- the mint step -----------------------------------------------------------


@pytest.mark.parametrize("name,job", sorted(WRITE_SETS))
def test_trusted_job_mints_first_for_exactly_its_write_set(name, job):
    text = workflow(name)
    block = jobs(text)[job]
    # Gated on the job-level boolean, which is the only `secrets` read in an
    # `if`-adjacent position (a step `if:` cannot read the context itself).
    assert HAS_APP_SECRETS in block
    first, *_ = steps(block)
    mint = mint_step(block)
    assert first == mint, "the mint step is the job's first step"
    assert f"        uses: {MINT_ACTION}\n" in mint
    assert "        if: env.HAS_APP_SECRETS == 'true'\n" in mint
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
def test_every_pat_read_in_a_trusted_job_is_the_one_token_expression(name, job):
    block = jobs(workflow(name))[job]
    reads = [l for l in code_lines(block) if "secrets.MARVIN_TOKEN" in l]
    assert reads, "the job still names the PAT as its fallback"
    for line in reads:
        assert TOKEN_EXPR in line, line
    # And the minted token is never read except through that expression.
    for line in code_lines(block):
        if "steps.mint.outputs" in line:
            assert TOKEN_EXPR in line, line


@pytest.mark.parametrize("name", ["claude-auto.yml", "claude-auto-review.yml"])
def test_loop_presence_check_sees_the_minted_token(name):
    gate = jobs(workflow(name))["gate"]
    assert f"          HAS_TOKEN: ${{{{ ({TOKEN_EXPR}) != '' }}}}\n" in gate


@pytest.mark.parametrize("name", REUSABLE)
def test_agent_job_never_sees_the_app_secrets_or_a_minted_token(name):
    text = workflow(name)
    agent = jobs(text)[AGENT_JOBS[name]]
    for needle in ("MARVIN_APP", "steps.mint", "secrets.MARVIN_TOKEN", "HAS_APP_SECRETS"):
        assert not [l for l in code_lines(agent) if needle in l], needle
    # And the app secrets are named by no job but the trusted two.
    trusted = {j for (n, j) in WRITE_SETS if n == name}
    for job, block in jobs(text).items():
        named = any("MARVIN_APP" in l for l in code_lines(block))
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
    # Every step that talks to GitHub reads the one token expression.
    talking = [s for s in steps(sync) if "GH_TOKEN:" in s]
    assert len(talking) == 2, "the preflight and the sync"
    for s in talking:
        assert f"          GH_TOKEN: ${{{{ {TOKEN_EXPR} }}}}\n" in s, s.splitlines()[0]


# --- this repo's stubs and the examples --------------------------------------


@pytest.mark.parametrize("stub", ["claude-stub.yml", "claude-review-stub.yml", "claude-auto-stub.yml"])
def test_own_stubs_pass_the_app_secrets_and_no_pat(stub):
    lines = code_lines(workflow(stub))
    assert not [l for l in lines if "secrets: inherit" in l]
    n_uses = sum(1 for l in lines if l.startswith("    uses: meridianlabs-ai/agents/.github/workflows/"))
    assert n_uses >= 1
    assert sum(1 for l in lines if l == "      MARVIN_APP_CLIENT_ID: ${{ secrets.MARVIN_APP_CLIENT_ID }}") == n_uses
    assert sum(1 for l in lines if l == "      MARVIN_APP_PRIVATE_KEY: ${{ secrets.MARVIN_APP_PRIVATE_KEY }}") == n_uses
    assert not [l for l in lines if "MARVIN_TOKEN" in l]


@pytest.mark.parametrize("example", ["claude-stub.yml", "claude-review-stub.yml", "claude-auto-stub.yml"])
def test_examples_pass_both_explicitly_with_the_pat_marked_transitional(example):
    text = (EXAMPLES / example).read_text()
    lines = code_lines(text)
    assert not [l for l in lines if "secrets: inherit" in l]
    n_uses = sum(1 for l in lines if l.startswith("    uses: meridianlabs-ai/agents/.github/workflows/"))
    assert n_uses >= 1
    for entry in ("MARVIN_APP_CLIENT_ID", "MARVIN_APP_PRIVATE_KEY", "MARVIN_TOKEN"):
        assert sum(1 for l in lines if l == f"      {entry}: ${{{{ secrets.{entry} }}}}") == n_uses, entry
    assert text.count("# TRANSITION: the machine account's PAT. Delete this line once this") == n_uses
