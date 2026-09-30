"""Deliberately failing test for the untrusted-agent-job step 1 checks.

It exists only to give this PR a failed `tests` run, so that the CI-fix
loop runs one Claude round on the job token (design/untrusted-agent-job.md,
Implementation plan, step 1). The fix is to delete this file; nothing else
in the PR needs changing.
"""


def test_ci_fix_probe():
    raise AssertionError(
        "CI-fix probe: delete tests/test_ci_fix_probe.py; it exists only to "
        "exercise the CI-fix loop for untrusted-agent-job step 1."
    )
