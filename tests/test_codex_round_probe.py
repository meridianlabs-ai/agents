"""Deliberately failing test for the codex fix-round check after #183.

It exists only to give this PR a failed `tests` run, so that the CI-fix
loop runs one round on the codex engine (`engine:codex`), whose
`fix-codex` job always runs the post-codex reclaim and its PATH check
(design/untrusted-agent-job.md, Implementation plan, step 1). The fix is
to delete this file; nothing else in the PR needs changing.
"""


def test_codex_round_probe():
    raise AssertionError(
        "Codex round probe: delete tests/test_codex_round_probe.py; it exists "
        "only to exercise a codex CI-fix round after #183."
    )
