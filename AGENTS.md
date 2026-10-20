# Agent Instructions

Instructions for every agent (Codex, Claude, others) working in `orbit-mcp`. `CLAUDE.md` is a symlink to this file.

## Agent review and release policy

These rules bind every agent and every model (Codex, Claude, others). Agents
write, review, merge and verify their own changes. Humans approve only the
gates below.

### Setup (once per machine)

1. `gh auth status` shows your own GitHub login, and `git config user.name`
   is your real name. Commits, pushes and PRs are always yours.
2. `infisical login` (the shared Orbit project). The bot's key lives in
   Infisical prod `/orbit-agent`; you never need to read it.
3. Install the wrapper from `orbit-search/orbit-infrastructure`:
   `ln -s "$PWD/scripts/orbit-agent-gh" ~/.local/bin/orbit-agent-gh` (from a
   checkout of that repo), then check `orbit-agent-gh whoami`.

### Rules

- **The human authors; the Orbit agent App comments and merges.** Commits,
  pushes and PRs use your own git and `gh` login. Review comments, replies,
  thread resolution, reviews and merges go through `orbit-agent-gh gh ...`, so
  they show as `orbit-harness[bot]`. Never commit, push or open a PR as the bot.
- **Proof, not claims.** The PR body follows `.github/pull_request_template.md`
  and shows the commands you ran and their output.
- **Review.** Greptile reviews every PR and reads `REVIEW.md`. Answer every
  finding: fix it, or close the thread with a reason. Greptile is the only
  outside reviewer; ask a new Codex or Claude thread for a second opinion.
- **Gates.** `.github/agent-policy.json` lists the owners
  (NicholasDominici, Vitor-Bukovitz) and the gated paths. The `agent-policy` commit status
  shows what needs a human. Ask for approval only for a gate:
  - billing (how we charge, credits, prices, refunds)
  - a change to an existing developer API field, type, error or behavior
  - CockroachDB migrations
  - provider spend limits, or a provider turned off
  - taste: how users see profile content
  - a PR by an author who is not an owner and changes behavior (owner
    clearance), or by someone outside the company
- **Implied approval is valid.** Never ask "do I have your explicit consent to
  merge?". When the owner has seen the brief or the media and their words show
  they want it shipped, record it and merge. A question, pushback or a change
  request is not approval.
- **Record the approval** as a PR comment (through `orbit-agent-gh`) in the
  format that the `agent-policy` check prints. A later commit that only fixes
  CI or a review finding keeps the approval with a carry record.
- **Merge** when CI is green, every review thread is resolved and
  `agent-policy` passes. Rollbacks are always started by a human.
- **A human owns each change:** the person who started the agent.

