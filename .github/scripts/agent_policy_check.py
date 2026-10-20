#!/usr/bin/env python3
"""Deterministic agent-policy check for pull requests.

Agents (any vendor or model) merge their own pull requests. Humans approve only
the changes in `.github/agent-policy.json`, in words, and the agent records that
approval as a PR comment. This script is the floor that no agent can skip: it
runs from the default branch, reads the policy from each PR's target branch,
reads the PR's files, body and comments from the GitHub API, and sets the
`agent-policy` commit status on the PR head.

Requirements it enforces:
  - gate:<id>        a changed path matches a gate in the policy (billing, ...)
  - gate:<contract>  the developer API contract snapshot changed an existing shape
  - owner-clearance  the author is not a repo owner and changed behavior paths
  - outside-author   the author has no write access to the repo
  - ui-media         a UI path changed and the PR body lacks desktop and mobile media
  - gate:ui          a UI path changed (an agent "ui-small" record clears a small change)
  - gate:large-pr    GitHub could not list every changed file, so the check fails closed

A requirement is met by an approval record from a repo owner for the head
commit, or for an earlier commit that carry records link to the head.

Stdlib only. It never runs code from the pull request.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

STATUS_CONTEXT = "agent-policy"
POLICY_PATH = ".github/agent-policy.json"
RECORD_MARKER = "<!-- agent-policy-record -->"
CARRY_MARKER = "<!-- agent-policy-carry -->"
MEMBER_ASSOCIATIONS = {"OWNER", "MEMBER"}
RECORD_AUTHOR_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
CONTRACT_METADATA_KEYS = {"description", "sourceFile"}
SHA_PATTERN = re.compile(r"\b[0-9a-f]{40}\b")
MEDIA_PATTERN = re.compile(
    r"!\[[^\]]*\]\([^)]+\)|<img\b|<video\b|github\.com/user-attachments/assets/[0-9a-f-]+",
    re.IGNORECASE,
)


@dataclass
class Record:
    """An approval record or a carry record posted as a PR comment."""

    kinds: set[str]
    approver: str
    commit: str
    carry_from: str = ""


@dataclass
class Requirement:
    kind: str
    reason: str
    met: bool = False


@dataclass
class Evaluation:
    requirements: list[Requirement] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(requirement.met for requirement in self.requirements)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-like glob: `**` crosses directories, `*` does not."""
    out = ""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            out += "(?:.*/)?"
            index += 3
            continue
        if pattern.startswith("**", index):
            out += ".*"
            index += 2
            continue
        out += "[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char)
        index += 1
    return re.compile(f"^{out}$")


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(glob_to_regex(pattern).match(path) for pattern in patterns)


def _field(body: str, name: str) -> str:
    # [ \t] and not \s: an empty value must not run into the next line.
    match = re.search(rf"^[ \t]*[-*]?[ \t]*{name}[ \t]*:[ \t]*(.*?)[ \t]*$", body, re.IGNORECASE | re.MULTILINE)
    return match.group(1) if match else ""


def parse_records(
    comments: list[dict], agent_logins: set[str] | None = None, insiders: set[str] | None = None
) -> list[Record]:
    """Read approval and carry records from comments by people with write access or the agent App.

    `insiders` holds the logins with write access to the repo; without it, the
    comment's author_association decides (GitHub reports a private org member
    to the Actions token as CONTRIBUTOR, so the checker passes insiders)."""
    records = []
    for comment in comments:
        body = comment.get("body") or ""
        login = ((comment.get("user") or {}).get("login") or "").lower()
        trusted = (
            login in insiders
            if insiders is not None
            else comment.get("author_association") in RECORD_AUTHOR_ASSOCIATIONS
        )
        if not trusted and login not in (agent_logins or set()):
            continue
        commit_match = SHA_PATTERN.search(_field(body, "Commit").lower())
        if not commit_match:
            continue
        if RECORD_MARKER in body:
            kinds = {kind.strip().lower() for kind in _field(body, "Kind").split(",") if kind.strip()}
            approver = _field(body, "Approved by").lstrip("@").split()[0] if _field(body, "Approved by") else ""
            # The evidence: what the owner said and where. A template placeholder is not evidence.
            evidence = [_field(body, "Words").strip("\"' "), _field(body, "Where")]
            if kinds and approver and all(value and not value.startswith("<") for value in evidence):
                records.append(Record(kinds=kinds, approver=approver.lower(), commit=commit_match.group(0)))
        elif CARRY_MARKER in body:
            from_match = SHA_PATTERN.search(_field(body, "From").lower())
            if from_match and _field(body, "Reason"):
                records.append(
                    Record(kinds=set(), approver="", commit=commit_match.group(0), carry_from=from_match.group(0))
                )
    return records


