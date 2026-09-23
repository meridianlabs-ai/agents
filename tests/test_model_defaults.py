"""The Claude model every reusable workflow requests by default
(design/architecture.md → Model selection).

The default is the `opus` alias, never a dated model id, so it floats with
Opus releases (decision: Ransom, 2026-09-23), and `--fallback-model default`
stays so a retired primary degrades to the account default. Callers that pass
no `model` pick the default up, so it is pinned here for all four Claude
workflows. Text-based like the other structural checks: PyYAML is not a test
dependency. Run with `python3 -m pytest` from the repo root.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ["claude.yml", "claude-review.yml", "claude-auto.yml", "claude-auto-review.yml"]


def workflow_call_inputs(path):
    # Inputs are the 6-space keys under `on.workflow_call.inputs`, their
    # bodies the 8-space lines that follow (the same walk as
    # test_dev_agent_composer.py's input-type check).
    lines = path.read_text().splitlines()
    start = lines.index("    inputs:") + 1
    inputs, current = {}, None
    for line in lines[start:]:
        if line.startswith("    ") and not line.startswith("     "):
            break                                   # `secrets:` — the next 4-space key
        if line.startswith("      ") and not line.startswith("       ") and line.rstrip().endswith(":"):
            current = line.strip()[:-1]
            inputs[current] = {}
        elif current and line.startswith("        ") and not line.startswith("         "):
            key, _, value = line.strip().partition(":")
            inputs[current][key] = value.strip()
    return inputs


@pytest.mark.parametrize("name", WORKFLOWS)
def test_default_model_is_the_opus_alias_with_the_account_default_fallback(name):
    inputs = workflow_call_inputs(ROOT / ".github" / "workflows" / name)
    assert inputs["model"]["default"] == '"opus"'
    assert inputs["fallback_model"]["default"] == '"default"'


@pytest.mark.parametrize("name", WORKFLOWS)
def test_model_inputs_reach_claude_code(name):
    text = (ROOT / ".github" / "workflows" / name).read_text()
    assert "${{ inputs.model != '' && format('--model {0}', inputs.model) || '' }}" in text
    assert ("${{ inputs.model != '' && inputs.fallback_model != '' && "
            "format('--fallback-model {0}', inputs.fallback_model) || '' }}") in text
