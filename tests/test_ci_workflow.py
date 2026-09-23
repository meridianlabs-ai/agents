"""The repo's own CI workflow (.github/workflows/tests.yml) and the @auto
stub that watches it (.github/workflows/claude-auto-stub.yml).

tests.yml runs this suite on every PR and push to main with no secrets: a
read-only job token, no `secrets.` reference, no `pull_request_target`, a
checkout that persists no credential (AGENTS.md → No git credential is ever
written to the workspace), and nothing that could start an agent. The
dogfood @auto stub is examples/claude-auto-stub.yml with its REQUIRED EDIT
made: its `workflow_run` names tests.yml's `name:`.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
TESTS_YML = WORKFLOWS / "tests.yml"
AUTO_STUB = WORKFLOWS / "claude-auto-stub.yml"
AUTO_EXAMPLE = ROOT / "examples" / "claude-auto-stub.yml"


def code_lines(text):
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def top_level_block(text, key):
    """The lines of the top-level `key:` block, comments dropped."""
    lines = code_lines(text)
    start = lines.index(f"{key}:")
    block = []
    for line in lines[start + 1:]:
        if not line.startswith(" "):
            break
        block.append(line)
    return block


def workflow_name(text):
    (name,) = re.findall(r"^name: (.+)$", text, re.M)
    return name


def test_tests_workflow_runs_the_suite_on_every_pr_and_push_to_main():
    text = TESTS_YML.read_text()
    # no path filter and no other event: the full suite on every PR and on
    # every push to main
    assert top_level_block(text, "on") == [
        "  pull_request:",
        "  push:",
        "    branches: [main]",
    ]
    # the command tests/README.md and AGENTS.md document
    assert "      - run: python3 -m pytest" in code_lines(text)


def test_tests_workflow_holds_no_secret_and_a_read_only_token():
    text = TESTS_YML.read_text()
    code = "\n".join(code_lines(text))
    assert top_level_block(text, "permissions") == ["  contents: read"]
    # no job widens the token
    assert code.count("permissions:") == 1
    assert "write" not in code
    assert "secrets." not in code and "secrets:" not in code
    assert "pull_request_target" not in code
    assert "id-token" not in code


def test_tests_workflow_checkouts_persist_no_credential():
    lines = code_lines(TESTS_YML.read_text())
    checkouts = [i for i, line in enumerate(lines) if "uses: actions/checkout@" in line]
    assert checkouts
    for i in checkouts:
        assert lines[i + 1:i + 3] == ["        with:", "          persist-credentials: false"]


def test_tests_workflow_pins_actions_by_major_tag_and_starts_no_agent():
    code = code_lines(TESTS_YML.read_text())
    uses = [line.split("uses:", 1)[1].strip() for line in code if "uses:" in line]
    assert uses
    # pinned the way the other workflows pin (actions/checkout@v4, ...): a
    # third-party action at its major tag, never a branch; nothing from this
    # repo, so no reusable agent workflow and no composite runs
    for ref in uses:
        assert re.fullmatch(r"actions/[a-z-]+@v\d+", ref), ref
    assert not [line for line in code if re.search(r"\bgh\b|@claude|@review|@auto", line)]


def test_own_auto_stub_is_the_example_watching_the_tests_workflow():
    stub = AUTO_STUB.read_text()
    example = AUTO_EXAMPLE.read_text()
    watched = re.findall(r"^    workflows: (.+)$", stub, re.M)
    assert watched == [f'["{workflow_name(TESTS_YML.read_text())}"]']

    def body(text):
        # from `name:` on, the watched-workflow line and its comment set aside
        text = text[text.index("\nname: Claude Auto\n"):]
        text = re.sub(r"^    workflows: .+\n", "", text, flags=re.M)
        return re.sub(r"^    # .*CI workflow.*\n", "", text, flags=re.M)

    assert body(stub) == body(example)
    # both halves of @auto, as in the example
    jobs = top_level_block(stub, "jobs")
    assert [line for line in jobs if re.fullmatch(r"  [a-z-]+:", line)] == ["  ci-fix:", "  review-fix:"]
