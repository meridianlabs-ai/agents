"""The `npm-pins` composite and the npm installs it versions.

The codex jobs ran openai/codex-action with no `codex-version`, so each run
installed the newest @openai/codex and @openai/codex-responses-api-proxy,
and the reviewer's sandbox step installed the newest
@anthropic-ai/sandbox-runtime as root. Both now take an exact version from
`.github/actions/npm-pins/package.json`, which Dependabot moves with a 7-day
cooldown. The composite's step is lifted from its action.yml and run against
the real manifest and against ranges, tags and missing entries (each
refused). Structural checks: every codex-action step passes the pinned
version from a pins step that runs whenever the action can; no workflow or
composite runs `npm install` without a version; Dependabot watches the
manifest with the cooldown.
"""

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PINS = ROOT / ".github" / "actions" / "npm-pins"
MANIFEST = PINS / "package.json"
WORKFLOWS = ROOT / ".github" / "workflows"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_app_token_minting import AGENT_JOBS, REUSABLE, jobs, steps  # noqa: E402
from test_land_helpers import lift_run  # noqa: E402

STEP = lift_run((PINS / "action.yml").read_text(), "    - id: read")
EXACT = re.compile(r"^\d+\.\d+\.\d+$")
PACKAGES = {"codex": "@openai/codex", "sandbox-runtime": "@anthropic-ai/sandbox-runtime"}
PINS_USES = "        uses: meridianlabs-ai/agents/.github/actions/npm-pins@main\n"
CODEX_ACTION = re.compile(r"        uses: openai/codex-action@[0-9a-f]{40} # v\S+\n        with:\n")


def read_pins(tmp_path, manifest):
    pins = tmp_path / "npm-pins"
    pins.mkdir()
    (pins / "package.json").write_text(json.dumps(manifest))
    out = tmp_path / "github-output"
    out.write_text("")
    env = {**os.environ, "GITHUB_ACTION_PATH": str(pins), "GITHUB_OUTPUT": str(out)}
    r = subprocess.run(["bash", "-c", STEP], text=True, capture_output=True, env=env, timeout=30, check=False)
    return r, out.read_text()


def condition(step: str):
    """The step's `if:` as YAML reads it (None when it has none)."""
    (parsed,) = yaml.safe_load(textwrap.dedent(step))
    return parsed.get("if")


def test_the_composite_outputs_what_its_step_writes():
    action = yaml.safe_load((PINS / "action.yml").read_text())
    (step,) = action["runs"]["steps"]
    assert step["id"] == "read"
    assert {k: v["value"] for k, v in action["outputs"].items()} == {
        key: f"${{{{ steps.read.outputs.{key} }}}}" for key in PACKAGES}


def test_the_manifest_pins_each_package_to_an_exact_version():
    deps = json.loads(MANIFEST.read_text())["dependencies"]
    assert set(deps) == set(PACKAGES.values())
    assert all(EXACT.match(v) for v in deps.values()), deps


def test_the_step_outputs_the_manifests_versions(tmp_path):
    deps = json.loads(MANIFEST.read_text())["dependencies"]
    r, out = read_pins(tmp_path, json.loads(MANIFEST.read_text()))
    assert r.returncode == 0, r.stdout + r.stderr
    assert out == "".join(f"{key}={deps[pkg]}\n" for key, pkg in PACKAGES.items())


@pytest.mark.parametrize("key", sorted(PACKAGES))
@pytest.mark.parametrize("version", ["^0.167.1", "~0.0.82", "latest", "0.167", "0.167.1-alpha.1", "",
                                     "0.167.1 || 9.9.9", "0.167.1\n9.9.9"])
def test_the_step_refuses_anything_but_an_exact_version(tmp_path, key, version):
    deps = {pkg: "1.2.3" for pkg in PACKAGES.values()}
    deps[PACKAGES[key]] = version
    r, out = read_pins(tmp_path, {"dependencies": deps})
    assert r.returncode != 0
    assert f"{key}=" not in out
    if version:
        assert "not an exact version" in r.stdout


