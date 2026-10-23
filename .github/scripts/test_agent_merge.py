"""Unit tests for .github/scripts/agent_merge.py, the server-side merge bot."""

import io
import json
import sys
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path

# The script lives in .github/scripts/ of the repo that holds this test.
_ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".github/scripts/agent_merge.py").exists())
sys.path.insert(0, str(_ROOT / ".github/scripts"))

import agent_merge as bot  # noqa: E402

HEAD = "a" * 40
OLD = "b" * 40


def run(name, conclusion="success", status="completed", app="github-actions", started="2026-10-23T01:00:00Z", run_id=1):
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "app": {"slug": app},
        "started_at": started,
    }


GREPTILE_RUN = run("Greptile Review", app="greptile-apps", run_id=50)


def ready(**changes):
    """A PR that passes every condition; each test changes one thing."""
    pr = bot.PullState(
        number=7,
        state="OPEN",
        is_draft=False,
        merge_state="CLEAN",
        base="main",
        default_branch="main",
        head=HEAD,
        created_at="2026-10-23T02:00:00Z",
        head_committed_at="2026-10-23T02:00:00Z",
        labels=["agent"],
        reviews=[],
        unresolved_threads=0,
        check_runs=[run("test"), run("lint", run_id=2), GREPTILE_RUN],
        statuses=[{"context": "agent-policy", "state": "success"}, {"context": "CodeRabbit", "state": "success"}],
    )
    return replace(pr, **changes)


class DecideTest(unittest.TestCase):
    def assertWaits(self, pr, reason_part):
        decision = bot.decide(pr)
        self.assertFalse(decision.merge, decision.reason)
        self.assertIn(reason_part, decision.reason)

    def test_ready_pr_merges(self):
        decision = bot.decide(ready())
        self.assertTrue(decision.merge, decision.reason)

    def test_has_hooks_merges(self):
        self.assertTrue(bot.decide(ready(merge_state="HAS_HOOKS")).merge)

    def test_closed_pr_waits(self):
        self.assertWaits(ready(state="MERGED"), "merged")

    def test_draft_waits(self):
        self.assertWaits(ready(is_draft=True), "draft")

    def test_hold_label_waits_in_any_case(self):
        self.assertWaits(ready(labels=["Hold"]), "hold")

    def test_other_base_waits(self):
        self.assertWaits(ready(base="feature/stack"), "targets feature/stack")

    def test_agent_policy_failure_waits(self):
        pr = ready(statuses=[{"context": "agent-policy", "state": "failure"}])
        self.assertWaits(pr, "agent-policy is failure")

    def test_agent_policy_missing_waits(self):
        self.assertWaits(ready(statuses=[]), "agent-policy is missing")

    def test_failed_check_waits(self):
        pr = ready(check_runs=[run("test", "failure"), GREPTILE_RUN])
        self.assertWaits(pr, "test is failure")

    def test_pending_check_waits(self):
        pr = ready(check_runs=[run("test", status="in_progress"), GREPTILE_RUN])
        self.assertWaits(pr, "test is in_progress")

    def test_pending_status_waits(self):
        statuses = [{"context": "agent-policy", "state": "success"}, {"context": "vercel", "state": "pending"}]
        self.assertWaits(ready(statuses=statuses), "vercel is pending")

    def test_rerun_replaces_older_failure(self):
        runs = [
            run("test", "failure", started="2026-10-23T01:00:00Z"),
            run("test", started="2026-10-23T01:10:00Z", run_id=3),
            GREPTILE_RUN,
        ]
        self.assertTrue(bot.decide(ready(check_runs=runs)).merge)

    def test_cancelled_run_is_no_result_and_unstable_merges(self):
        # A newer run in a concurrency group cancels the pending one; GitHub then says UNSTABLE.
        runs = [run("test"), run("evaluate", "cancelled", run_id=4), GREPTILE_RUN]
        self.assertTrue(bot.decide(ready(check_runs=runs, merge_state="UNSTABLE")).merge)

    def test_own_running_job_is_ignored(self):
        runs = [run("test"), run("agent-merge", status="in_progress", run_id=5), GREPTILE_RUN]
        self.assertTrue(bot.decide(ready(check_runs=runs, merge_state="UNSTABLE")).merge)

    def test_no_greptile_waits(self):
        self.assertWaits(ready(check_runs=[run("test")]), "Greptile")

    def test_greptile_check_running_waits(self):
        runs = [run("test"), run("Greptile Review", app="greptile-apps", status="in_progress", run_id=6)]
        self.assertWaits(ready(check_runs=runs), "Greptile Review is in_progress")

    def test_greptile_check_from_another_app_does_not_count(self):
        self.assertWaits(ready(check_runs=[run("test"), run("Greptile Review", run_id=7)]), "Greptile")

    def test_greptile_review_of_head_counts(self):
        reviews = [{"author": {"login": "greptile-apps"}, "commit": {"oid": HEAD}, "state": "COMMENTED"}]
        self.assertTrue(bot.decide(ready(check_runs=[run("test")], reviews=reviews)).merge)

    def test_greptile_review_of_older_commit_does_not_count(self):
        reviews = [{"author": {"login": "greptile-apps"}, "commit": {"oid": OLD}, "state": "COMMENTED"}]
        self.assertWaits(ready(check_runs=[run("test")], reviews=reviews), "Greptile")

    def test_unresolved_thread_waits(self):
        self.assertWaits(ready(unresolved_threads=2), "2 unresolved")

    def test_behind_waits_for_the_human(self):
        self.assertWaits(ready(merge_state="BEHIND"), "human's login updates")

    def test_blocked_dirty_and_unknown_wait(self):
        for state in ("BLOCKED", "DIRTY", "UNKNOWN", "DRAFT"):
            self.assertWaits(ready(merge_state=state), state)

    def test_old_pr_without_new_push_waits(self):
        pr = ready(created_at="2026-03-01T00:00:00Z", head_committed_at="2026-03-02T00:00:00Z")
        self.assertWaits(pr, "before")

    def test_old_pr_with_new_push_merges(self):
        pr = ready(created_at="2026-03-01T00:00:00Z", head_committed_at="2026-10-23T05:00:00Z")
        self.assertTrue(bot.decide(pr).merge)


