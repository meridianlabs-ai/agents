"""Deliberately failing test for the step 7 post-merge check.

It exists only to give this PR a failed `tests` run, so that the CI-fix loop
runs one round on the codex engine (`engine:codex`), now on OpenAI workload
identity federation (design/untrusted-agent-job.md, Implementation plan,
step 7). The fix is to delete this file; nothing else in the PR needs
changing.
"""


def test_step7_codex_round_probe():
    raise AssertionError(
        "Step 7 codex round probe: delete tests/test_step7_codex_round_probe.py; "
        "it exists only to exercise a codex CI-fix round on OpenAI federation."
    )
