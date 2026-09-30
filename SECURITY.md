# Security

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository:
[Report a vulnerability](https://github.com/meridianlabs-ai/agents/security/advisories/new)
(enabled 2026-09-18). Never open a public issue or pull request for a
security problem: this repository is public, and its workflows run on every
Meridian repository the moment a fix merges, so a public report is a
public exploit window.

Include what you can of: the workflow, composite action or script involved;
the event or trigger that reaches it; what an attacker controls (a PR head,
a comment, test output, a manifest field); the write or credential it
reaches; and a run link if you have one. Do not include a live token. A
maintainer acknowledges the report in the advisory thread, confirms or
disputes it against `main`, and fixes it through a pull request from a
maintainer's machine, since agents running in CI cannot change workflow
files here. Disclosure is coordinated with you in the same thread.

## What this repository is

Reusable GitHub Actions workflows that run coding agents (Claude Code, or
OpenAI Codex on request) as a dev agent, a reviewer and two autonomous loops,
plus the composite actions and scripts they share. Meridian repositories and
the public `meridianlabs-ai/inspect_ai` fork run them through thin stubs
pinned to this repository's `main`; the `meridianlabs-ai/actions` repository
builds two more agent workflows on the same composites.

## Threat model

The trust boundaries, the guarantees and what is by design, not a finding,
are in [THREAT_MODEL.md](THREAT_MODEL.md).
