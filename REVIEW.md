# Review guide (orbit-mcp)

Every review agent reads this file: Greptile, and any Codex or Claude review.

## What failure looks like here

- **Wrong result.** The change computes, stores or returns the wrong data, or the wrong person's data.
- **Data safety.** A destructive write, an unbounded query or a missing tenant or auth check.
- **Secrets.** A key, token or credential in code, logs or error messages.
- **Runaway cost.** Unbounded loops, fan-out or provider calls; cache keys without a TTL.

## How to report

- Start the summary with a count: "2 Important, 1 Nit" or "No issues found".
- Give a file and a line as evidence for each finding. Do not guess from a name.
- Label each finding: **Important**, **Nit** or **Pre-existing** (the problem was there before this PR).
- Post at most 3 Nits. Nits never block a merge.
- Name the type of bug, so the author fixes every instance, not one line.
- After three review rounds, review only the new commits. Do not repeat a finding that is already posted.
- Say when a change needs a human: billing, the existing developer API contract, provider spend limits or a provider turned off, taste, or UI.

## Skip

- Lockfiles and generated files.
