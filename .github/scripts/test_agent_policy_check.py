"""Unit tests for .github/scripts/agent_policy_check.py, the agent-policy commit status."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# The checker lives in .github/scripts/ of the repo that holds this test.
_ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".github/scripts/agent_policy_check.py").exists())
sys.path.insert(0, str(_ROOT / ".github/scripts"))

import agent_policy_check as check  # noqa: E402

HEAD = "a" * 40
OLD = "b" * 40
POLICY = {
    "owners": ["NicholasDominici", "jonnybcw"],
    "gates": [
        {"id": "billing", "reason": "Billing", "paths": ["src/services/*billing*.ts", "src/billing/**"]},
        {"id": "developer-api-contract", "reason": "Contract", "contractSnapshot": "tests/contracts/snap.json"},
    ],
    "uiPaths": ["web/**"],
    "noBehaviorPaths": ["**/*.md", "tests/**", "src/tests/**"],
}


def pr(author="NicholasDominici", association="OWNER", body="", number=1, base="main"):
    return {
        "number": number,
        "state": "open",
        "user": {"login": author},
        "author_association": association,
        "head": {"sha": HEAD},
        "base": {"ref": base, "sha": OLD},
        "body": body,
    }


def files(*names):
    return [{"filename": name} for name in names]


def record(
    kind, approver="NicholasDominici", commit=HEAD, association="OWNER", words='"ship it"', where="agent session"
):
    body = (
        f"{check.RECORD_MARKER}\n**Approval record**\n- Kind: {kind}\n- Approved by: @{approver}\n"
        f"- Words: {words}\n- Where: {where}\n- Commit: {commit}\n"
    )
    return {"body": body, "author_association": association, "user": {"login": "NicholasDominici"}}


def carry(from_sha, to_sha):
    return {
        "body": f"{check.CARRY_MARKER}\n- From: {from_sha}\n- Commit: {to_sha}\n- Reason: fix-only",
        "author_association": "OWNER",
        "user": {"login": "NicholasDominici"},
    }


class GlobTest(unittest.TestCase):
    def test_double_star_crosses_directories(self):
        self.assertTrue(check.matches_any("src/billing/a/b.ts", ["src/billing/**"]))
        self.assertTrue(check.matches_any("README.md", ["**/*.md"]))
        self.assertTrue(check.matches_any("docs/x/README.md", ["**/*.md"]))

    def test_single_star_stays_in_directory(self):
        self.assertTrue(check.matches_any("src/services/developer-api-billing.ts", ["src/services/*billing*.ts"]))
        self.assertFalse(check.matches_any("src/services/sub/billing.ts", ["src/services/*billing*.ts"]))


class EvaluateTest(unittest.TestCase):
    def test_owner_change_outside_gates_needs_nothing(self):
        result = check.evaluate(POLICY, pr(), files("src/app.py"), [], {})
        self.assertEqual(result.requirements, [])
        self.assertTrue(result.passed)

    def test_billing_gate_needs_owner_record_on_head(self):
        result = check.evaluate(POLICY, pr(), files("src/services/developer-api-billing.ts"), [], {})
        self.assertFalse(result.passed)
        result = check.evaluate(
            POLICY, pr(), files("src/services/developer-api-billing.ts"), [record("gate:billing")], {}
        )
        self.assertTrue(result.passed)

    def test_record_for_old_commit_needs_a_carry_chain(self):
        changed = files("src/billing/x.ts")
        self.assertFalse(check.evaluate(POLICY, pr(), changed, [record("gate:billing", commit=OLD)], {}).passed)
        comments = [record("gate:billing", commit=OLD), carry(OLD, HEAD)]
        self.assertTrue(check.evaluate(POLICY, pr(), changed, comments, {}).passed)

    def test_non_owner_approver_and_outside_comment_do_not_count(self):
        changed = files("src/billing/x.ts")
        self.assertFalse(
            check.evaluate(POLICY, pr(), changed, [record("gate:billing", approver="hunterrice")], {}).passed
        )
        forged = record("gate:billing", association="NONE")
        self.assertFalse(check.evaluate(POLICY, pr(), changed, [forged], {}).passed)

    def test_records_from_the_agent_app_count(self):
        changed = files("src/billing/x.ts")
        bot = {**record("gate:billing", association="NONE"), "user": {"login": "orbit-harness[bot]"}}
        self.assertFalse(check.evaluate(POLICY, pr(), changed, [bot], {}).passed)
        policy = {**POLICY, "agentLogins": ["orbit-harness[bot]"]}
        self.assertTrue(check.evaluate(policy, pr(), changed, [bot], {}).passed)

    def test_non_owner_behavior_change_needs_owner_clearance(self):
        author = pr(author="hunterrice", association="MEMBER")
        result = check.evaluate(POLICY, author, files("src/endpoints/search.ts"), [], {})
        self.assertEqual([r.kind for r in result.requirements], ["owner-clearance"])
        result = check.evaluate(POLICY, author, files("src/endpoints/search.ts"), [record("owner-clearance")], {})
        self.assertTrue(result.passed)

    def test_non_owner_docs_and_tests_change_needs_nothing(self):
        author = pr(author="hunterrice", association="MEMBER")
        self.assertTrue(check.evaluate(POLICY, author, files("README.md", "tests/a.test.ts"), [], {}).passed)

    def test_one_record_can_meet_two_requirements(self):
        author = pr(author="hunterrice", association="MEMBER")
        comments = [record("owner-clearance, gate:billing")]
        self.assertTrue(check.evaluate(POLICY, author, files("src/billing/x.ts"), comments, {}).passed)

    def test_a_partial_file_list_fails_closed(self):
        big = {**pr(), "changed_files": 3500}
        result = check.evaluate(POLICY, big, files("README.md"), [], {})
        self.assertEqual([r.kind for r in result.requirements], ["gate:large-pr"])
        self.assertTrue(check.evaluate(POLICY, big, files("README.md"), [record("gate:large-pr")], {}).passed)

    def test_write_access_decides_inside_or_outside(self):
        private_member = pr(association="CONTRIBUTOR")
        insiders = {"nicholasdominici"}
        self.assertTrue(check.evaluate(POLICY, private_member, files("README.md"), [], {}, insiders).passed)
        stranger = pr(author="someone", association="MEMBER")
        kinds = [r.kind for r in check.evaluate(POLICY, stranger, files("README.md"), [], {}, insiders).requirements]
        self.assertIn("outside-author", kinds)

    def test_records_from_private_members_count_with_insiders(self):
        changed = files("src/billing/x.ts")
        comment = record("gate:billing", association="CONTRIBUTOR")
        comment["user"] = {"login": "NicholasDominici"}
        self.assertFalse(check.evaluate(POLICY, pr(), changed, [comment], {}).passed)
        self.assertTrue(check.evaluate(POLICY, pr(), changed, [comment], {}, {"nicholasdominici"}).passed)

    def test_outside_author_needs_owner_record(self):
        outsider = pr(author="someone", association="CONTRIBUTOR")
        kinds = [r.kind for r in check.evaluate(POLICY, outsider, files("README.md"), [], {}).requirements]
        self.assertEqual(kinds, ["outside-author"])

    def test_contract_change_kinds(self):
        self.assertEqual(
            check.contract_change({"a": {"type": "string"}}, {"a": {"type": "string"}, "b": {}}), "additive"
        )
        self.assertEqual(check.contract_change({"a": {"type": "string"}}, {"a": {"type": "number"}}), "changed")
        self.assertEqual(check.contract_change({"a": 1, "b": 2}, {"a": 1}), "changed")
        self.assertEqual(check.contract_change({"sourceFile": "x", "a": 1}, {"sourceFile": "y", "a": 1}), "none")
        variants = [{"type": "null"}, {"type": "object", "properties": {"a": {}}}]
        grown = [{"type": "null"}, {"type": "object", "properties": {"a": {}, "b": {}}}]
        self.assertEqual(check.contract_change({"variants": variants}, {"variants": grown}), "additive")
        self.assertEqual(check.contract_change({"variants": variants}, {"variants": variants[:1]}), "changed")

    def test_api_fields_named_like_metadata_are_compared(self):
        def snapshot(kind):
            return {
                "description": "doc",
                "contracts": {
                    "Dto": {"sourceFile": "a.ts", "shape": {"properties": {"description": {"shape": {"type": kind}}}}}
                },
            }

        self.assertEqual(check.contract_change(snapshot("string"), snapshot("number")), "changed")
        moved = snapshot("string")
        moved["contracts"]["Dto"]["sourceFile"] = "b.ts"
        moved["description"] = "new doc"
        self.assertEqual(check.contract_change(snapshot("string"), moved), "none")

    def test_a_record_without_words_or_where_does_not_count(self):
        changed = files("src/billing/x.ts")
        for comment in (
            record("gate:billing", words=""),
            record("gate:billing", where=""),
            record("gate:billing", words='"<what the owner said>"'),
        ):
            self.assertFalse(check.evaluate(POLICY, pr(), changed, [comment], {}).passed)

    def test_only_a_changed_contract_needs_the_gate(self):
        changed = files("tests/contracts/snap.json")
        self.assertTrue(check.evaluate(POLICY, pr(), changed, [], {"tests/contracts/snap.json": "additive"}).passed)
        result = check.evaluate(POLICY, pr(), changed, [], {"tests/contracts/snap.json": "changed"})
        self.assertEqual([r.kind for r in result.requirements], ["gate:developer-api-contract"])

    def test_ui_change_needs_media_and_approval_or_small_record(self):
        changed = files("web/page.tsx")
        no_media = check.evaluate(POLICY, pr(), changed, [record("ui-small")], {})
        self.assertFalse(no_media.passed)
        body = "Desktop ![d](https://x/d.png) Mobile ![m](https://x/m.png)"
        self.assertFalse(check.evaluate(POLICY, pr(body=body), changed, [], {}).passed)
        self.assertTrue(check.evaluate(POLICY, pr(body=body), changed, [record("ui-small")], {}).passed)
        self.assertTrue(check.evaluate(POLICY, pr(body=body), changed, [record("gate:ui")], {}).passed)


class FakeGitHub:
    """Serves PRs, files and comments from dicts and records every status write."""

    def __init__(self, prs, files_by_pr, comments_by_pr, fail_on=None):
        self.repo = "orbit-search/example"
        self.prs = prs
        self.files_by_pr = files_by_pr
        self.comments_by_pr = comments_by_pr
        self.fail_on = fail_on
        self.statuses = []

    def request(self, method, path, payload=None, raw=False):
        if method == "POST" and path.startswith("/statuses/"):
            self.statuses.append(payload)
            return {}
        if method == "POST":
            return {}
        if self.fail_on and self.fail_on in path:
            raise check.urllib.error.URLError("GitHub API unavailable")
        if path.startswith("/commits/"):
            sha = path.split("/")[2]
            return [item for item in self.prs.values() if item["head"]["sha"] == sha]
        return self.prs[int(path.rsplit("/", 1)[1])]

    def paginate(self, path):
        number = int(path.split("/")[2])
        if self.fail_on and self.fail_on in path:
            raise check.urllib.error.URLError("GitHub API unavailable")
        return self.files_by_pr[number] if path.endswith("/files") else self.comments_by_pr[number]

    def has_write_access(self, login):
        return login.lower() in {"nicholasdominici", "jonnybcw", "hunterrice"}

    def file_at(self, path, ref):
        return self.policies.get(ref) if hasattr(self, "policies") else None


class MainTest(unittest.TestCase):
    def run_main(self, fake, number=1, **event_fields):
        with tempfile.TemporaryDirectory() as tmp:
            event = Path(tmp) / "event.json"
            event.write_text(json.dumps({"pull_request": {"number": number}, **event_fields}))
            policy = Path(tmp) / "policy.json"
            policy.write_text(json.dumps(POLICY))
            env = {
                "GITHUB_REPOSITORY": fake.repo,
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_TOKEN": "x",
                "AGENT_POLICY_PATH": str(policy),
                "GITHUB_EVENT_NAME": "pull_request_target",
            }
            with mock.patch.dict(os.environ, env), mock.patch.object(check, "GitHub", lambda repo, token: fake):
                return check.main()

    def test_status_is_pending_before_the_result(self):
        fake = FakeGitHub({1: pr()}, {1: files("src/app.py")}, {1: []})
        self.run_main(fake)
        self.assertEqual([s["state"] for s in fake.statuses], ["pending", "success"])

    def test_a_failed_recheck_replaces_an_earlier_success(self):
        fake = FakeGitHub({1: pr()}, {1: files("src/billing/x.ts")}, {1: []}, fail_on="/comments")
        with self.assertRaises(check.urllib.error.URLError):
            self.run_main(fake)
        self.assertEqual([s["state"] for s in fake.statuses], ["pending", "error"])

    def test_prs_sharing_a_head_all_need_their_approvals(self):
        approved = pr(number=1, base="main")
        unapproved = pr(number=2, base="dev")
        fake = FakeGitHub(
            {1: approved, 2: unapproved},
            {1: files("src/billing/x.ts"), 2: files("src/billing/x.ts")},
            {1: [record("gate:billing")], 2: []},
        )
        self.run_main(fake, number=1)
        self.assertEqual(fake.statuses[-1]["state"], "failure")
        self.assertIn("#2", fake.statuses[-1]["description"])

    def test_a_failed_pr_listing_replaces_an_earlier_success(self):
        fake = FakeGitHub({1: pr()}, {1: files("src/app.py")}, {1: []}, fail_on="/commits/")
        with self.assertRaises(check.urllib.error.URLError):
            self.run_main(fake)
        self.assertEqual([s["state"] for s in fake.statuses][-1], "error")
        self.assertNotIn("success", [s["state"] for s in fake.statuses])

    def test_github_reads_retry_network_errors(self):
        client = check.GitHub("orbit-search/example", "x")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        calls = [check.urllib.error.URLError("reset"), response]
        with (
            mock.patch.object(check.urllib.request, "urlopen", side_effect=calls),
            mock.patch.object(check.time, "sleep"),
        ):
            self.assertEqual(client.request("GET", "/pulls/1"), {"ok": True})

    def test_each_pr_follows_its_target_branch_policy(self):
        fake = FakeGitHub({1: pr()}, {1: files("src/app.py")}, {1: []})
        fake.policies = {OLD: {**POLICY, "gates": [{"id": "all", "reason": "Everything", "paths": ["src/**"]}]}}
        self.run_main(fake)
        self.assertEqual(fake.statuses[-1]["state"], "failure")
        self.assertIn("gate:all", fake.statuses[-1]["description"])

    def shared_head(self):
        approved = pr(number=1, base="main")
        unapproved = pr(number=2, base="dev")
        return FakeGitHub(
            {1: approved, 2: unapproved},
            {1: files("src/billing/x.ts"), 2: files("src/billing/x.ts")},
            {1: [record("gate:billing")], 2: []},
        )

    def test_closing_the_unapproved_pr_frees_the_shared_head(self):
        fake = self.shared_head()
        fake.prs[2]["state"] = "closed"
        self.run_main(fake, number=2, action="closed")
        self.assertEqual(fake.statuses[-1]["state"], "success")

    def test_moving_the_unapproved_pr_rechecks_its_old_head(self):
        fake = self.shared_head()
        new_head = "c" * 40
        fake.prs[2]["head"] = {"sha": new_head}
        self.run_main(fake, number=2, action="synchronize", before=HEAD)
        old_head_states = [s["state"] for s in fake.statuses[:2]]
        self.assertEqual(old_head_states, ["pending", "success"])
        self.assertEqual(fake.statuses[-1]["state"], "failure")


class WriteAccessTest(unittest.TestCase):
    def access(self, answer=None, error=None):
        client = check.GitHub("orbit-search/example", "x")
        side = error if error is not None else (lambda *a, **k: answer)
        with mock.patch.object(client, "request", side_effect=side):
            return client.has_write_access("someone")

    def test_write_level_permissions_count(self):
        for level in ("admin", "maintain", "write"):
            self.assertTrue(self.access({"permission": level}), level)

    def test_lower_permissions_do_not_count(self):
        for level in ("triage", "read", "none"):
            self.assertFalse(self.access({"permission": level}), level)

    def test_unknown_user_is_outside_and_other_errors_raise(self):
        not_found = check.urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        self.assertFalse(self.access(error=not_found))
        server = check.urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None)
        with self.assertRaises(check.urllib.error.HTTPError):
            self.access(error=server)


class PolicyFileTest(unittest.TestCase):
    def test_the_repo_policy_is_valid(self):
        policy = json.loads((_ROOT / ".github/agent-policy.json").read_text())
        self.assertTrue(policy["owners"])
        for gate in policy["gates"]:
            self.assertTrue(gate["id"] and gate["reason"])
            self.assertTrue(gate.get("paths") or gate.get("contractSnapshot"))


if __name__ == "__main__":
    unittest.main()
