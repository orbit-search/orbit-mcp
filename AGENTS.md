# Agent Instructions

Instructions for every agent (Codex, Claude, others) working in `orbit-mcp`. `CLAUDE.md` is a symlink to this file.

## Deploys and the release check

- A merge to `main` deploys a candidate revision of both Cloud Run services with no traffic
  (`cloudbuild.dev.yaml`, trigger `orbit-mcp-deploy-dev`), runs dev verify on the candidate tag URLs, and
  then the prod build (`cloudbuild.prod.yaml`, trigger `orbit-mcp-prod`) moves the traffic to exactly
  those revisions. A failed release rolls back on its own and prod promotes hold (`docs/RELEASE_PIPELINE.md`).
  Agents never run the deploy triggers to ship; to ship, merge.
- `release/` is a pinned copy of `orbit-infrastructure/release-kit`; change it there and sync. This repo
  owns only `release/services/orbit-mcp.json`.

## Agent review and release policy

These rules bind every agent and every model (Codex, Claude, others). Agents
write, review, merge and verify their own changes. Humans approve only the
gates below.

### Setup (once per machine)

Your human does two things once, both in a browser. Your agent does the rest.

**Your human:**

1. `infisical login` (the shared Orbit project). The bot's key lives in
   Infisical prod `/orbit-agent`; nobody reads it. The wrapper mints a
   one-hour token from it.
2. Connect Slack, so the agent can DM owners for approvals itself. Reuse a
   working Slack connector in your tool. If one is not available, use the
   per-tool setup below. Every tool uses Slack's MCP server (`https://mcp.slack.com/mcp`) and the person's own
   Slack login (OAuth). Each person signs in once in their own tool, because
   the agent sends approval DMs as that person (Slack gives each user their
   own token).
   - Claude app: sidebar > **Customize** > **Connectors** > **+** > **Slack**,
     then sign in. Claude Code sessions in the app use it too.
   - Claude Code CLI: `/plugin install slack` in a session (or
     `claude plugin install slack`), then sign in.
   - Codex app: **Settings** > **MCP servers** > **Add server**, name `slack`,
     **Streamable HTTP**, URL `https://mcp.slack.com/mcp`. **Save**,
     **Restart**, then **Authenticate**.
   - Codex CLI: add this to `~/.codex/config.toml`, then run
     `codex mcp login slack`. The CLI, the app and the IDE extension share
     this file.

     ```toml
     [mcp_servers.slack]
     url = "https://mcp.slack.com/mcp"
     auth = "oauth"

     [mcp_servers.slack.oauth]
     client_id = "<client ID of Orbit's Slack app>"
     ```

   Manual Codex MCP setup needs the `[mcp_servers.slack.oauth]` client ID:
   Slack's server accepts only a registered Slack app, not Codex's automatic
   client registration. Get the registered client ID from Nicholas Dominici
   or another Orbit Slack app owner. If that app has a client secret, run
   `codex mcp add slack --url https://mcp.slack.com/mcp --oauth-client-id <id> --oauth-client-secret <secret>`
   instead of editing the file.

**Your agent**, itself, never asking its human:

3. When `orbit-agent-gh` is missing, install the wrapper from a checkout of
   `orbit-search/orbit-infrastructure` (clone it first if there is none).
   Give the absolute path of the checkout; a relative path makes a broken link:
   `mkdir -p ~/.local/bin && ln -sf "<absolute path of the checkout>/scripts/orbit-agent-gh" ~/.local/bin/orbit-agent-gh`.
   Make sure `~/.local/bin` is on `PATH`, then check `orbit-agent-gh whoami`.
4. When `orbit-agent-gh` cannot mint a token because the Infisical login is
   missing or expired, ask the human for `infisical login` (a browser login),
   and only that.

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
  `agent-policy` passes. A release that fails its release check rolls back on
  its own; a human can also roll back (`docs/RELEASE_PIPELINE.md`).
- **When to ask your human.** Finish on your own when you can verify the goal
  yourself: bug fixes, backend features, optimizations, infra and CI changes
  get a goal you can check (tests, a replay, a metric), and they merge when
  every check passes. Ask one short question only at a gate (UI ready, taste,
  billing, an API contract, migrations, spend, owner clearance), when your
  human said to wait, or when no check can tell you the task is done. Never
  ask "may I merge?" on a change that passed every check.
- **Do not ask your human before merging.** A PR that passes every check
  merges; the merge bot does it on the server, and you may merge it yourself
  too. The bot (`agent-merge-sweep` in orbit-infrastructure, every 5 minutes,
  for repos with `"agentMerge": true` in `.github/agent-policy.json`)
  squash-merges, as `orbit-harness[bot]`, exactly the head commit it checked,
  when the PR is open and not a draft, `agent-policy` passes, every check
  passes, Greptile reviewed that commit and no review thread is open. It never
  updates a branch that is behind. To keep a ready PR open, mark it as a draft
  or add the `hold` label.
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

