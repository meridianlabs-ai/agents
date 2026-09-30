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
  and B for the OpenAI key, from the repository's sentinel secrets only;
- the canary runs weekly (off the hour) as well as on its push paths and by
  hand;
- each probe agent job requests an OIDC token exactly when the real one
  does (the Claude job for WIF, the codex job never) and runs the OIDC
  exchange probe before its scan, expecting a minted App token in the
  Claude job and none in the codex job; the probe script, run against a
  stub curl, revokes a minted token at once, prints no token and fails on
  the unexpected outcome or a failed revocation
  (design/untrusted-agent-job.md → Testing).
"""

import os
import re
import subprocess
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


def test_the_canary_runs_weekly_as_well_as_on_push_and_by_hand():
    # Per-job delivery is measured platform behaviour, not a contract, and no
    # push here would reveal a change in it (decision: Ransom, 2026-09-23).
    text = workflow(CANARY)
    on = text[text.index("\non:\n"):text.index("\npermissions:\n")]
    assert "\n  workflow_dispatch:\n" in on
    assert "\n  push:\n    paths:\n" in on
    cron = re.findall(r'^    - cron: "([^"]+)"', on, re.M)
    assert len(cron) == 1
    minute, hour, dom, month, dow = cron[0].split()
    assert minute.isdigit() and minute != "0", "off the top of the hour"
    assert hour.isdigit() and (dom, month) == ("*", "*") and dow.isdigit(), "once a week"


# --- the OIDC exchange probe (design/untrusted-agent-job.md → Testing) -------

EXCHANGE_PROBE = Path(__file__).resolve().parent / "app_token_exchange_probe.sh"


def job_permissions(block: str) -> str:
    if "    permissions:\n" not in block:
        return ""
    return block[block.index("    permissions:\n"):block.index("    steps:\n")]


@pytest.mark.parametrize("name", REUSABLE)
def test_the_probe_agent_jobs_request_oidc_like_the_real_ones(name):
    # The exchange probe measures what a runner in each agent job can mint,
    # so each probe job requests an OIDC token exactly when the real one does.
    real, probe = jobs(workflow(name)), jobs(workflow(PROBE))
    for role in ("claude", "codex"):
        wants = "id-token: write" in "\n".join(code_lines(job_permissions(real[real_jobs(name)[role]])))
        has = "id-token: write" in "\n".join(code_lines(job_permissions(probe[PROBE_JOBS[role]])))
        assert wants == has == (role == "claude"), (name, role)
    # A called workflow's jobs get no more than the calling job grants.
    caller = jobs(workflow(CANARY))["pipeline-probe"]
    assert "    permissions:\n      contents: read\n      id-token: write\n" in caller


def test_each_probe_agent_job_runs_the_exchange_probe_before_its_scan():
    probe = jobs(workflow(PROBE))
    for job, expect in (("agent", "minted"), ("agent-codex", "refused")):
        runs = [s for s in steps(probe[job]) if "app_token_exchange_probe.sh" in s]
        assert len(runs) == 1 and f"        run: bash tests/app_token_exchange_probe.sh {expect}\n" in runs[0], job
        assert steps(probe[job]).index(runs[0]) == len(steps(probe[job])) - 2, job
    for job in ("gate", "land"):
        assert "app_token_exchange_probe.sh" not in probe[job]
    assert '      - "tests/app_token_exchange_probe.sh"\n' in workflow(CANARY)


CURL_STUB = r"""#!/usr/bin/env bash
# Stub curl: -o <file> gets the canned body, -w '%{http_code}' prints the
# canned status. Each call is logged, one line of argv.
printf '%s\n' "$*" >>"$STUB/calls"
out=/dev/null
while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift 2 ;; *) url=$1; shift ;; esac; done
case "$url" in
  *audience=claude-code-github-action) printf '{"value":"JWT-SECRET-1"}' >"$out"; echo 200 ;;
  https://exchange.test/*) cat "$STUB/exchange_body" >"$out"; cat "$STUB/exchange_status" ;;
  */installation/token) cat "$STUB/revoke_status" ;;
  *) echo "unexpected url $url" >&2; exit 7 ;;