def approved_commits(head: str, records: list[Record]) -> set[str]:
    """The head plus every earlier commit that carry records link to the head."""
    commits = {head}
    changed = True
    while changed:
        changed = False
        for record in records:
            if record.carry_from and record.commit in commits and record.carry_from not in commits:
                commits.add(record.carry_from)
                changed = True
    return commits


def contract_change(base: object, head: object, field_map: bool = False) -> str:
    """Return "none", "additive" or "changed" for two contract snapshot values.

    `description` and `sourceFile` are snapshot metadata only outside a
    `properties` map; inside one they are real API field names.
    """
    if isinstance(base, dict) and isinstance(head, dict):
        skip = set() if field_map else CONTRACT_METADATA_KEYS
        result = "none"
        for key, base_value in base.items():
            if key in skip:
                continue
            if key not in head:
                return "changed"
            inner = contract_change(base_value, head[key], field_map=key == "properties")
            if inner == "changed":
                return "changed"
            if inner == "additive":
                result = "additive"
        if any(key not in base and key not in skip for key in head):
            result = "additive"
        return result
    if isinstance(base, list) and isinstance(head, list):
        # Lists hold union variants: compare them item by item. A new or a
        # removed variant changes what clients can receive.
        if len(base) != len(head):
            return "changed"
        results = [contract_change(b, h) for b, h in zip(base, head)]
        if "changed" in results:
            return "changed"
        return "additive" if "additive" in results else "none"
    return "none" if base == head else "changed"


def evaluate(
    policy: dict,
    pr: dict,
    files: list[dict],
    comments: list[dict],
    contract_changes: dict[str, str],
    insiders: set[str] | None = None,
) -> Evaluation:
    """Decide which requirements apply to the PR and which records meet them."""
    paths = [entry["filename"] for entry in files] + [
        entry["previous_filename"] for entry in files if entry.get("previous_filename")
    ]
    owners = {owner.lower() for owner in policy.get("owners", [])}
    author = (pr.get("user") or {}).get("login", "")
    evaluation = Evaluation()

    for gate in policy.get("gates", []):
        hits = [path for path in paths if matches_any(path, gate.get("paths", []))]
        if hits:
            evaluation.requirements.append(
                Requirement(f"gate:{gate['id']}", f"{gate['reason']} ({', '.join(sorted(hits)[:3])})")
            )
        snapshot = gate.get("contractSnapshot")
        if snapshot and contract_changes.get(snapshot) == "changed":
            evaluation.requirements.append(
                Requirement(f"gate:{gate['id']}", f"{gate['reason']} ({snapshot} changed an existing shape)")
            )

    if author.lower() not in owners:
        behavior = [path for path in paths if not matches_any(path, policy.get("noBehaviorPaths", []))]
        if behavior:
            evaluation.requirements.append(
                Requirement(
                    "owner-clearance",
                    f"@{author} is not an owner of this repo and changed {', '.join(sorted(behavior)[:3])}",
                )
            )
    listed = len(files)
    if pr.get("changed_files") and listed < pr["changed_files"]:
        # The files API lists at most 3,000 files: never decide on a partial list.
        evaluation.requirements.append(
            Requirement(
                "gate:large-pr", f"GitHub listed {listed} of {pr['changed_files']} changed files; an owner reviews it"
            )
        )
    outside = (
        author.lower() not in insiders
        if insiders is not None
        else pr.get("author_association") not in MEMBER_ASSOCIATIONS
    )
    if outside:
        evaluation.requirements.append(Requirement("outside-author", f"@{author} has no write access to this repo"))

    ui_hits = [path for path in paths if matches_any(path, policy.get("uiPaths", []))]
    body = pr.get("body") or ""
    if ui_hits:
        media = len(MEDIA_PATTERN.findall(body))
        has_media = media >= 2 and re.search(r"desktop", body, re.I) and re.search(r"mobile", body, re.I)
        evaluation.requirements.append(
            Requirement(
                "ui-media",
                "UI paths changed; the PR body needs desktop and mobile screenshots (and recordings for animation)",
                met=bool(has_media),
            )
        )
        evaluation.requirements.append(
            Requirement("gate:ui", "UI change; a human approves from the media unless the change is small")
        )

    records = parse_records(comments, {login.lower() for login in policy.get("agentLogins", [])}, insiders)
    commits = approved_commits(pr["head"]["sha"], records)
    for requirement in evaluation.requirements:
        if requirement.kind == "ui-media":
            continue
        requirement.met = any(
            record.commit in commits
            and (
                (requirement.kind in record.kinds and record.approver in owners)
                # A small UI change (same layout, flow and style) is Auto: the
                # agent records "ui-small" with its reason instead of an owner.
                or (requirement.kind == "gate:ui" and "ui-small" in record.kinds)
            )
            for record in records
        )
    return evaluation


