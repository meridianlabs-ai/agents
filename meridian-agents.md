# Meridian shared agent rules: start here

This is the one file that a Meridian agent reads. It links to the shared rules for agents that work on Meridian repos. Status: draft. Not in force until `rules-v1` is tagged.

## Files

- [`rules/README.md`](rules/README.md): how to read the rules, and how to adopt and change them.
- [`rules/pr-review.md`](rules/pr-review.md): the review bar, what blocks a merge, how to report findings.
- [`rules/code-changes.md`](rules/code-changes.md): how to change code and prepare a PR.
- [`rules/security.md`](rules/security.md): untrusted input, CI execution, workflow files, supply chain.
- [`rules/public-writing.md`](rules/public-writing.md): text that other people read, and how an agent discloses itself.
- [`rules/ownership.md`](rules/ownership.md): who is who, and who tracks CI and canary failures in each repo.
- ["Lessons from your work"](rules/README.md#lessons-from-your-work): how to propose a rule change from what you learn.

## Your duties

1. Read the files above. Obey every Required rule. Obey a Recommended rule unless you have a reason, and say the reason.
2. Before you act on a red CI run or a red canary alert, read `rules/ownership.md`. Act only if your person is the tracker for that repo.
3. When you learn a lesson from your work, ask your person, then open a small PR against `meridianlabs-ai/agents` that changes the rule.
