"""The engine-isolation canary's pipeline probe measures the shape the agent
workflows actually have (Claude Security finding 4629153, fix criterion 3).

The probe (.github/workflows/engine-isolation-canary-pipeline.yml) is what
shows, on a hosted runner and with synthetic sentinels only, that the job
message of the job running the Claude agent carries no App secret and no
OpenAI key. That evidence covers the four reusable workflows only while the
probe keeps their shape, so these checks hold the two together:

- every job of each reusable workflow references the same secrets, by name,
  as the probe's job in the same role (gate, Claude agent, codex agent,
  land), no other job references any, and the probe declares the same
  `workflow_call` secrets;
- the probe's two agent jobs are selected by the gate's `engine` output at
  the job level like the real ones, and its land job needs all three and
  runs `always()` after a successful gate;
- each probe job's last step is the root memory scan, its expectations
  follow from that job's references (sentinel A stands in for both App
  secrets, B for the OpenAI key), and the land job fails unless the
  selected agent job succeeded;
- the canary calls the probe once per engine with A for both App secrets
  and B for the OpenAI key, from the repository's sentinel secrets only.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_app_token_minting import AGENT_JOBS, REUSABLE, code_lines, jobs, steps, workflow  # noqa: E402

PROBE = "engine-isolation-canary-pipeline.yml"
CANARY = "engine-isolation-canary.yml"
APP = {"MARVIN_APP_CLIENT_ID", "MARVIN_APP_PRIVATE_KEY"}
OPENAI = "OPENAI_API_KEY"
# role -> the probe's job name; the real workflows' names come from AGENT_JOBS.
PROBE_JOBS = {"gate": "gate", "claude": "agent", "codex": "agent-codex", "land": "land"}


def secret_refs(block: str) -> set:
    return set(re.findall(r"secrets\.([A-Za-z0-9_]+)", "\n".join(code_lines(block))))


def declared_secrets(text: str) -> set:
    head = text[: text.index("\njobs:\n")]
    section = head[head.index("\n    secrets:\n") + len("\n    secrets:\n"):]
    return set(re.findall(r"^      ([A-Z0-9_]+):$", section, re.M))


def real_jobs(name: str) -> dict:
    claude, codex = AGENT_JOBS[name]
    return {"gate": "gate", "claude": claude, "codex": codex, "land": "land"}


def job_line(block: str, key: str) -> str:
    return next(line for line in block.splitlines() if line.startswith(f"    {key}: "))


@pytest.mark.parametrize("name", REUSABLE)
def test_each_role_references_the_same_secrets_as_the_probe(name):
    real, probe = jobs(workflow(name)), jobs(workflow(PROBE))
    for role, job in real_jobs(name).items():
        assert secret_refs(real[job]) == secret_refs(probe[PROBE_JOBS[role]]), (role, job)
    others = set(real) - set(real_jobs(name).values())
    assert not {job: secret_refs(real[job]) for job in others if secret_refs(real[job])}
    assert declared_secrets(workflow(name)) == declared_secrets(workflow(PROBE))


def test_the_probe_references_what_the_design_says():
    probe = jobs(workflow(PROBE))
    assert set(probe) == set(PROBE_JOBS.values())
    assert secret_refs(probe["gate"]) == APP == secret_refs(probe["land"])
    assert secret_refs(probe["agent"]) == set()
    assert secret_refs(probe["agent-codex"]) == {OPENAI}
    # The gate and land jobs reference the App secrets in job env as well as
    # at a step that runs, as claude.yml's do.
    for job in ("gate", "land"):
        assert "      HAS_APP_SECRETS: ${{ secrets.MARVIN_APP_CLIENT_ID != '' }}\n" in probe[job]


@pytest.mark.parametrize("name", REUSABLE)
def test_the_probe_selects_its_jobs_like_the_real_workflow(name):
    real, probe = jobs(workflow(name)), jobs(workflow(PROBE))
    roles = real_jobs(name)
    for role in ("claude", "codex"):
        assert job_line(probe[PROBE_JOBS[role]], "needs") == job_line(real[roles[role]], "needs") == "    needs: gate"
    assert "needs.gate.outputs.engine != 'codex'" in job_line(real[roles["claude"]], "if")
    assert "needs.gate.outputs.engine == 'codex'" in job_line(real[roles["codex"]], "if")
    assert job_line(probe["agent"], "if") == "    if: needs.gate.outputs.ok == 'true' && needs.gate.outputs.engine != 'codex'"
    assert job_line(probe["agent-codex"], "if") == "    if: needs.gate.outputs.ok == 'true' && needs.gate.outputs.engine == 'codex'"
    assert job_line(real["land"], "needs") == f"    needs: [gate, {roles['claude']}, {roles['codex']}]"
    assert job_line(probe["land"], "needs") == "    needs: [gate, agent, agent-codex]"
    assert job_line(real["land"], "if").startswith("    if: always() && ")
    assert job_line(probe["land"], "if") == \
        "    if: always() && needs.gate.result == 'success' && needs.gate.outputs.ok == 'true'"


def test_each_probe_job_ends_with_the_scan_its_references_imply():
    probe = jobs(workflow(PROBE))
    for job, block in probe.items():
        refs = secret_refs(block)
        want = ("present" if refs & APP else "absent", "present" if OPENAI in refs else "absent")
        last = steps(block)[-1]
        assert f"        run: sudo python3 tests/secret_delivery_scan.py {want[0]} {want[1]}\n" in last, job
        # Nothing but the scan and the checkout reads the tree: no step
        # between the references and the scan prints a secret.
        for step in steps(block):
            assert "echo" not in step or "secrets." not in step, (job, step.splitlines()[0])
    assert '[ "$AGENT_RESULT" = success ]' in probe["land"]
    assert ("      AGENT_RESULT: ${{ needs.gate.outputs.engine == 'codex' && needs.agent-codex.result "
            "|| needs.agent.result }}\n") in probe["land"]


def test_the_canary_runs_the_probe_per_engine_with_sentinels_only():
    caller = jobs(workflow(CANARY))["pipeline-probe"]
    assert "        engine: [claude, codex]\n" in caller
    assert f"    uses: ./.github/workflows/{PROBE}\n" in caller
    assert "      engine: ${{ matrix.engine }}\n" in caller
    passed = dict(re.findall(r"^      ([A-Z0-9_]+): \$\{\{ secrets\.([A-Z0-9_]+) \}\}$", caller, re.M))
    assert passed == {"MARVIN_APP_CLIENT_ID": "CANARY_SENTINEL_A", "MARVIN_APP_PRIVATE_KEY": "CANARY_SENTINEL_A",
                      OPENAI: "CANARY_SENTINEL_B"}
    # The canary as a whole reads no secret but the two sentinels.
    assert secret_refs(workflow(CANARY)) == {"CANARY_SENTINEL_A", "CANARY_SENTINEL_B"}
    assert "secrets: inherit" not in "\n".join(code_lines(workflow(CANARY)))


def test_the_stand_in_action_prints_lengths_only():
    text = (Path(__file__).resolve().parent / "fixtures" / "secret-input" / "action.yml").read_text()
    runs = text[text.index("\nruns:\n"):]
    echoes = [line.strip() for line in runs.splitlines() if line.strip().startswith("echo")]
    assert echoes and all(re.fullmatch(r'echo "[^$]*(\$\{#[A-Z_]+\}[^$]*)+"', e) for e in echoes), echoes