def describe(evaluation: Evaluation) -> str:
    if not evaluation.requirements:
        return "No human approval needed"
    missing = [requirement.kind for requirement in evaluation.requirements if not requirement.met]
    if not missing:
        return "All approvals recorded: " + ", ".join(requirement.kind for requirement in evaluation.requirements)
    return "Needs: " + ", ".join(missing)


def summary_markdown(evaluation: Evaluation, head: str) -> str:
    lines = [f"## agent-policy: {describe(evaluation)}", ""]
    for requirement in evaluation.requirements:
        lines.append(f"- {'met' if requirement.met else 'MISSING'} `{requirement.kind}`: {requirement.reason}")
    missing = [r.kind for r in evaluation.requirements if not r.met and r.kind != "ui-media"]
    if missing:
        lines += [
            "",
            "Record the owner's words in a PR comment (implied approval is valid):",
            "",
            "```",
            RECORD_MARKER,
            "**Approval record**",
            f"- Kind: {', '.join(missing)}",
            "- Approved by: @<owner login>",
            '- Words: "<what the owner said>"',
            "- Where: <agent session | PR comment | Slack>",
            f"- Commit: {head}",
            "```",
            "",
            "A later commit that only fixes CI or a review finding keeps the approval with a carry record:",
            "",
            "```",
            CARRY_MARKER,
            "- From: <approved commit>",
            "- Commit: <new head commit>",
            "- Reason: fix-only (CI or review finding)",
            "```",
            "",
            "A small UI change (same layout, flow and style) needs no owner: record `Kind: ui-small` with the reason in `Words`.",
        ]
    return "\n".join(lines) + "\n"


class GitHub:
    def __init__(self, repo: str, token: str) -> None:
        self.repo = repo
        self.token = token

    def request(self, method: str, path: str, payload: object | None = None, raw: bool = False) -> object:
        url = path if path.startswith("https://") else f"https://api.github.com/repos/{self.repo}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read()
                return body.decode() if raw else json.loads(body or b"null")
            except urllib.error.URLError as error:
                # Retry reads on network errors and 5xx; a 4xx answer is final.
                retryable = not isinstance(error, urllib.error.HTTPError) or error.code >= 500
                if method != "GET" or not retryable or attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")

    def paginate(self, path: str) -> list[dict]:
        items: list[dict] = []
        for page in range(1, 31):
            sep = "&" if "?" in path else "?"
            batch = self.request("GET", f"{path}{sep}per_page=100&page={page}")
            if not isinstance(batch, list) or not batch:
                break
            items += batch
            if len(batch) < 100:
                break
        return items

    def has_write_access(self, login: str) -> bool:
        try:
            answer = self.request("GET", f"/collaborators/{urllib.parse.quote(login)}/permission")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return False
            raise
        return answer.get("permission") in {"admin", "maintain", "write"}

    def file_at(self, path: str, ref: str) -> object | None:
        try:
            text = self.request("GET", f"/contents/{urllib.parse.quote(path)}?ref={ref}", raw=True)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
        return json.loads(str(text))


def contract_changes_for(github: GitHub, policy: dict, pr: dict, files: list[dict]) -> dict[str, str]:
    changes = {}
    changed = {entry["filename"]: entry for entry in files}
    for gate in policy.get("gates", []):
        snapshot = gate.get("contractSnapshot")
        if snapshot and snapshot in changed:
            if changed[snapshot].get("status") in {"removed", "renamed"}:
                changes[snapshot] = "changed"
            else:
                base = github.file_at(snapshot, pr["base"]["sha"])
                head = github.file_at(snapshot, pr["head"]["sha"])
                changes[snapshot] = "additive" if base is None else contract_change(base, head)
    return changes


def evaluate_pr(github: GitHub, policy: dict, pr: dict) -> Evaluation:
    # Each PR follows the policy of its own target branch (merged, so trusted);
    # the default branch's copy applies when that branch has none.
    policy = github.file_at(POLICY_PATH, pr["base"]["sha"]) or policy
    files = github.paginate(f"/pulls/{pr['number']}/files")
    comments = github.paginate(f"/issues/{pr['number']}/comments")
    # Write access decides who is inside the company; author_association hides private org members.
    marked = [c for c in comments if RECORD_MARKER in (c.get("body") or "") or CARRY_MARKER in (c.get("body") or "")]
    logins = {pr["user"]["login"]} | {(c.get("user") or {}).get("login") for c in marked}
    logins.discard(None)
    insiders = {login.lower() for login in logins if github.has_write_access(login)}
    return evaluate(policy, pr, files, comments, contract_changes_for(github, policy, pr, files), insiders)