class FakeGitHub:
    """Answers the calls that read_pull, merge and event_pull_numbers make."""

    def __init__(self, node, merge_error=None, commit_pulls=None):
        self.node = node
        self.merge_error = merge_error
        self.commit_pulls = commit_pulls or []
        self.calls = []

    def graphql(self, query, variables):
        self.calls.append(("graphql", variables["number"]))
        return {"repository": {"defaultBranchRef": {"name": "main"}, "pullRequest": self.node}}

    def paginate(self, path, key=None):
        self.calls.append(("GET", path))
        if "/status" in path:
            return [{"context": "agent-policy", "state": "success"}]
        if "/check-runs" in path:
            return [run("test"), GREPTILE_RUN]
        return [{"number": 7}, {"number": 8}]

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "PUT" and self.merge_error:
            raise self.merge_error
        if path.endswith("/pulls?per_page=100"):
            return self.commit_pulls
        return {"merged": True}


def node(**changes):
    value = {
        "number": 7,
        "state": "OPEN",
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "baseRefName": "main",
        "headRefOid": HEAD,
        "createdAt": "2026-10-23T02:00:00Z",
        "labels": {"nodes": []},
        "reviews": {"nodes": []},
        "commits": {"nodes": [{"commit": {"oid": HEAD, "committedDate": "2026-10-23T02:00:00Z"}}]},
        "reviewThreads": {
            "nodes": [{"isResolved": True}, {"isResolved": False}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    }
    value.update(changes)
    return value


class GitHubFlowTest(unittest.TestCase):
    def test_read_pull_counts_unresolved_threads(self):
        pr = bot.read_pull(FakeGitHub(node()), "orbit-search/x", 7)
        self.assertEqual(pr.unresolved_threads, 1)
        self.assertEqual(pr.default_branch, "main")
        self.assertEqual(pr.head_committed_at, "2026-10-23T02:00:00Z")

    def test_merge_squashes_at_the_checked_head(self):
        github = FakeGitHub(node())
        pr = bot.read_pull(github, "orbit-search/x", 7)
        self.assertEqual(bot.merge(github, "orbit-search/x", pr), "merged")
        self.assertIn(
            ("PUT", "/repos/orbit-search/x/pulls/7/merge", {"merge_method": "squash", "sha": HEAD}), github.calls
        )

    def test_moved_head_is_not_an_error(self):
        error = urllib.error.HTTPError(
            "u", 409, "conflict", {}, io.BytesIO(json.dumps({"message": "Head branch was modified"}).encode())
        )
        github = FakeGitHub(node(), merge_error=error)
        pr = bot.read_pull(github, "orbit-search/x", 7)
        self.assertIn("409", bot.merge(github, "orbit-search/x", pr))

    def test_handle_waits_on_unresolved_thread_and_never_merges(self):
        github = FakeGitHub(node())
        lines = bot.handle(github, "orbit-search/x", [7], dry_run=False)
        self.assertIn("wait: 1 unresolved", lines[0])
        self.assertFalse([call for call in github.calls if call[0] == "PUT"])

    def test_handle_dry_run_never_merges(self):
        github = FakeGitHub(node(reviewThreads={"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}))
        lines = bot.handle(github, "orbit-search/x", [7], dry_run=True)
        self.assertIn("dry run: would merge", lines[0])
        self.assertFalse([call for call in github.calls if call[0] == "PUT"])

    def test_events_map_to_pull_requests(self):
        github = FakeGitHub(
            node(),
            commit_pulls=[
                {"number": 9, "state": "open", "head": {"sha": HEAD}},
                {"number": 10, "state": "closed", "head": {"sha": HEAD}},
                {"number": 11, "state": "open", "head": {"sha": OLD}},
            ],
        )
        self.assertEqual(
            bot.event_pull_numbers(github, "o/r", "pull_request_review", {"pull_request": {"number": 3}}), [3]
        )
        self.assertEqual(bot.event_pull_numbers(github, "o/r", "status", {"sha": HEAD}), [9])
        suite = {"check_suite": {"head_sha": HEAD, "pull_requests": [{"number": 4}]}}
        self.assertEqual(bot.event_pull_numbers(github, "o/r", "check_suite", suite), [4, 9])
        self.assertEqual(bot.event_pull_numbers(github, "o/r", "workflow_dispatch", {"inputs": {"pr": "12"}}), [12])
        self.assertEqual(bot.event_pull_numbers(github, "o/r", "workflow_dispatch", {"inputs": {"pr": ""}}), [7, 8])


if __name__ == "__main__":
    unittest.main()