esac
"""


def run_probe(tmp_path, expect, *, oidc=True, status="200", body='{"token":"ghs_APPTOKEN"}', revoke="204"):
    stub = tmp_path / "stub"
    (stub / "bin").mkdir(parents=True)
    (stub / "bin" / "curl").write_text(CURL_STUB)
    (stub / "bin" / "curl").chmod(0o755)
    (stub / "exchange_status").write_text(status)
    (stub / "exchange_body").write_text(body)
    (stub / "revoke_status").write_text(revoke)
    summary = tmp_path / "summary"
    env = {"PATH": f"{stub / 'bin'}:{os.environ['PATH']}", "STUB": str(stub), "GITHUB_STEP_SUMMARY": str(summary),
           "APP_TOKEN_EXCHANGE_URL": "https://exchange.test/api/github/github-app-token-exchange",
           "GITHUB_API_URL": "https://api.test", "HOME": str(tmp_path)}
    if oidc:
        env |= {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.test/token?api-version=2.0",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "REQ-SECRET"}
    r = subprocess.run(["bash", str(EXCHANGE_PROBE), expect], env=env, capture_output=True, text=True)
    calls = (stub / "calls").read_text().splitlines() if (stub / "calls").exists() else []
    return r, calls, summary.read_text() if summary.exists() else ""


def test_exchange_probe_revokes_a_minted_token_and_prints_nothing_from_it(tmp_path):
    r, calls, summary = run_probe(tmp_path, "minted")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Claude App token exchange as runner: minted (exchange HTTP 200; revoked, HTTP 204); expected minted" in r.stdout
    assert summary.strip() == r.stdout.splitlines()[-1]
    assert len(calls) == 3 and "-X DELETE" in calls[2] and "https://api.test/installation/token" in calls[2]
    assert "Authorization: Bearer ghs_APPTOKEN" in calls[2] and "Authorization: Bearer JWT-SECRET-1" in calls[1]
    # The tokens appear only in mask commands, which the runner hides.
    for secret in ("ghs_APPTOKEN", "JWT-SECRET-1", "REQ-SECRET"):
        printed = [line for line in (r.stdout + r.stderr + summary).splitlines() if secret in line]
        assert all(line == f"::add-mask::{secret}" for line in printed), secret
    # A minted token is a failure once the App is uninstalled (step 6).
    r, calls, _ = run_probe(tmp_path / "b", "refused")
    assert r.returncode == 1 and "outcome is minted, expected refused" in r.stdout
    assert "-X DELETE" in calls[-1], "revoked even when unexpected"


def test_exchange_probe_fails_when_the_revocation_fails(tmp_path):
    r, _, _ = run_probe(tmp_path, "minted", revoke="401")
    assert r.returncode == 1 and "revoking the minted App token answered HTTP 401" in r.stdout


@pytest.mark.parametrize("status, body, detail", [
    ("401", '{"error":{"message":"Workflow validation failed"}}', "exchange HTTP 401: Workflow validation failed"),
    ("404", '{"message":"no installation\\n\\u001b[31m"}', "exchange HTTP 404: no installation[31m"),
    ("502", "<html>bad gateway</html>", "exchange HTTP 502"),
    ("200", '{"token":null}', "exchange HTTP 200"),
])
def test_exchange_probe_reports_a_refusal(tmp_path, status, body, detail):
    r, calls, _ = run_probe(tmp_path, "refused", status=status, body=body)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"refused ({detail}); expected refused" in r.stdout
    assert not any("DELETE" in c for c in calls)
    r, _, _ = run_probe(tmp_path / "b", "minted", status=status, body=body)
    assert r.returncode == 1 and "outcome is refused, expected minted" in r.stdout


def test_exchange_probe_without_an_oidc_request_token_is_refused_without_a_call(tmp_path):
    r, calls, _ = run_probe(tmp_path, "refused", oidc=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "refused (no OIDC request token in this job); expected refused" in r.stdout
    assert calls == []


def test_exchange_probe_refuses_an_unknown_expectation(tmp_path):
    r, calls, _ = run_probe(tmp_path, "maybe")
    assert r.returncode == 2 and calls == []