@pytest.mark.parametrize("key", sorted(PACKAGES))
def test_the_step_refuses_a_missing_package(tmp_path, key):
    deps = {pkg: "1.2.3" for pkg in PACKAGES.values() if pkg != PACKAGES[key]}
    r, out = read_pins(tmp_path, {"dependencies": deps})
    assert r.returncode != 0
    assert f"{key}=" not in out


@pytest.mark.parametrize("name", REUSABLE)
def test_every_codex_action_step_installs_the_pinned_codex(name):
    codex_job = jobs((WORKFLOWS / name).read_text())[AGENT_JOBS[name][1]]
    m = CODEX_ACTION.search(codex_job)
    assert m and len(CODEX_ACTION.findall(codex_job)) == 1
    assert codex_job[m.end():].startswith("          codex-version: ${{ steps.npmpins.outputs.codex }}\n")
    job_steps = steps(codex_job)
    pins = [i for i, s in enumerate(job_steps) if "\n        id: npmpins\n" in s]
    node = [i for i, s in enumerate(job_steps) if s.startswith("      - name: Set up Node for codex-action\n")]
    assert len(pins) == 1 and len(node) == 1
    assert PINS_USES in job_steps[pins[0]]
    # Right before the Node step, under its condition: it runs whenever the
    # codex user is created and so whenever codex-action can.
    assert pins[0] == node[0] - 1
    assert condition(job_steps[pins[0]]) == condition(job_steps[node[0]])


def test_the_review_sandbox_installs_the_pinned_runtime_without_scripts():
    review = jobs((WORKFLOWS / "claude-review.yml").read_text())["review"]
    job_steps = steps(review)
    pins = next(i for i, s in enumerate(job_steps) if "\n        id: sandboxpins\n" in s)
    install = next(i for i, s in enumerate(job_steps) if "\n        id: sandbox\n" in s)
    assert install == pins + 1
    assert PINS_USES in job_steps[pins]
    assert condition(job_steps[pins]) == condition(job_steps[install])
    assert "          SRT_VERSION: ${{ steps.sandboxpins.outputs.sandbox-runtime }}\n" in job_steps[install]
    assert ('          sudo npm install -g --ignore-scripts "@anthropic-ai/sandbox-runtime@${SRT_VERSION:?}"\n'
            in job_steps[install])


def test_the_canary_runs_codex_action_and_the_by_name_checks_on_the_pinned_codex():
    text = (WORKFLOWS / "engine-isolation-canary.yml").read_text()
    for job in ("caller-recipes", "codex-action-path"):
        assert "        uses: ./.github/actions/npm-pins\n" in jobs(text)[job]
    action = jobs(text)["codex-action-path"]
    m = CODEX_ACTION.search(action)
    assert action[m.end():].startswith("          codex-version: ${{ steps.npmpins.outputs.codex }}\n")
    assert '[ "$got" = "codex-cli $CODEX_VERSION" ]' in action
    assert '      - ".github/actions/npm-pins/**"\n' in text


def test_every_npm_install_command_line_names_a_pinned_version():
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted((ROOT / ".github" / "actions").glob("*/action.yml"))
    installs = []
    for p in files:
        for line in p.read_text().splitlines():
            # A command line, not prose in a comment or description.
            code = line.strip()
            if not re.match(r"(sudo )?npm (install|i)\b", code):
                continue
            installs.append(p.name)
            # One package, its version a variable set from the pins that
            # fails the step when empty (an empty version installs the newest).
            assert re.search(r'npm install -g --ignore-scripts "@[a-z-]+/[a-z-]+@\$\{[A-Z_]+:\?\}"', code), (p.name, code)
    assert sorted(installs) == ["claude-review.yml", "engine-isolation-canary.yml"]


def test_dependabot_moves_the_pins_with_a_seven_day_cooldown():
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    npm = [u for u in config["updates"] if u["package-ecosystem"] == "npm"]
    assert len(npm) == 1
    assert npm[0]["directory"] == "/.github/actions/npm-pins"
    assert npm[0]["cooldown"]["default-days"] >= 7