def open_prs_at(github: GitHub, head: str, known: dict | None = None) -> list[dict]:
    """Every open PR whose head is this commit; one commit status covers all of them."""
    associated = github.request("GET", f"/commits/{head}/pulls")
    numbers = {
        item["number"]
        for item in associated or []
        if item.get("state") == "open" and (item.get("head") or {}).get("sha") == head
    }
    if known and known.get("state") == "open" and known["head"]["sha"] == head:
        numbers.add(known["number"])
    prs = [
        known if known and number == known["number"] else github.request("GET", f"/pulls/{number}")
        for number in sorted(numbers)
    ]
    # Re-read state: the commit-to-PR index can lag a close or a push.
    return [pr for pr in prs if pr.get("state") == "open" and pr["head"]["sha"] == head]


def set_status(github: GitHub, head: str, state: str, description: str) -> None:
    repo = github.repo
    run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    github.request(
        "POST",
        f"/statuses/{head}",
        {"state": state, "context": STATUS_CONTEXT, "description": description[:140], "target_url": run_url},
    )


def publish(github: GitHub, policy: dict, head: str, list_prs: Callable[[], list[dict]]) -> dict[int, Evaluation]:
    """Evaluate every open PR at `head` and set one combined status on it."""
    # Pending first, before any read: a recheck that cannot finish must not
    # leave an earlier success in place.
    set_status(github, head, "pending", "Evaluating the agent policy")
    try:
        prs = list_prs()
        results = [(item, evaluate_pr(github, policy, item)) for item in prs]
    except Exception:
        set_status(github, head, "error", "agent-policy could not finish; rerun the agent-policy workflow")
        raise
    if not results:
        set_status(github, head, "success", "No open pull request on this commit")
        return {}

    summary = "\n".join(
        f"### PR #{item['number']} into {item['base']['ref']}\n\n" + summary_markdown(evaluation, head)
        for item, evaluation in results
    )
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(summary)

    passed = all(evaluation.passed for _, evaluation in results)
    if len(results) == 1:
        description = describe(results[0][1])
    else:
        description = "; ".join(f"#{item['number']}: {describe(evaluation)}" for item, evaluation in results)
    set_status(github, head, "success" if passed else "failure", description)
    return {item["number"]: evaluation for item, evaluation in results}


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    event = json.load(open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8"))
    github = GitHub(repo, os.environ["GITHUB_TOKEN"])
    number = (event.get("pull_request") or event.get("issue") or {}).get("number")
    if not number:
        print("No pull request in this event")
        return 0
    policy = json.load(open(os.environ.get("AGENT_POLICY_PATH", POLICY_PATH), encoding="utf-8"))
    # Mark the event's head pending before the first read, so a failed read
    # cannot leave an earlier success on it.
    event_head = ((event.get("pull_request") or {}).get("head") or {}).get("sha")
    if event_head and event.get("action") != "closed":
        set_status(github, event_head, "pending", "Evaluating the agent policy")
    try:
        pr = github.request("GET", f"/pulls/{number}")
    except Exception:
        if event_head and event.get("action") != "closed":
            set_status(github, event_head, "error", "agent-policy could not finish; rerun the agent-policy workflow")
        raise

    # A head this PR closed on or moved away from may still carry a combined
    # status for other open PRs: check those PRs again without this one.
    left_heads = []
    if pr.get("state") != "open":
        left_heads.append(pr["head"]["sha"])
    if event.get("action") == "synchronize" and event.get("before"):
        left_heads.append(event["before"])
    for left in left_heads:
        publish(github, policy, left, lambda left=left: open_prs_at(github, left))
    if pr.get("state") != "open":
        print(f"PR #{number} is {pr.get('state')}; rechecked the PRs left on its head")
        return 0

    head = pr["head"]["sha"]
    results = publish(github, policy, head, lambda: open_prs_at(github, head, known=pr))
    own = results[pr["number"]]
    needs_owner = any(r.kind == "owner-clearance" and not r.met for r in own.requirements)
    if needs_owner and os.environ.get("GITHUB_EVENT_NAME", "").startswith("pull_request"):
        author = pr["user"]["login"].lower()
        reviewers = [owner for owner in policy.get("owners", []) if owner.lower() != author]
        try:
            github.request("POST", f"/issues/{number}/labels", {"labels": ["needs-owner"]})
            if reviewers:
                github.request("POST", f"/pulls/{number}/requested_reviewers", {"reviewers": reviewers})
        except urllib.error.HTTPError as error:
            print(f"Could not label or request owners: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
