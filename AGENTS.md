# Agent Instructions

Instructions for every agent (Codex, Claude, others) working in `orbit-mcp`. `CLAUDE.md` is a symlink to this file.

## Deploys and the release check

- A merge to `main` deploys both Cloud Run services (`cloudbuild.yaml`, trigger `orbit-mcp-deploy`) by
  digest, behind single flight, with a release record, a deploy check and a watch window
  (`docs/RELEASE_PIPELINE.md`). Agents never run the deploy trigger to ship; to ship, merge.
- `release/` is a pinned copy of `orbit-infrastructure/release-kit`; change it there and sync. This repo
  owns only `release/services/orbit-mcp.json`.

## Agent review and release policy

These rules bind every agent and every model (Codex, Claude, others). Agents
write, review, merge and verify their own changes. Humans approve only the
gates below.

### Setup (once per machine)

1. `gh auth status` shows your own GitHub login, and `git config user.name`
   is your real name. Commits, pushes and PRs are always yours.
2. `infisical login` (the shared Orbit project). The bot's key lives in
   Infisical prod `/orbit-agent`; you never need to read it.
3. Install the wrapper from a checkout of `orbit-search/orbit-infrastructure`:
   `mkdir -p ~/.local/bin && ln -sf "$PWD/scripts/orbit-agent-gh" ~/.local/bin/orbit-agent-gh`.
   Make sure `~/.local/bin` is on your `PATH`, then check `orbit-agent-gh whoami`.
4. Connect Slack, so your agent can send approval requests itself: in
   Claude, add the Slack connector (claude.ai Settings > Connectors); in Codex,
   add a Slack MCP server to `~/.codex/config.toml`.

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
  and the gated paths. The `agent-policy` commit status
  shows what needs a human for those paths. Gates without a path rule (an
  existing API contract, spend limits, taste) are your judgement: when your
  change touches one, ask for approval and record it. Ask for approval only
  for a gate:
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
  `agent-policy` passes. Rollbacks are started by a human: a release that
  fails its release check posts an alert with the rollback command
  (`docs/RELEASE_PIPELINE.md`).
- **Do not ask your human before merging.** A PR that passes every check
  merges; the merge bot does it on the server, and you may merge it yourself
  too. The bot (`.github/workflows/agent-merge.yml`) squash-merges, as
  `orbit-harness[bot]`, exactly the head commit it checked, when the PR is open
  and not a draft, `agent-policy` passes, every check passes, Greptile reviewed
  that commit and no review thread is open. It never updates a branch that is
  behind. To keep a ready PR open, mark it as a draft or add the `hold` label.
- **Ask for approval in Slack.** When `agent-policy` needs approval from
  someone who is not in your session (a gate, or owner clearance where your
  human is not an owner), send the request yourself as a Slack direct message
  through your human's Slack connector. Message only the people the check
  names (the `owners` in `.github/agent-policy.json`; Slack ids are in
  `release/release_people.json`), in one group DM per request, never in a
  channel post: the PR link, what needs approval and why, and how to answer
  (comment "ship it" on the PR, or reply in the DM and you record their
  words). Record the approval on the PR as usual. If no Slack connector is
  available, tell your human once to connect Slack, and continue other work.
- **Merge deploys.** A merge to `main` deploys (`docs/RELEASE_PIPELINE.md`).
  Agents never move deploy tags and never run the deploy triggers to ship; to
  ship, merge. Never cancel a deploy, rollback or release-check build. A build
  that already deploys still finishes: let it finish, then roll back as
  `docs/RELEASE_PIPELINE.md` says.
- **A human owns each change:** the person who started the agent.

