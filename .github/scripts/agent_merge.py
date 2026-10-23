#!/usr/bin/env python3
"""Merge ready pull requests as the Orbit agent App (orbit-harness[bot]).

Agents merge their own PRs, but a coding agent is woken only by failures and
comments, so a PR that passes everything could wait for a human to say "merge".
This script is the server-side merge: `.github/workflows/agent-merge.yml` runs
it from the default branch on check, status, review and PR events, and
orbit-infrastructure's `agent-merge-sweep.yml` runs it with `--sweep` every 15
minutes for every repo that has the workflow.

It squash-merges a PR, at exactly the head commit it checked, when:
  - the PR is open, targets the default branch, is not a draft and has no
    `hold` label;
  - `agent-policy` is success on the head;
  - every check on the head passed (the latest run of each name; a cancelled
    or skipped run is no result) and no commit status is pending or failing;
  - Greptile finished a review of the head commit (its "Greptile Review" check
    completed, or it posted a review for that commit);
  - no review thread is unresolved;
  - GitHub reports the PR as mergeable (CLEAN or HAS_HOOKS; UNSTABLE only when
    the checks above found nothing pending or failing, because a cancelled run
    alone makes GitHub report UNSTABLE).
A PR that is BEHIND waits: the bot never authors commits, so the human's login
updates the branch. PRs opened before the bot existed, with no push since,
are left to their authors.

Stdlib only. It never runs code from the pull request.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

POLICY_CONTEXT = "agent-policy"
OWN_CHECK = "agent-merge"
WORKFLOW_PATH = ".github/workflows/agent-merge.yml"
HOLD_LABEL = "hold"
GREPTILE_CHECK = "Greptile Review"
GREPTILE_APP = "greptile-apps"
GREPTILE_LOGINS = {"greptile-apps", "greptile-apps[bot]"}
# PRs opened before the bot shipped merge only after a new push.
ELIGIBLE_SINCE = "2026-10-23T00:00:00Z"
PASSED = {"success", "neutral", "skipped"}
NO_RESULT = {"cancelled", "stale"}
READY_STATES = {"CLEAN", "HAS_HOOKS"}

PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    pullRequest(number: $number) {
      number state isDraft mergeStateStatus baseRefName headRefOid createdAt
      labels(first: 100) { nodes { name } }
      reviews(last: 100) { nodes { author { login } commit { oid } state } }
      commits(last: 1) { nodes { commit { oid committedDate } } }
      reviewThreads(first: 100, after: $cursor) {
        nodes { isResolved }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


@dataclass
class PullState:
    """Everything the merge decision reads about one PR, at one head commit."""

    number: int
    state: str
    is_draft: bool
    merge_state: str
    base: str
    default_branch: str
    head: str
    created_at: str
    head_committed_at: str
    labels: list[str] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)
    unresolved_threads: int = 0
    check_runs: list[dict] = field(default_factory=list)
    statuses: list[dict] = field(default_factory=list)


@dataclass
class Decision:
    merge: bool
    reason: str


def latest_check_runs(check_runs: list[dict]) -> dict[tuple[str, str], dict]:
    """The newest run of each (app, name) pair; a re-run replaces the older result."""
    latest: dict[tuple[str, str], dict] = {}
    for run in check_runs:
        key = (((run.get("app") or {}).get("slug") or ""), run.get("name") or "")
        stamp = (run.get("started_at") or "", run.get("id") or 0)
        current = latest.get(key)
        if current is None or stamp > (current.get("started_at") or "", current.get("id") or 0):
            latest[key] = run
    return latest


def check_problems(check_runs: list[dict], statuses: list[dict]) -> list[str]:
    """Checks and statuses on the head that are pending or did not pass."""
    problems = []
    for (_, name), run in sorted(latest_check_runs(check_runs).items()):
        if name == OWN_CHECK:
            continue
        if run.get("status") != "completed":
            problems.append(f"{name} is {run.get('status') or 'pending'}")
        elif run.get("conclusion") not in PASSED | NO_RESULT:
            problems.append(f"{name} is {run.get('conclusion')}")
    for status in statuses:
        if status.get("context") != POLICY_CONTEXT and status.get("state") != "success":
            problems.append(f"{status.get('context')} is {status.get('state')}")
    return problems


def greptile_reviewed(pr: PullState) -> bool:
    """True when Greptile finished a review of the head commit."""
    for run in pr.check_runs:
        app = (run.get("app") or {}).get("slug")
        if run.get("name") == GREPTILE_CHECK and app == GREPTILE_APP and run.get("status") == "completed":
            return True
    return any(
        ((review.get("author") or {}).get("login") or "").lower() in GREPTILE_LOGINS
        and ((review.get("commit") or {}).get("oid") or "") == pr.head
        for review in pr.reviews
    )


def decide(pr: PullState) -> Decision:
    """Merge or wait, with the first reason that blocks the merge."""
    if pr.state != "OPEN":
        return Decision(False, f"state is {pr.state.lower()}")
    if pr.base != pr.default_branch:
        return Decision(False, f"targets {pr.base}, not {pr.default_branch}")
    if pr.is_draft:
        return Decision(False, "draft")
    if any(label.lower() == HOLD_LABEL for label in pr.labels):
        return Decision(False, f"has the {HOLD_LABEL} label")
    if max(pr.created_at, pr.head_committed_at) < ELIGIBLE_SINCE:
        return Decision(False, f"opened and last pushed before {ELIGIBLE_SINCE}")
    policy = [status for status in pr.statuses if status.get("context") == POLICY_CONTEXT]
    if not policy or policy[0].get("state") != "success":
        state = policy[0].get("state") if policy else "missing"
        return Decision(False, f"{POLICY_CONTEXT} is {state}")
    problems = check_problems(pr.check_runs, pr.statuses)
    if problems:
        return Decision(False, "; ".join(problems[:5]))
    if not greptile_reviewed(pr):
        return Decision(False, "Greptile has not finished a review of the head commit")
    if pr.unresolved_threads:
        return Decision(False, f"{pr.unresolved_threads} unresolved review thread(s)")
    if pr.merge_state == "BEHIND":
        return Decision(False, "behind its base; the human's login updates the branch")
    # UNSTABLE here can only mean a cancelled run or this job's own run: the checks above passed.
    if pr.merge_state not in READY_STATES | {"UNSTABLE"}:
        return Decision(False, f"GitHub reports merge state {pr.merge_state}")
    return Decision(True, f"ready at {pr.head[:12]}")


class GitHub:
    def __init__(self, token: str) -> None:
        """Keep the token for API calls."""
        self.token = token

    def request(self, method: str, path: str, payload: object | None = None) -> object:
        """Call the GitHub API; reads retry network errors and 5xx twice."""
        url = path if path.startswith("https://") else f"https://api.github.com{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read() or b"null")
            except urllib.error.URLError as error:
                # A GraphQL read is a POST too; every other POST or PUT is final.
                read = method == "GET" or path == "/graphql"
                retryable = not isinstance(error, urllib.error.HTTPError) or error.code >= 500
                if not read or not retryable or attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")

    def paginate(self, path: str, key: str | None = None) -> list[dict]:
        """Read up to 10 pages of a list endpoint (`key` names the list in an object answer)."""
        items: list[dict] = []
        for page in range(1, 11):
            sep = "&" if "?" in path else "?"
            answer = self.request("GET", f"{path}{sep}per_page=100&page={page}")
            batch = (answer or {}).get(key, []) if key else answer
            if not isinstance(batch, list) or not batch:
                break
            items += batch
            if len(batch) < 100:
                break
        return items

    def graphql(self, query: str, variables: dict) -> dict:
        """Run a GraphQL query and return its data; errors raise."""
        answer = self.request("POST", "/graphql", {"query": query, "variables": variables})
        if not isinstance(answer, dict) or answer.get("errors"):
            raise RuntimeError(f"GraphQL error: {answer.get('errors') if isinstance(answer, dict) else answer}")
        return answer["data"]


def read_pull(github: GitHub, repo: str, number: int) -> PullState | None:
    """Read one PR's state; None when it does not exist. Waits briefly while GitHub computes mergeability."""
    owner, name = repo.split("/", 1)
    for attempt in range(4):
        threads = 0
        cursor = None
        while True:
            data = github.graphql(PR_QUERY, {"owner": owner, "name": name, "number": number, "cursor": cursor})
            node = (data.get("repository") or {}).get("pullRequest")
            if node is None:
                return None
            page = node["reviewThreads"]
            threads += sum(1 for thread in page["nodes"] if not thread["isResolved"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        if node["mergeStateStatus"] != "UNKNOWN" or attempt == 3:
            break
        time.sleep(3)
    head = node["headRefOid"]
    last = (node["commits"]["nodes"] or [{}])[0].get("commit") or {}
    statuses = github.paginate(f"/repos/{repo}/commits/{head}/status", key="statuses")
    check_runs = github.paginate(f"/repos/{repo}/commits/{head}/check-runs?filter=all", key="check_runs")
    return PullState(
        number=node["number"],
        state=node["state"],
        is_draft=node["isDraft"],
        merge_state=node["mergeStateStatus"],
        base=node["baseRefName"],
        default_branch=data["repository"]["defaultBranchRef"]["name"],
        head=head,
        created_at=node["createdAt"],
        head_committed_at=(last.get("committedDate") or "") if last.get("oid") == head else "",
        labels=[label["name"] for label in node["labels"]["nodes"]],
        reviews=node["reviews"]["nodes"],
        unresolved_threads=threads,
        check_runs=check_runs,
        statuses=statuses,
    )


def merge(github: GitHub, repo: str, pr: PullState) -> str:
    """Squash-merge at the checked head only; GitHub refuses if the head moved."""
    try:
        github.request(
            "PUT",
            f"/repos/{repo}/pulls/{pr.number}/merge",
            {"merge_method": "squash", "sha": pr.head},
        )
    except urllib.error.HTTPError as error:
        # 405: not mergeable now (a rule, or another run merged it); 409: the head moved.
        if error.code in (405, 409, 422):
            detail = json.loads(error.read() or b"{}").get("message", "")
            return f"not merged ({error.code}: {detail})"
        raise
    return "merged"


def open_pull_numbers(github: GitHub, repo: str) -> list[int]:
    """Every open PR in the repo."""
    return [pr["number"] for pr in github.paginate(f"/repos/{repo}/pulls?state=open")]


def pulls_at_commit(github: GitHub, repo: str, sha: str) -> list[int]:
    """Open PRs whose head is this commit."""
    if not sha:
        return []
    answer = github.request("GET", f"/repos/{repo}/commits/{sha}/pulls?per_page=100") or []
    return [
        item["number"] for item in answer if item.get("state") == "open" and (item.get("head") or {}).get("sha") == sha
    ]


def event_pull_numbers(github: GitHub, repo: str, event_name: str, event: dict) -> list[int]:
    """The PRs an event can have made ready."""
    if event_name in ("schedule", "workflow_dispatch"):
        wanted = str((event.get("inputs") or {}).get("pr") or "").strip()
        return [int(wanted)] if wanted else open_pull_numbers(github, repo)
    if event.get("pull_request"):
        return [event["pull_request"]["number"]]
    source = event.get("check_suite") or event.get("check_run") or event.get("workflow_run") or {}
    numbers = {pr["number"] for pr in source.get("pull_requests") or []}
    sha = source.get("head_sha") or event.get("sha") or ""
    return sorted(numbers | set(pulls_at_commit(github, repo, sha)))


def has_merge_workflow(github: GitHub, repo: dict) -> bool:
    """True when the repo's default branch has the agent-merge workflow."""
    path = urllib.parse.quote(WORKFLOW_PATH)
    try:
        github.request("GET", f"/repos/{repo['full_name']}/contents/{path}?ref={repo['default_branch']}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise
    return True


def handle(github: GitHub, repo: str, numbers: list[int], dry_run: bool) -> list[str]:
    """Decide, and merge when ready, each PR; one log line per PR."""
    lines = []
    for number in numbers:
        pr = read_pull(github, repo, number)
        if pr is None:
            continue
        decision = decide(pr)
        outcome = decision.reason
        if decision.merge:
            outcome = "dry run: would merge" if dry_run else merge(github, repo, pr)
            outcome = f"{outcome} ({decision.reason})"
        lines.append(f"{repo}#{number}: {'merge' if decision.merge else 'wait'}: {outcome}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sweep", action="store_true", help="every open PR in every repo with the workflow")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--pr", type=int, action="append", help="these PRs instead of the event's")
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("AGENT_MERGE_DRY_RUN") == "1")
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("agent-merge: GITHUB_TOKEN is not set", file=sys.stderr)
        return 2
    github = GitHub(token)

    lines: list[str] = []
    if args.sweep:
        repos = github.paginate("/installation/repositories", key="repositories")
        for repo in sorted(repos, key=lambda item: item["full_name"]):
            if repo.get("archived") or not has_merge_workflow(github, repo):
                continue
            try:
                lines += handle(github, repo["full_name"], open_pull_numbers(github, repo["full_name"]), args.dry_run)
            except (urllib.error.URLError, RuntimeError) as error:
                # One repo's API failure must not stop the sweep of the others.
                lines.append(f"{repo['full_name']}: error: {error}")
    else:
        if args.pr:
            numbers = args.pr
        else:
            with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle_file:
                event = json.load(handle_file)
            numbers = event_pull_numbers(github, args.repo, os.environ.get("GITHUB_EVENT_NAME", ""), event)
        lines = handle(github, args.repo, numbers, args.dry_run)

    report = "\n".join(lines) or "agent-merge: no open pull request to check"
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as out:
            out.write(f"```\n{report}\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
