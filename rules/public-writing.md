# Public writing

Scope: these files are written for Meridian Labs employees and their agents. Anyone can read them. Nobody outside Meridian is expected to follow them, but we think they are reasonable, good practice.

These rules apply to text that other people read: PR titles and bodies, issues, comments, reviews, commit messages and chat posts.

## Required

### Style

- Write in ASD-STE100 Simplified Technical English.
- Write one topic in each sentence.
- Write instructions with 20 words or fewer. Write descriptions with 25 words or fewer.
- Write paragraphs with 6 sentences or fewer.
- Use the active voice.
- Use only the present, the simple past and the simple future tense.
- Write instructions in the imperative.
- Use one word for one meaning. Use the same term each time.
- Keep the articles ("a", "an", "the").
- Do not use idioms or slang.
- Keep code, file, flag and product names as they are, in backticks.
- Keep quotes from tools or errors exact, in code blocks.
- Be concise. Describe the change as it is now.

### Disclosure

- When an agent writes text that a human did not review, the text must say that an agent wrote it.
- Start the text with the agent's name and its owner, for example `Review Bot (Alex's bot):`.
- Do not post text under a person's name without approval from that person. A standing approval for a type of post is sufficient.
- Do not use emoji.

### References

- Refer to a PR or an issue by repo, number and a short description, for example `inspect_ai pr 1234 - fix log path`. Do not use a bare number.

### Automation triggers

- Do not write a token that starts a workflow (for example `@claude` or `@auto`) in a PR body or comment, unless you want to start that workflow.
- Do not quote the marker text that automation reads.

## Recommended

- Do not offer optional feedback with no clear action. Fix it, require it, file an issue that someone will do, or omit it.
- Do not ask a question when the reason is not stated or the answer no longer matters.
- Use "baseline" or "baselined", not "grandfathered".
- When the repo uses Conventional Commits, mark a breaking change with `!` and a `BREAKING CHANGE:` paragraph.
