"""Tests of kind vercel (release_vercel.py and its parts of release_light.py and release_kit.py).

Standard library only: `python3 -m unittest discover -s release -p 'test_*.py'`.
Every Vercel and GitHub call goes to an injected fake; the token is a fake
string. urllib.request.urlopen and subprocess.run raise in every test, so no
test reaches Vercel, GitHub, Secret Manager or a CLI.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import promote_release_guard as guard  # noqa: E402
import release_config as rc  # noqa: E402
import release_gate as rg  # noqa: E402
import release_kit as kit  # noqa: E402
import release_light as rl  # noqa: E402
import release_record as rr  # noqa: E402
import release_rollback as rb  # noqa: E402
import release_signals as rs  # noqa: E402
import release_vercel as rvc  # noqa: E402
import release_verify as rv  # noqa: E402

TEAM = "team_demo123"
TOKEN = "test-token-not-real"
BYPASS = "bypass-secret-not-real"
COMMIT = "c" * 40
OLD_COMMIT = "b" * 40
NEWER_COMMIT = "d" * 40
DOMAIN = "demo.example.com"


def vercel_data(**overrides) -> dict:
    """A kind vercel config: one project, one domain, one smoke check."""
    data = {
        "service": "demo-web",
        "title": "demo web",
        "kind": "vercel",
        "tier": "full",
        "github_repo": "orbit-search/demo-web",
        "connected_repository": "orbit-search-demo-web",
        "vercel": {
            "team": TEAM,
            "project": "demo-web",
            "domains": [DOMAIN],
            "log_settle_seconds": 0,
            "assign_wait_seconds": 30,
        },
        "triggers": {"dev": ["demo-web-release"], "prod": ["demo-web-promote"], "rollback": "demo-web-rollback"},
        "smoke": [{"type": "http_public", "name": "health", "url": f"https://{DOMAIN}/health?x=1"}],
        "verify": {"checkpoints": [15, 60]},
        "code": {"source_prefixes": ["app/"], "extensions": [".ts", ".tsx"]},
    }
    data.update(overrides)
    return data


WEB = rl.parse_light(vercel_data())
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=rr.UTC)


def _ms(when: datetime) -> int:
    """Milliseconds since the epoch."""
    return int(when.timestamp() * 1000)


def deployment(uid: str, commit: str, state: str = "READY", created: int = 1000, ref: str = "main", **extra) -> dict:
    """A deployment as the Vercel API returns it."""
    return {
        "uid": uid,
        "id": uid,
        "url": f"{uid.lower()}-orbit.vercel.app",
        "readyState": state,
        "state": state,
        "target": "production",
        "source": "git",
        "createdAt": created,
        "meta": {"githubCommitSha": commit, "githubCommitRef": ref},
        **extra,
    }


def log_row(dpl: str, when: datetime, status: int = 200, logs: list | None = None, path: str = "/") -> dict:
    """A request log row."""
    return {
        "requestId": f"r-{dpl}-{when.timestamp()}",
        "timestamp": when.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "deploymentId": dpl,
        "statusCode": status,
        "requestPath": path,
        "logs": logs or [],
    }


class FakeVercel:
    """The Vercel API of one team: a project, deployments, aliases and request logs. Records every request."""

    def __init__(self) -> None:
        """One live deployment (dpl_old of OLD_COMMIT) and one staged (dpl_new of COMMIT)."""
        self.requests: list[tuple[str, str, dict, dict | None, dict]] = []
        self.project = {
            "id": "prj_1",
            "name": "demo-web",
            "autoAssignCustomDomains": True,
            "lastRollbackTarget": None,
            "protectionBypass": {BYPASS: {"scope": "automation-bypass"}, "other": {"scope": "integration"}},
        }
        self.deployments = {
            "dpl_old": deployment("dpl_old", OLD_COMMIT, created=1000),
            "dpl_new": deployment("dpl_new", COMMIT, created=2000),
        }
        self.aliases = {DOMAIN: "dpl_old"}
        self.rows: list[dict] = []
        self.fail: dict[str, int] = {}
        # What a passed Deployment Check does in Vercel: the domains move to that deployment.
        self.assign_on_promote = True

    def __call__(self, request: urllib.request.Request) -> tuple[int, bytes]:
        """Answer one request."""
        parts = urllib.parse.urlsplit(request.full_url)
        query = {key: values[0] for key, values in urllib.parse.parse_qs(parts.query).items()}
        body = json.loads(request.data) if request.data else None
        self.requests.append((request.get_method(), parts.path, query, body, dict(request.header_items())))
        if query.get("teamId") != TEAM:
            return 403, b'{"error": "no team"}'
        if request.get_header("Authorization") != f"Bearer {TOKEN}":
            return 401, b'{"error": "bad token"}'
        for prefix, status in self.fail.items():
            if parts.path.startswith(prefix):
                return status, b'{"error": {"message": "failed on purpose"}}'
        path = parts.path
        if path == "/v9/projects/demo-web":
            return 200, json.dumps(self.project).encode()
        if path.startswith("/v13/deployments/"):
            uid = path.rsplit("/", 1)[1]
            if uid not in self.deployments:
                return 404, b'{"error": {"code": "not_found"}}'
            return 200, json.dumps(self.deployments[uid]).encode()
        if path == "/v6/deployments":
            items = sorted(self.deployments.values(), key=lambda item: -item["createdAt"])
            if query.get("meta-githubCommitSha"):
                items = [item for item in items if item["meta"]["githubCommitSha"] == query["meta-githubCommitSha"]]
            if query.get("since"):
                items = [item for item in items if item["createdAt"] >= int(query["since"])]
            return 200, json.dumps({"deployments": items}).encode()
        if path.startswith("/v4/aliases/"):
            domain = path.rsplit("/", 1)[1]
            return 200, json.dumps({"alias": domain, "deploymentId": self.aliases.get(domain)}).encode()
        if path.startswith("/v10/projects/prj_1/promote/"):
            self.aliases = {domain: path.rsplit("/", 1)[1] for domain in self.aliases}
            self.project["autoAssignCustomDomains"] = True
            return 201, b""
        if path.startswith("/v9/projects/prj_1/rollback/"):
            self.aliases = {domain: path.rsplit("/", 1)[1] for domain in self.aliases}
            self.project["autoAssignCustomDomains"] = False
            return 201, b""
        if path == "/api/logs/request-logs":
            return 200, self.logs(query)
        raise AssertionError(f"unexpected Vercel request {request.get_method()} {path}")

    def logs(self, query: dict) -> bytes:
        """One page (50 rows) of the request logs that match the query."""
        start, end = int(query["startDate"]), int(query["endDate"])
        rows = []
        for row in self.rows:
            when = _ms(datetime.strptime(row["timestamp"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=rr.UTC))
            if not start <= when < end:
                continue
            if query.get("deploymentId") and row["deploymentId"] != query["deploymentId"]:
                continue
            if query.get("statusCode") == "5xx" and row["statusCode"] < 500:
                continue
            levels = set((query.get("level") or "").split(",")) - {""}
            if levels and not any(item.get("level") in levels for item in row["logs"]):
                continue
            rows.append(row)
        page = int(query.get("page") or 0)
        chunk = rows[page * 50 : page * 50 + 50]
        return json.dumps({"rows": chunk, "hasMoreRows": len(rows) > page * 50 + 50}).encode()

    def calls(self, method: str, prefix: str) -> list[str]:
        """The paths of the recorded requests with this method and path prefix."""
        return [path for verb, path, _, _, _ in self.requests if verb == method and path.startswith(prefix)]


def _no_network(*_args, **_kwargs):
    """Every live call fails the test."""
    raise AssertionError("a test reached the network or a CLI")


def _args(**values) -> argparse.Namespace:
    """An argparse namespace with the defaults of the light commands."""
    defaults = {
        "env": "prod",
        "build_id": "b1",
        "commit": COMMIT,
        "previous_commit": "",
        "images_file": None,
        "artifacts_file": None,
        "release_id": "latest",
        "release_id_file": None,
        "status": "deployed",
        "force_fail": "",
        "failed_marker": "",
        "alerted_marker": "",
        "parse_result": "",
        "build_url": "https://build",
        "wait": False,
        "dry_run_dir": None,
        "out_dir": "",
        "poll_seconds": 0.0,
        "older_timeout_seconds": 1,
        "dev_timeout_seconds": 1,
        "from_dev_index": False,
        "from_skip_file": None,
        "state": None,
        "description": "",
        "target_url": "",
    }
    defaults.update(values)
    return argparse.Namespace(**defaults)


class VercelCase(unittest.TestCase):
    """A fake Vercel, recorded commit statuses and Slack lines, local stores, and no network."""

    def setUp(self) -> None:
        """Start the fakes and patches."""
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vercel = FakeVercel()
        self.statuses: list[tuple[str, str, str, str]] = []
        self.slack: list[str] = []

        def status(repo, commit, context, state, description, token, **_):
            """Record a commit status."""
            self.statuses.append((commit, context, state, description))

        def slack(text, blocks=None):
            """Record a Slack line."""
            self.slack.append(text)
            return True

        self.api = rvc.VercelApi(TEAM, lambda: TOKEN, http=self.vercel, sleep=lambda _: None)
        self.patches = [
            mock.patch.dict(os.environ, {"VERCEL_RELEASE_TOKEN": "", "GITHUB_TOKEN": "", "WEBHOOK_ORBIT": ""}),
            mock.patch.object(urllib.request, "urlopen", side_effect=_no_network),
            mock.patch.object(subprocess, "run", side_effect=_no_network),
            mock.patch.object(rl, "vercel_api", return_value=self.api),
            mock.patch.object(rl, "GoogleApi"),
            mock.patch.object(rvc, "post_status", side_effect=status),
            mock.patch.object(rl, "status_token", return_value="github-test-token"),
            mock.patch.object(rr, "github_token", side_effect=guard.GuardError("no token")),
            mock.patch.object(rr, "build_info", return_value={"id": "b1"}),
            mock.patch.object(rr, "post_slack", side_effect=slack),
        ]
        for patch in self.patches:
            patch.start()
        self.dev = rr.LocalStore(self.root / "dev")
        self.prod = rr.LocalStore(self.root / "prod")

    def tearDown(self) -> None:
        """Stop the patches and remove what the test created."""
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()


# --------------------------------------------------------------------------- config and render


class VercelConfigTest(unittest.TestCase):
    def test_parse(self) -> None:
        """Kind vercel is tier full with a dev stage, its own context, and the token secret."""
        self.assertTrue(WEB.has_gate and WEB.has_dev and WEB.auto_rollback)
        self.assertEqual(WEB.vercel_team, TEAM)
        self.assertEqual(WEB.vercel_domains, (DOMAIN,))
        self.assertEqual(WEB.check_context, "orbit-release/demo-web")
        self.assertEqual(WEB.token_secret, "vercel-release-token")
        self.assertEqual(WEB.github_token_secret, "connection")
        other = rl.parse_light(vercel_data(vercel={**vercel_data()["vercel"], "github_token_secret": "GITHUB_TOKEN"}))
        self.assertIn(
            "secrets/GITHUB_TOKEN/versions/latest", kit.rendered(other)["release/services/demo-web.rollback.yaml"]
        )
        self.assertEqual(WEB.dev.deploy_trigger, "demo-web-release")
        self.assertEqual(WEB.prod.deploy_trigger, "demo-web-promote")
        self.assertEqual(WEB.rollback_trigger, "demo-web-rollback")
        self.assertEqual(WEB.log_settle_seconds, 0)
        self.assertEqual(WEB.log_filter, "")

    def test_named_github_secret_renders_it_everywhere(self) -> None:
        """A named github_token_secret puts that secret in GITHUB_TOKEN of every build and every status step."""
        named = rl.parse_light(
            vercel_data(vercel={**vercel_data()["vercel"], "github_token_secret": "release-kit-github-token"})
        )
        for name, text in kit.rendered(named).items():
            self.assertIn("secrets/release-kit-github-token/versions/latest", text, name)
            self.assertIn('env: "GITHUB_TOKEN"', text, name)
        release = kit.rendered(named)["release/services/demo-web.release.yaml"]
        default = kit.rendered(WEB)["release/services/demo-web.release.yaml"]
        self.assertEqual(release.count('secretEnv: ["GITHUB_TOKEN"]'), 3)
        # The steps that had only GITHUB_TOKEN lose their secretEnv line; no step is lost.
        self.assertEqual(default.count("    - name:"), release.count("    - name:"))
        self.assertNotIn("secretEnv: []", default)

    def test_status_token_sources(self) -> None:
        """GITHUB_TOKEN first; else a read-write connection token per status; a named secret needs GITHUB_TOKEN."""
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "env-token"}):
            self.assertEqual(rl.status_token(WEB), "env-token")
        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": ""}),
            mock.patch.object(guard.GoogleApi, "repository_read_write_token", return_value="rw-token") as mint,
        ):
            self.assertEqual(rl.status_token(WEB), "rw-token")
            self.assertEqual(rl.status_token(WEB), "rw-token")
            self.assertEqual(mint.call_count, 2)  # minted for each status, never cached
            mint.assert_called_with("orbit-search", "orbit-search-demo-web")
        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": ""}),
            mock.patch.object(guard.GoogleApi, "repository_read_write_token", side_effect=guard.GuardError("no role")),
        ):
            with self.assertRaisesRegex(rr.RecordError, "no GitHub token for the commit status: no role"):
                rl.status_token(WEB)
        named = rl.parse_light(vercel_data(vercel={**vercel_data()["vercel"], "github_token_secret": "gh-secret"}))
        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": ""}),
            mock.patch.object(guard.GoogleApi, "repository_read_write_token") as mint,
        ):
            with self.assertRaisesRegex(rr.RecordError, "gh-secret"):
                rl.status_token(named)
            mint.assert_not_called()

    def test_read_write_token_request(self) -> None:
        """The read-write token comes from accessReadWriteToken of the linked repository."""
        api = guard.GoogleApi("orbit-search", "us-central1")
        seen = []

        def fake_http(request):
            seen.append((request.get_method(), request.full_url))
            return {"token": "rw-token"}

        with (
            mock.patch.object(api, "_access_token", return_value="gcloud-token"),
            mock.patch.object(guard, "_http_json", side_effect=fake_http),
        ):
            self.assertEqual(api.repository_read_write_token("orbit-search", "orbit-search-demo-web"), "rw-token")
        self.assertEqual(
            seen,
            [
                (
                    "POST",
                    "https://cloudbuild.googleapis.com/v2/projects/orbit-search/locations/us-central1/connections/"
                    "orbit-search/repositories/orbit-search-demo-web:accessReadWriteToken",
                )
            ],
        )
        with (
            mock.patch.object(api, "_access_token", return_value="gcloud-token"),
            mock.patch.object(guard, "_http_json", side_effect=guard.GuardError("HTTP 403")),
        ):
            with self.assertRaisesRegex(guard.GuardError, "roles/cloudbuild.tokenAccessor"):
                api.repository_read_write_token("orbit-search", "orbit-search-demo-web")

    def test_bad_configs(self) -> None:
        """Wrong tier, a smoke URL off the domains, no smoke check, a bad team, domain or trigger list."""
        bad = [
            vercel_data(tier="check_only"),
            vercel_data(smoke=[{"type": "http_public", "url": "https://other.example.com/health"}]),
            vercel_data(smoke=[{"type": "http_public", "url": f"http://{DOMAIN}/health"}]),
            vercel_data(smoke=[]),
            vercel_data(vercel={"team": "orbit", "project": "demo-web", "domains": [DOMAIN]}),
            vercel_data(vercel={"team": TEAM, "project": "demo-web", "domains": ["https://demo.example.com"]}),
            vercel_data(vercel={"team": TEAM, "project": "demo-web"}),
            vercel_data(triggers={"dev": ["x"]}),
            vercel_data(deploy_triggers=["x"]),
        ]
        for data in bad:
            with self.assertRaises(rc.ConfigError, msg=json.dumps(data)[:300]):
                rl.parse_light(data)

    def test_loader_picks_light(self) -> None:
        """release_config.load_any_service loads kind vercel through release_light."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "demo-web.json").write_text(json.dumps(vercel_data()))
            with mock.patch.object(rc, "SERVICES_DIR", Path(tmp)):
                self.assertEqual(rc.config_kind("demo-web"), "vercel")
                self.assertIsInstance(rc.load_any_service("demo-web"), rl.LightService)

    def test_render(self) -> None:
        """The kit renders all four builds; each reads the three secrets and names the service."""
        # As a private repository (a vendored people map): a public repository's copy has none.
        with mock.patch.object(kit, "people_vendored", return_value=True):
            files = kit.rendered(WEB)
        self.assertEqual(
            sorted(files),
            [
                "release/services/demo-web.promote.yaml",
                "release/services/demo-web.release-verify.yaml",
                "release/services/demo-web.release.yaml",
                "release/services/demo-web.rollback.yaml",
            ],
        )
        for name, text in files.items():
            self.assertIn("secrets/vercel-release-token/versions/latest", text, name)
            # Default github_token_secret "connection": no GitHub token secret in any build.
            self.assertNotIn("GITHUB_TOKEN", text, name)
            self.assertNotIn("secrets/connection/", text, name)
            self.assertIn('secretEnv: ["WEBHOOK_ORBIT", "VERCEL_RELEASE_TOKEN"]', text, name)
            self.assertNotIn("kubectl", text, name)
            self.assertNotIn(TOKEN, text, name)
        release = files["release/services/demo-web.release.yaml"]
        self.assertIn("vercel-wait --commit", release)
        self.assertIn('if [ "$${WAIT_STATUS}" -eq 5 ]', release)
        self.assertIn("release_gate.py --service demo-web request-promote", release)
        self.assertIn("--from-dev-index", release)
        promote = files["release/services/demo-web.promote.yaml"]
        for step in ("promote-preflight", "promote-gate", "release-record-begin", "promote", "status-skip"):
            self.assertIn(f'id: "{step}"', promote)
        self.assertIn("check-prod", promote)
        self.assertIn('tags: ["demo-web-rollback"]', files["release/services/demo-web.rollback.yaml"])
        self.assertIn(
            '"RELEASE_AUTO_ROLLBACK=${_AUTO_ROLLBACK}"', files["release/services/demo-web.release-verify.yaml"]
        )

    def test_rendered_builds_post_only_status_lines(self) -> None:
        """Every Slack post of a rendered build is a status line with the ops board link; reasons stay in the log."""
        with mock.patch.dict(os.environ, {"RELEASE_OPS_URL": ""}):
            files = kit.rendered(WEB)
        posts = [
            line.split("--data ", 1)[1]
            for text in files.values()
            for line in text.splitlines()
            if "curl" in line and "WEBHOOK_ORBIT" in line
        ]
        self.assertEqual(len(posts), 4, posts)
        lines = {rr.rendered_status_line(state, WEB.name) for state in ("on hold", "warning", "down")}
        for post in posts:
            self.assertTrue(any(f'"text":"{line}"' in post for line in lines), post)
        self.assertNotIn("PAYLOAD", "".join(files.values()))

    def test_rendered_promote_keeps_the_prod_build_contract(self) -> None:
        """Every stop before the promote leaves the no-change receipt, and the finish retries."""
        import test_release_kit as tk

        promote = kit.rendered(WEB)["release/services/demo-web.promote.yaml"]
        self.assertEqual(kit.prod_build_problems("promote.yaml", promote), [])
        steps = {kit._step_id(block): block for block in kit.STEP_START.split(promote)}
        tk.assert_receipts(self, steps["release-record-begin"], steps["notify-promote-blocked"])
        self.assertIn("WEBHOOK_ORBIT", steps["release-record-finish"])

    def test_a_failed_promote_posts_one_line(self) -> None:
        """A failed promote posts the deploy check's down line only, never down and then warning."""
        import test_release_kit as tk

        tk.assert_one_line_per_result(self, kit.rendered(WEB)["release/services/demo-web.promote.yaml"], WEB.name)

    def test_rendering_ignores_release_ops_url(self) -> None:
        """The rendered builds link the fixed ops board: a local RELEASE_OPS_URL never makes check reject them."""
        with mock.patch.dict(os.environ, {"RELEASE_OPS_URL": ""}):
            default = kit.rendered(WEB)
        with mock.patch.dict(os.environ, {"RELEASE_OPS_URL": "https://ops.example.com"}):
            self.assertEqual(kit.rendered(WEB), default)
        self.assertIn("https://ops.orbitsearch.com/demo-web", default["release/services/demo-web.promote.yaml"])

    def test_kit_files_include_vercel(self) -> None:
        """The module and its tests are pinned kit files (sync copies them)."""
        self.assertIn("release_vercel.py", kit.KIT_FILES)
        self.assertIn("test_release_vercel.py", kit.KIT_FILES)


# --------------------------------------------------------------------------- the API, the token, the status


class VercelApiTest(VercelCase):
    def test_team_and_token_on_every_call(self) -> None:
        """Every request carries the team id and the bearer token; an error message never holds the token."""
        self.assertEqual(self.api.project("demo-web")["id"], "prj_1")
        method, path, query, _, headers = self.vercel.requests[0]
        self.assertEqual((method, path, query["teamId"]), ("GET", "/v9/projects/demo-web", TEAM))
        self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")
        with self.assertRaises(rvc.VercelError) as raised:
            self.api.deployment("dpl_missing")
        self.assertIn("HTTP 404", str(raised.exception))
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertIsInstance(raised.exception, rr.RecordError)

    def test_retries_429_and_5xx(self) -> None:
        """429 and 5xx are tried again; the last answer counts."""
        answers = iter([(429, b""), (502, b""), (200, b'{"id": "prj_1"}')])
        api = rvc.VercelApi(TEAM, lambda: TOKEN, http=lambda _: next(answers), sleep=lambda _: None)
        self.assertEqual(api.project("demo-web"), {"id": "prj_1"})
        always = rvc.VercelApi(TEAM, lambda: TOKEN, http=lambda _: (503, b"busy"), sleep=lambda _: None, attempts=2)
        with self.assertRaises(rvc.VercelError):
            always.project("demo-web")

    def test_request_logs_pages(self) -> None:
        """The logs read follows hasMoreRows and reports an incomplete read at the page limit."""
        start = NOW - timedelta(minutes=30)
        self.vercel.rows = [log_row("dpl_new", start + timedelta(seconds=index)) for index in range(120)]
        rows, complete = self.api.request_logs("prj_1", start, NOW, deployment="dpl_new")
        self.assertEqual((len(rows), complete), (120, True))
        rows, complete = self.api.request_logs("prj_1", start, NOW, deployment="dpl_new", max_pages=2)
        self.assertEqual((len(rows), complete), (100, False))
        _, path, query, _, _ = self.vercel.requests[0]
        self.assertEqual((path, query["ownerId"], query["environment"]), ("/api/logs/request-logs", TEAM, "production"))

    def test_token_source(self) -> None:
        """The token comes from VERCEL_RELEASE_TOKEN, else from Secret Manager (read once, never printed)."""
        calls = []

        def secret_manager(request):
            """Answer the Secret Manager access call."""
            calls.append(request.full_url)
            return 200, json.dumps({"payload": {"data": base64.b64encode(b"sm-token\n").decode()}}).encode()

        with mock.patch.dict(os.environ, {"VERCEL_RELEASE_TOKEN": "env-token"}):
            self.assertEqual(rvc.token_source("orbit-search", "vercel-release-token", lambda: "g")(), "env-token")
        self.assertEqual(calls, [])
        token = rvc.token_source("orbit-search", "vercel-release-token", lambda: "g", http=secret_manager)
        self.assertEqual((token(), token()), ("sm-token", "sm-token"))
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].endswith("/secrets/vercel-release-token/versions/latest:access"))
        denied = rvc.token_source("orbit-search", "vercel-release-token", lambda: "g", http=lambda _: (403, b"no"))
        with self.assertRaises(rvc.VercelError) as raised:
            denied()
        self.assertIn("secretAccessor", str(raised.exception))

    def test_post_status(self) -> None:
        """The status goes to GitHub with the context and a description cut to 140 characters."""
        sent = []

        def github(request):
            """Accept the status."""
            sent.append((request.full_url, json.loads(request.data), request.get_header("Authorization")))
            return 201, b"{}"

        self.patches[5].stop()
        try:
            rvc.post_status("o/r", COMMIT, "orbit-release/x", "pending", "y" * 300, "gh", http=github)
            url, body, auth = sent[0]
            self.assertEqual(url, f"https://api.github.com/repos/o/r/statuses/{COMMIT}")
            self.assertEqual(
                (body["state"], body["context"], len(body["description"])), ("pending", "orbit-release/x", 140)
            )
            self.assertEqual(auth, "Bearer gh")
            with self.assertRaises(rvc.VercelError) as raised:
                rvc.post_status("o/r", COMMIT, "c", "success", "d", "gh", http=lambda _: (404, b"Not Found"))
            self.assertIn("commit status write", str(raised.exception))
            with self.assertRaises(rvc.VercelError):
                rvc.post_status("o/r", COMMIT, "c", "done", "d", "gh", http=github)
        finally:
            self.patches[5].start()

    def test_gate_and_skip_status(self) -> None:
        """Frozen and held stay pending; failed, stale and unverified fail; superseded leaves the status alone."""
        passed = {"verdict": {"status": "passed"}}
        cases = [
            ({}, "failure"),
            ({"verdict": {"status": "failed"}}, "failure"),
            ({"verdict": {"status": "superseded"}}, ""),
            ({**passed, "promote": {"status": "started", "build_id": "p1"}}, "pending"),
            ({**passed, "promote": {"status": "held", "reason": "rolled back"}}, "pending"),
            ({**passed, "promote": {"status": "frozen", "reason": "x"}}, "pending"),
            ({**passed, "promote": {"status": "stale", "reason": "newer"}}, "failure"),
            (passed, "pending"),
        ]
        for index, state in cases:
            self.assertEqual(rvc.gate_status(index)[0], state, index)
        self.assertEqual(rvc.skip_status("held: rolled back. To resume: x")[0], "pending")
        self.assertEqual(rvc.skip_status("frozen: by n")[0], "pending")
        self.assertEqual(rvc.skip_status("stale: prod already runs it")[0], "failure")
        self.assertEqual(rvc.skip_status("unverified: dev verify missing")[0], "failure")
        self.assertEqual(rvc.skip_status("")[0], "")


# --------------------------------------------------------------------------- deployments


class DeploymentTest(VercelCase):
    def test_wait_for_deployment(self) -> None:
        """A building deployment is waited for; a git build wins over a redeploy of the same commit."""
        self.vercel.deployments["dpl_new"]["readyState"] = "BUILDING"
        ticks = iter(range(100))

        def sleep(_):
            """Vercel finishes the build while the wait sleeps."""
            self.vercel.deployments["dpl_new"]["readyState"] = "READY"

        found = rvc.wait_for_deployment(self.api, "prj_1", COMMIT, 60, sleep=sleep, clock=lambda: next(ticks))
        self.assertEqual(rvc.deployment_id(found), "dpl_new")
        self.vercel.deployments["dpl_redeploy"] = deployment("dpl_redeploy", COMMIT, created=3000, source="redeploy")
        self.assertEqual(rvc.deployment_id(rvc.commit_deployment(self.api, "prj_1", COMMIT)), "dpl_new")
        clock = iter([0, 10, 100])
        none = rvc.wait_for_deployment(self.api, "prj_1", "e" * 40, 50, sleep=lambda _: None, clock=lambda: next(clock))
        self.assertIsNone(none)

    def test_newer_deployment(self) -> None:
        """Only a newer, not failed deployment of another commit on the branch supersedes."""
        new = self.vercel.deployments["dpl_new"]
        self.assertIsNone(rvc.newer_deployment(self.api, "prj_1", new, "main"))
        self.vercel.deployments["dpl_same"] = deployment("dpl_same", COMMIT, created=2500)
        self.vercel.deployments["dpl_err"] = deployment("dpl_err", NEWER_COMMIT, "ERROR", created=2600)
        self.vercel.deployments["dpl_branch"] = deployment("dpl_branch", NEWER_COMMIT, created=2700, ref="feature")
        self.assertIsNone(rvc.newer_deployment(self.api, "prj_1", new, "main"))
        self.vercel.deployments["dpl_newer"] = deployment("dpl_newer", NEWER_COMMIT, "BUILDING", created=2800)
        self.assertEqual(rvc.deployment_id(rvc.newer_deployment(self.api, "prj_1", new, "main")), "dpl_newer")

    def test_rollback_state_and_bypass(self) -> None:
        """Auto-assignment off is Vercel's hold; the automation bypass secret is the one with its scope."""
        self.assertFalse(rvc.in_rollback_state(self.vercel.project))
        self.assertTrue(rvc.in_rollback_state({"autoAssignCustomDomains": False}))
        self.assertEqual(rvc.bypass_secret(self.vercel.project), BYPASS)
        self.assertEqual(rvc.bypass_secret({}), "")


# --------------------------------------------------------------------------- the dev build


class VercelDevTest(VercelCase):
    def _wait(self) -> int:
        """Run vercel-wait into the temp dir."""
        return rl.cmd_vercel_wait(_args(out_dir=str(self.root)), WEB, self.dev, None)

    def _record_dev(self) -> str:
        """vercel-wait, then begin and finish the dev record; return its release id."""
        self.assertEqual(self._wait(), rv.EXIT_OK)
        id_file = self.root / "release_id"
        args = _args(env="dev", images_file=str(self.root / "vercel_deployment.json"), release_id_file=str(id_file))
        self.assertEqual(rl.cmd_begin(args, WEB, self.dev, None), 0)
        release_id = id_file.read_text().strip()
        self.assertEqual(rl.cmd_finish(_args(env="dev", release_id=release_id), WEB, self.dev, None), 0)
        return release_id

    def _verify(self, release_id: str, get=None) -> int:
        """Dev verify with a fake HTTP GET (default: 200 for every URL)."""
        seen = []

        def fake_get(request):
            """Record the URL and the bypass header, answer 200."""
            seen.append((request.full_url, request.get_header(rvc.BYPASS_HEADER.capitalize())))
            return 200, b"ok"

        self.seen = seen
        real = rl.vercel_dev_checks
        with (
            mock.patch.object(rl, "vercel_dev_checks", lambda s, r, a: real(s, r, a, get=get or fake_get)),
            mock.patch.object(rv, "newer_dev_build", return_value=None),
        ):
            args = _args(env="dev", release_id=release_id, failed_marker=str(self.root / "failed"))
            return rl.cmd_dev_verify(args, WEB, self.dev, None)

    def test_an_older_dev_record_still_finishes_when_a_newer_one_is_latest(self) -> None:
        """Dev builds overlap: when a newer dev record is latest, an older one finishes (and can verify) and
        latest.json keeps the newer one. Prod still refuses a late finish."""
        self.assertEqual(self._wait(), rv.EXIT_OK)
        ids = []
        started = "2026-10-23T11:00:00Z"
        for name, commit in (("older", COMMIT), ("newer", NEWER_COMMIT)):
            id_file = self.root / f"{name}_id"
            args = _args(
                env="dev",
                commit=commit,
                images_file=str(self.root / "vercel_deployment.json"),
                release_id_file=str(id_file),
            )
            with mock.patch.object(rr, "utc_now", return_value=started):
                self.assertEqual(rl.cmd_begin(args, WEB, self.dev, None), 0)
            ids.append(id_file.read_text().strip())
        older, newer = ids
        self.assertGreater(newer, older)  # same start second: the commit orders them
        self.assertEqual(rl.cmd_finish(_args(env="dev", release_id=newer), WEB, self.dev, None), 0)
        self.assertEqual(rl.cmd_finish(_args(env="dev", release_id=older), WEB, self.dev, None), 0)
        self.assertEqual(rr.read_record(self.dev, older)["status"], "deployed")
        self.assertEqual(rr.read_record(self.dev, older)["artifacts"], {"demo-web": "dpl_new"})
        self.assertEqual(self.dev.read("latest.json")[0]["release_id"], newer)
        # Any other failure to move latest.json still fails the dev finish.
        with (
            mock.patch.object(rr, "point_latest_unless_newer", side_effect=rr.RecordError("write retries exhausted")),
            self.assertRaisesRegex(rr.RecordError, "write retries exhausted"),
        ):
            rl.cmd_finish(_args(env="dev", release_id=newer), WEB, self.dev, None)
        self.prod.write("latest.json", {"release_id": "99991231T000000Z-x", "commit": COMMIT, "kind": "promote"}, 0)
        rr.create_record(
            self.prod,
            {
                "release_id": "20261023T110000Z-c",
                "kind": "promote",
                "commit": COMMIT,
                "status": "deploying",
                "previous": {},
            },
            "deploy_started",
        )
        with self.assertRaisesRegex(rr.RecordError, "newer release"):
            rl.cmd_finish(_args(env="prod", release_id="20261023T110000Z-c"), WEB, self.prod, None)

    def test_wait_outcomes(self) -> None:
        """READY writes the deployment; a cancel by the Ignored Build Step is "not affected" (exit 5, success); any other
        cancel and ERROR fail."""
        self.assertEqual(self._wait(), rv.EXIT_OK)
        self.assertEqual(json.loads((self.root / "vercel_deployment.json").read_text()), {"demo-web": "dpl_new"})
        self.vercel.deployments["dpl_new"]["readyState"] = "CANCELED"
        self.assertEqual(self._wait(), rv.EXIT_ERROR)
        self.assertEqual(self.statuses[-1][2], "failure")
        self.vercel.deployments["dpl_new"]["errorLink"] = "https://vercel.com/docs/platform/projects#ignored-build-step"
        self.assertEqual(self._wait(), rvc.EXIT_NOT_AFFECTED)
        self.assertEqual(self.statuses[-1][2], "success")
        self.assertIn("not affected", self.statuses[-1][3])
        self.vercel.deployments["dpl_new"]["readyState"] = "ERROR"
        self.assertEqual(self._wait(), rv.EXIT_ERROR)
        self.assertEqual(self.statuses[-1][2], "failure")

    def test_dev_verify_passes_and_indexes_the_deployment(self) -> None:
        """The smoke check runs on the deployment URL with the bypass header; the index holds the deployment."""
        release_id = self._record_dev()
        self.assertEqual(rr.read_record(self.dev, release_id)["artifacts"], {"demo-web": "dpl_new"})
        self.assertEqual(self._verify(release_id), rv.EXIT_OK)
        self.assertEqual(self.seen, [("https://dpl_new-orbit.vercel.app/health?x=1", BYPASS)])
        index, _ = self.dev.read(rg.dev_commit_index(COMMIT))
        self.assertEqual(index["verdict"]["status"], "passed")
        self.assertEqual(index["artifacts"], {"demo-web": "dpl_new"})
        record = json.dumps(rr.read_record(self.dev, release_id))
        self.assertNotIn(BYPASS, record)
        self.assertNotIn(TOKEN, record)

    def test_dev_verify_fails_on_an_error_line_or_a_5xx(self) -> None:
        """An error log line or a 5xx of the deployment during dev verify fails it."""
        release_id = self._record_dev()
        recent = datetime.now(rr.UTC) - timedelta(seconds=5)
        self.vercel.rows = [
            log_row("dpl_new", recent, 500, [{"level": "error", "message": "TypeError: x is undefined"}])
        ]
        self.assertEqual(self._verify(release_id), rv.EXIT_FAILED)
        record = rr.read_record(self.dev, release_id)
        self.assertEqual(record["verdict"]["status"], "failed")
        self.assertIn("TypeError", " ".join(record["verdict"]["reasons"]))
        self.assertTrue((self.root / "failed").exists())

    def test_dev_verify_needs_the_bypass_secret_and_a_passing_smoke_check(self) -> None:
        """No Protection Bypass for Automation secret, or a failing smoke check, fails dev verify."""
        release_id = self._record_dev()
        self.vercel.project["protectionBypass"] = {}
        self.assertEqual(self._verify(release_id), rv.EXIT_FAILED)
        self.assertIn("Protection Bypass", " ".join(rr.read_record(self.dev, release_id)["verdict"]["reasons"]))
        self.vercel.project["protectionBypass"] = {BYPASS: {"scope": "automation-bypass"}}
        self.assertEqual(self._verify(release_id, get=lambda _: (500, b"down")), rv.EXIT_FAILED)

    def test_dev_artifacts_refuse_a_wrong_deployment(self) -> None:
        """A deployment of another commit or one that is not READY never becomes the dev artifact."""
        self.vercel.deployments["dpl_new"]["meta"]["githubCommitSha"] = OLD_COMMIT
        with self.assertRaises(rr.RecordError):
            rl.vercel_deployment_artifacts(WEB, self.api, COMMIT, "dpl_new")
        self.vercel.deployments["dpl_new"]["meta"]["githubCommitSha"] = COMMIT
        self.vercel.deployments["dpl_new"]["readyState"] = "ERROR"
        with self.assertRaises(rr.RecordError):
            rl.vercel_deployment_artifacts(WEB, self.api, COMMIT, "dpl_new")

    def test_status_from_the_dev_index(self) -> None:
        """vercel-status --from-dev-index posts what the gate decided; superseded posts nothing."""
        self.dev.write(
            rg.dev_commit_index(COMMIT),
            {"verdict": {"status": "passed"}, "promote": {"status": "held", "reason": "rolled back"}},
            0,
        )
        with mock.patch.object(rl, "open_store", return_value=self.dev):
            rl.cmd_vercel_status(_args(from_dev_index=True), WEB, self.prod, None)
        self.assertEqual(self.statuses[-1][1:3], ("orbit-release/demo-web", "pending"))
        skip = self.root / "skip"
        skip.write_text("stale: prod already runs a newer commit.\n")
        rl.cmd_vercel_status(_args(from_skip_file=str(skip)), WEB, self.prod, None)
        self.assertEqual(self.statuses[-1][2], "failure")
        count = len(self.statuses)
        rl.cmd_vercel_status(_args(from_skip_file=str(self.root / "missing")), WEB, self.prod, None)
        self.assertEqual(len(self.statuses), count)

    def test_cli_rejects_a_short_commit(self) -> None:
        """The vercel commands take only a full commit sha."""
        self.assertEqual(rl.main(["vercel-status", "--commit", "abc", "--state", "pending"]), rv.EXIT_ERROR)


# --------------------------------------------------------------------------- the prod build


class VercelProdTest(VercelCase):
    def test_live_artifacts_and_the_newest_rule(self) -> None:
        """The verified deployment passes while it is the newest; a newer one of another commit supersedes."""
        self.assertEqual(
            rl.vercel_live_artifacts(WEB, self.api, COMMIT, {"demo-web": "dpl_new"}), {"demo-web": "dpl_new"}
        )
        self.vercel.deployments["dpl_newer"] = deployment("dpl_newer", NEWER_COMMIT, "READY", created=3000)
        with self.assertRaises(guard.Superseded):
            rl.vercel_live_artifacts(WEB, self.api, COMMIT, {"demo-web": "dpl_new"})
        with self.assertRaises(rr.RecordError):
            rl.vercel_live_artifacts(WEB, self.api, COMMIT, {})

    def test_hold_guard(self) -> None:
        """A Vercel rollback the kit did not record blocks; the kit's own rollback leaves the decision to the gate."""
        rl.vercel_hold_guard(WEB, self.prod, self.api)
        self.vercel.project["autoAssignCustomDomains"] = False
        with self.assertRaises(guard.GuardError) as raised:
            rl.vercel_hold_guard(WEB, self.prod, self.api)
        self.assertIn("Undo Rollback", str(raised.exception))
        self.prod.write("latest.json", {"release_id": "r1", "commit": OLD_COMMIT, "kind": "rollback"}, 0)
        rl.vercel_hold_guard(WEB, self.prod, self.api)

    def test_first_release_starts_its_commits_at_what_serves(self) -> None:
        """With no record before it, the prod record's previous commit is the commit the primary domain serves."""
        artifacts = self.root / "artifacts.json"
        artifacts.write_text(json.dumps({"demo-web": "dpl_new"}))
        id_file = self.root / "release_id"
        args = _args(env="prod", artifacts_file=str(artifacts), release_id_file=str(id_file))
        self.assertEqual(rl.cmd_begin(args, WEB, self.prod, None), 0)
        record = rr.read_record(self.prod, id_file.read_text().strip())
        self.assertEqual(record["previous"]["commit"], OLD_COMMIT)
        self.assertIsNone(record["previous"]["release_id"])

    def test_promote_by_status(self) -> None:
        """The status success comes first; when Vercel assigns the domains, nothing else is called."""

        def status(repo, commit, context, state, description, token, **_):
            """Vercel's Deployment Check passes: it assigns the domains."""
            self.statuses.append((commit, context, state, description))
            self.vercel.aliases = {DOMAIN: "dpl_new"}

        with mock.patch.object(rvc, "post_status", side_effect=status):
            failures = rl.vercel_promote(WEB, self.api, {"demo-web": "dpl_new"}, self.prod, sleep=lambda _: None)
        self.assertEqual(failures, [])
        self.assertEqual(self.statuses[-1][:3], (COMMIT, "orbit-release/demo-web", "success"))
        self.assertEqual(self.vercel.calls("POST", "/v10/"), [])

    def test_promote_by_api_after_a_rollback_or_a_stuck_alias(self) -> None:
        """Vercel's hold after a kit rollback (auto-assignment off) or domains that do not move: promote by API."""
        self.prod.write("latest.json", {"release_id": "r1", "commit": OLD_COMMIT, "kind": "rollback"}, 0)
        self.vercel.project["autoAssignCustomDomains"] = False
        self.assertEqual(rl.vercel_promote(WEB, self.api, {"demo-web": "dpl_new"}, self.prod, sleep=lambda _: None), [])
        self.assertEqual(self.vercel.calls("POST", "/v10/"), ["/v10/projects/prj_1/promote/dpl_new"])
        self.vercel.aliases = {DOMAIN: "dpl_old"}
        self.vercel.requests.clear()
        ticks = iter(range(0, 1000, 20))
        self.assertEqual(
            rl.vercel_promote(
                WEB, self.api, {"demo-web": "dpl_new"}, self.prod, sleep=lambda _: None, clock=lambda: next(ticks)
            ),
            [],
        )
        self.assertEqual(self.vercel.calls("POST", "/v10/"), ["/v10/projects/prj_1/promote/dpl_new"])

    def test_promote_rechecks_the_hold(self) -> None:
        """A dashboard rollback after preflight stops the promote before the status: the kit never undoes it."""
        self.vercel.project["autoAssignCustomDomains"] = False
        failures = rl.vercel_promote(WEB, self.api, {"demo-web": "dpl_new"}, self.prod, sleep=lambda _: None)
        self.assertEqual(len(failures), 1)
        self.assertIn("did not record", failures[0])
        self.assertEqual(self.statuses, [])
        self.assertEqual(self.vercel.calls("POST", "/v10/"), [])

    def test_promote_stops_when_github_refuses_the_status(self) -> None:
        """A refused status is a failed promote, and the kit never promotes around the Deployment Check."""
        with mock.patch.object(rvc, "post_status", side_effect=rvc.VercelError("GitHub refused")):
            failures = rl.vercel_promote(WEB, self.api, {"demo-web": "dpl_new"}, self.prod, sleep=lambda _: None)
        self.assertEqual(len(failures), 1)
        self.assertEqual(self.vercel.calls("POST", "/v10/"), [])

    def test_prod_record_and_deploy_check(self) -> None:
        """begin snapshots the deployment that served (the rollback target); the deploy check reads the domains."""
        artifacts = self.root / "artifacts.json"
        artifacts.write_text(json.dumps({"demo-web": "dpl_new"}))
        id_file = self.root / "release_id"
        args = _args(artifacts_file=str(artifacts), release_id_file=str(id_file))
        self.assertEqual(rl.cmd_begin(args, WEB, self.prod, None), 0)
        release_id = id_file.read_text().strip()
        record = rr.read_record(self.prod, release_id)
        self.assertEqual(record["previous"]["workloads"]["vercel/demo-web"]["deployment"], "dpl_old")
        self.assertEqual(record["previous"]["workloads"]["vercel/demo-web"]["commit"], OLD_COMMIT)
        self.vercel.aliases = {DOMAIN: "dpl_new"}
        with mock.patch.object(rr, "release_lock"):
            self.assertEqual(rl.cmd_finish(_args(release_id=release_id), WEB, self.prod, None), 0)
        with mock.patch.object(rv, "check_http_public", return_value=rv.Check("health", "passed", ["ok"])):
            self.assertEqual(rl.cmd_check(_args(release_id=release_id), WEB, self.prod, None), rv.EXIT_OK)
        self.vercel.aliases = {DOMAIN: "dpl_old"}
        with mock.patch.object(rv, "check_http_public", return_value=rv.Check("health", "passed", ["ok"])):
            self.assertEqual(rl.cmd_check(_args(release_id=release_id), WEB, self.prod, None), rv.EXIT_FAILED)
        self.assertIn("serves dpl_old", " ".join(rr.read_record(self.prod, release_id)["verdict"]["reasons"]))


# --------------------------------------------------------------------------- rollback


class VercelRollbackTest(VercelCase):
    def _release(self) -> dict:
        """A deployed prod release of dpl_new that replaced dpl_old."""
        record = {
            "release_id": "20261023T110000Z-ccccccccc",
            "kind": "promote",
            "commit": COMMIT,
            "status": "deployed",
            "artifacts": {"demo-web": "dpl_new"},
            "previous": {
                "release_id": None,
                "commit": OLD_COMMIT,
                "workloads": {"vercel/demo-web": {"deployment": "dpl_old"}},
            },
            "times": {"deploy_started": "2026-10-23T11:00:00Z", "deploy_finished": "2026-10-23T11:01:00Z"},
        }
        self.vercel.aliases = {DOMAIN: "dpl_new"}
        return record

    def test_plan_and_apply(self) -> None:
        """The domains go back to the deployment that served before; Vercel then holds the project."""
        latest = self._release()
        steps, problems, notes = rl.vercel_rollback_steps(WEB, self.api, {"workloads": latest["previous"]["workloads"]})
        self.assertEqual(problems, [])
        self.assertEqual((steps[0].current, steps[0].target, steps[0].action), ("dpl_new", "dpl_old", "set"))
        self.assertIn("auto-assignment", notes[0])
        plan = rb.Plan(
            from_release=latest["release_id"],
            from_commit=COMMIT,
            target_release=None,
            target_commit=OLD_COMMIT,
            target_images={},
            steps=steps,
            not_rolled_back=[],
            notes=notes,
            problems=problems,
        )
        self.assertEqual(rl.apply_rollback(WEB, plan, None), [])
        self.assertEqual(self.vercel.calls("POST", "/v9/"), ["/v9/projects/prj_1/rollback/dpl_old"])
        after = rl.snapshot_vercel(WEB, self.api)
        self.assertEqual(rl.check_rollback(WEB, plan, after).status, "passed")
        self.assertFalse(after["vercel/demo-web"]["auto_assign"])

    def test_apply_marks_the_removed_commit(self) -> None:
        """The rollback turns the removed commit's status to failure (a promote or an earlier rollback's target),
        never the target's."""
        latest = self._release()
        rr.create_record(self.prod, dict(latest), "deploy_started")
        target = {"release_id": None, "commit": OLD_COMMIT, "workloads": latest["previous"]["workloads"]}
        args = argparse.Namespace(build_id="rb1", target="previous", dry_run=False, expect_latest="")
        with (
            mock.patch.object(rb, "assert_alone"),
            mock.patch.object(rl, "resolve_target", return_value=(latest, target)),
            mock.patch.object(rb, "create_rollback_record", return_value={"release_id": "rb"}),
            mock.patch.object(rb, "finish_rollback", return_value=0) as finish,
        ):
            self.assertEqual(rl._rollback_apply(args, WEB, self.prod, None), 0)
            self.assertEqual(self.statuses[-1][:3], (COMMIT, "orbit-release/demo-web", "failure"))
            self.assertEqual(finish.call_args.args[7][0].status, "passed")
            count = len(self.statuses)
            self.vercel.aliases = {DOMAIN: "dpl_new"}
            rollback = {**latest, "kind": "rollback", "commit": OLD_COMMIT}
            older = {
                "release_id": "r0",
                "commit": "e" * 40,
                "workloads": {"vercel/demo-web": {"deployment": "dpl_new"}},
            }
            with mock.patch.object(rl, "resolve_target", return_value=(rollback, older)):
                rl._rollback_apply(args, WEB, self.prod, None)
            self.assertEqual(self.statuses[-1][:3], (OLD_COMMIT, "orbit-release/demo-web", "failure"))
            count = len(self.statuses)
            same = {**older, "commit": OLD_COMMIT}
            with mock.patch.object(rl, "resolve_target", return_value=(rollback, same)):
                rl._rollback_apply(args, WEB, self.prod, None)
            self.assertEqual(len(self.statuses), count)

    def test_rollback_past_several_releases_marks_each_removed_commit(self) -> None:
        """A window that judged releases A and B together rolls back past A: both commits get failure, not the
        target's, and a chain that ends without a target release stops at the first record."""
        a_commit, b_commit = "a" * 40, NEWER_COMMIT
        first = {
            "release_id": "r1",
            "kind": "promote",
            "commit": a_commit,
            "status": "deployed",
            "previous": {"release_id": "r0", "commit": OLD_COMMIT},
        }
        second = {
            "release_id": "r2",
            "kind": "promote",
            "commit": b_commit,
            "status": "deployed",
            "previous": {"release_id": "r1", "commit": a_commit},
        }
        zero = {"release_id": "r0", "kind": "promote", "commit": OLD_COMMIT, "status": "deployed", "previous": {}}
        for record in (zero, first, second):
            rr.create_record(self.prod, dict(record), "deploy_started")
        target = {"release_id": "r0", "commit": OLD_COMMIT}
        self.assertEqual(rl.removed_commits(self.prod, second, target), [b_commit, a_commit])
        self.assertEqual(rl.removed_commits(self.prod, second, {"release_id": "r1", "commit": a_commit}), [b_commit])
        self.assertEqual(
            rl.removed_commits(self.prod, second, {"release_id": None, "commit": "f" * 40}),
            [b_commit, a_commit, OLD_COMMIT],
        )
        # A long chain is walked to its target, with no cap.
        previous = "r0"
        for number in range(1, 31):
            rr.create_record(
                self.prod,
                {
                    "release_id": f"s{number:02d}",
                    "kind": "promote",
                    "commit": f"{number:040d}",
                    "status": "deployed",
                    "previous": {"release_id": previous},
                },
                "deploy_started",
            )
            previous = f"s{number:02d}"
        chain = rl.removed_commits(self.prod, rr.read_record(self.prod, "s30"), target)
        self.assertEqual(len(chain), 30)
        self.assertEqual(chain[-1], f"{1:040d}")
        self.vercel.aliases = {DOMAIN: "dpl_new"}
        args = argparse.Namespace(build_id="rb1", target="previous-of:r1", dry_run=False, expect_latest="")
        with (
            mock.patch.object(rb, "assert_alone"),
            mock.patch.object(
                rl,
                "resolve_target",
                return_value=(second, {**target, "workloads": {"vercel/demo-web": {"deployment": "dpl_old"}}}),
            ),
            mock.patch.object(rb, "create_rollback_record", return_value={"release_id": "rb"}),
            mock.patch.object(rb, "finish_rollback", return_value=0),
        ):
            self.assertEqual(rl._rollback_apply(args, WEB, self.prod, None), 0)
        failed = [(commit, state) for commit, _, state, _ in self.statuses]
        self.assertEqual(failed, [(b_commit, "failure"), (a_commit, "failure")])

    def test_any_domain_off_target_makes_the_call(self) -> None:
        """The primary domain already serves the target but another does not: the rollback still runs."""
        self.vercel.aliases = {DOMAIN: "dpl_old", "www.demo.example.com": "dpl_new"}
        two = rl.parse_light(
            vercel_data(vercel={**vercel_data()["vercel"], "domains": [DOMAIN, "www.demo.example.com"]})
        )
        steps, problems, _ = rl.vercel_rollback_steps(two, self.api, {"artifacts": {"demo-web": "dpl_old"}})
        self.assertEqual((steps[0].action, problems), ("set", []))

    def test_expired_target_blocks(self) -> None:
        """A target deployment that Vercel no longer has blocks the rollback before any change."""
        self._release()
        del self.vercel.deployments["dpl_old"]
        _, problems, _ = rl.vercel_rollback_steps(WEB, self.api, {"artifacts": {"demo-web": "dpl_old"}})
        self.assertEqual(len(problems), 1)
        self.assertIn("pick another --to", problems[0])
        _, problems, _ = rl.vercel_rollback_steps(WEB, self.api, {})
        self.assertIn("recorded no deployment", problems[0])


# --------------------------------------------------------------------------- the window


class VercelWindowTest(VercelCase):
    def setUp(self) -> None:
        """A deployed release of dpl_new at NOW - 20 min, the previous deployment dpl_old."""
        super().setUp()
        self.record = {
            "release_id": "20261023T114000Z-ccccccccc",
            "kind": "promote",
            "commit": COMMIT,
            "status": "deployed",
            "artifacts": {"demo-web": "dpl_new"},
            "commits": [{"sha": COMMIT, "title": "Change the page (#7)", "pr": 7, "author": "dev"}],
            "previous": {
                "release_id": None,
                "commit": OLD_COMMIT,
                "workloads": {"vercel/demo-web": {"deployment": "dpl_old"}},
            },
            "times": {"deploy_started": "2026-10-23T11:40:00Z", "deploy_finished": "2026-10-23T11:40:00Z"},
            "verdict": {"status": "pending"},
        }
        self.vercel.aliases = {DOMAIN: "dpl_new"}
        self.ok = mock.patch.object(rv, "check_http_public", return_value=rv.Check("health", "passed", ["ok"]))
        self.ok.start()

    def tearDown(self) -> None:
        """Stop the smoke patch."""
        self.ok.stop()
        super().tearDown()

    def _traffic(self, dpl: str, start: datetime, count: int, errors: int) -> None:
        """count requests of a deployment from start on, errors of them 5xx."""
        for index in range(count):
            self.vercel.rows.append(log_row(dpl, start + timedelta(seconds=index), 500 if index < errors else 200))

    def test_quiet_release_passes(self) -> None:
        """No new line, a 5xx share like before, the smoke checks pass: nothing fails."""
        self._traffic("dpl_old", NOW - timedelta(minutes=30), 100, 1)
        self._traffic("dpl_new", NOW - timedelta(minutes=15), 100, 1)
        result = rl.evaluate_vercel_window(WEB, self.prod, self.record, NOW)
        self.assertEqual(result["regressions"], [])
        self.assertEqual(result["parts"]["logs"]["status"], "checked")
        self.assertEqual(result["parts"]["5xx"]["status"], "insufficient")
        self.assertEqual(result["parts"]["domains"]["status"], "checked")

    def test_5xx_share_regression_needs_runtime_code(self) -> None:
        """A 5xx share far above the previous deployment's fails a release that changed runtime code only."""
        self._traffic("dpl_old", NOW - timedelta(minutes=30), 100, 1)
        self._traffic("dpl_new", NOW - timedelta(minutes=15), 100, 30)
        # Skew Protection: the old deployment still serves a few old clients; they never count against the release.
        self._traffic("dpl_old", NOW - timedelta(minutes=10), 20, 20)
        with mock.patch.object(rv, "runtime_change", return_value=(True, "it changes app/page.tsx")):
            result = rl.evaluate_vercel_window(WEB, self.prod, self.record, NOW)
        self.assertEqual(result["parts"]["5xx"]["status"], "exceeded")
        self.assertIn("5xx 30/100", result["regressions"][0]["text"])
        with mock.patch.object(rv, "runtime_change", return_value=(False, "docs only")):
            result = rl.evaluate_vercel_window(WEB, self.prod, self.record, NOW)
        self.assertEqual(result["regressions"], [])
        self.assertIn("does not roll back", result["warnings"][0]["text"])

    def test_new_error_line_is_a_problem(self) -> None:
        """An error line the release's deployment writes again and again, unknown to the baseline, is a problem."""
        known = {"level": "error", "message": "Error: upstream timeout after 30 s"}
        new = {"level": "error", "message": "TypeError: Cannot read properties of undefined (reading 'plan')"}
        self.vercel.rows.append(log_row("dpl_old", NOW - timedelta(hours=3), 200, [known]))
        for minute in range(5):
            when = NOW - timedelta(minutes=minute + 1)
            self.vercel.rows.append(log_row("dpl_new", when, 500, [new]))
            self.vercel.rows.append(log_row("dpl_new", when, 200, [known]))
        result = rl.evaluate_vercel_window(WEB, self.prod, self.record, NOW)
        texts = [problem["text"] for problem in result["problems"]]
        self.assertEqual(len(texts), 1)
        self.assertIn("Cannot read properties", texts[0])

    def test_failed_smoke_check_and_moved_domain(self) -> None:
        """A failing smoke check is a regression. When the domains serve another deployment, nothing is judged."""
        with mock.patch.object(rv, "check_http_public", return_value=rv.Check("health", "failed", ["GET: 502"])):
            result = rl.evaluate_vercel_window(WEB, self.prod, self.record, NOW)
            self.assertEqual([item["kind"] for item in result["regressions"]], ["check"])
            self.assertTrue(result["judge"])
            self.vercel.aliases = {DOMAIN: "dpl_other"}
            result = rl.evaluate_vercel_window(WEB, self.prod, self.record, NOW)
        self.assertEqual(result["regressions"], [])
        self.assertEqual(result["parts"]["domains"]["status"], "inconclusive")
        self.assertEqual(result["parts"]["smoke"]["status"], "inconclusive")
        self.assertFalse(result["judge"])
        self.assertFalse(result["complete"])
        self.assertFalse(rl.window_checked(result))

    def test_every_part_decides_checked(self) -> None:
        """An unreadable 5xx read makes the checkpoint inconclusive even when the logs were read."""
        self.assertTrue(rl.window_checked({"parts": {"logs": {"status": "checked"}}}))
        self.assertFalse(rl.window_checked({"parts": {"logs": {"status": "checked"}}, "complete": False}))
        with mock.patch.object(rvc, "request_counts", side_effect=rvc.VercelError("HTTP 500")):
            result = rl.evaluate_vercel_window(WEB, self.prod, self.record, NOW)
        self.assertEqual(result["parts"]["logs"]["status"], "checked")
        self.assertEqual(result["parts"]["5xx"]["status"], "inconclusive")
        self.assertFalse(result["complete"])

    def test_judge_share(self) -> None:
        """The share rule: minimum samples, a baseline, the bound, and an upper bound from an incomplete read."""
        limit = rvc.ShareLimit()
        old = rvc.Requests("dpl_old", total=200, errors=2)
        self.assertEqual(rvc.judge_share(rvc.Requests("dpl_new", 200, 3), old, limit)[0], "insufficient")
        self.assertEqual(rvc.judge_share(rvc.Requests("dpl_new", 200, 6), old, limit)[0], "ok")
        self.assertEqual(rvc.judge_share(rvc.Requests("dpl_new", 200, 20), old, limit)[0], "exceeded")
        self.assertEqual(rvc.judge_share(rvc.Requests("dpl_new", 200, 20), None, limit)[0], "no_baseline")
        partial = rvc.Requests("dpl_new", 2000, 300, complete=False)
        self.assertEqual(rvc.judge_share(partial, old, limit)[0], "inconclusive")
        self.assertEqual(rvc.judge_share(rvc.Requests("dpl_new", 2000, 10, complete=False), old, limit)[0], "ok")
        capped_old = rvc.Requests("dpl_old", total=2000, errors=1000, complete=False)
        self.assertEqual(rvc.judge_share(rvc.Requests("dpl_new", 100, 30), capped_old, limit)[0], "insufficient")
        capped_new = rvc.Requests("dpl_new", 100, 30, errors_complete=False)
        self.assertEqual(rvc.judge_share(capped_new, old, limit)[0], "inconclusive")

    def test_incomplete_baseline_judges_nothing(self) -> None:
        """A baseline read that hits its page limit never lets old lines look new."""
        scope = rs.Scope([self.record], [], [(NOW - timedelta(hours=24), NOW - timedelta(minutes=20))], (NOW, NOW), NOW)
        with mock.patch.object(rvc, "BASELINE_MAX_PAGES", 1):
            self.vercel.rows = [
                log_row("dpl_old", NOW - timedelta(hours=2, seconds=index), 200, [{"level": "error", "message": "x"}])
                for index in range(60)
            ]
            problems, notes, judged = rvc.new_error_problems(self.api, "prj_1", ["dpl_new"], scope, NOW, min_count=1)
        self.assertEqual((problems, judged), ([], False))
        self.assertIn("baseline read stopped", notes[0])


# --------------------------------------------------------------------------- review fixes of the adoption drafts


class VercelReviewFixTest(VercelCase):
    """One test per finding: chained rollback target, unchecked verdicts, human rollbacks, capped baselines,
    manual cancels and retries of a live commit."""

    def _record(self, release_id: str, commit: str, dpl: str, previous: dict, **extra) -> dict:
        """A deployed prod release record, written to the prod store."""
        record = {
            "schema_version": 1,
            "service": "demo-web",
            "environment": "prod",
            "release_id": release_id,
            "kind": "promote",
            "commit": commit,
            "status": "deployed",
            "artifacts": {"demo-web": dpl},
            "commits": [
                {"sha": commit, "title": f"Change {commit[:4]} (#{len(release_id)})", "pr": None, "author": "dev"}
            ],
            "previous": previous,
            "times": {"deploy_started": "2026-10-23T11:40:00Z", "deploy_finished": "2026-10-23T11:40:30Z"},
            "verdict": {"status": "pending"},
            "checks": {},
            **extra,
        }
        rr.create_record(self.prod, record, "deploy_started")
        return record

    def test_1_chained_5xx_failure_rolls_back_past_the_oldest_release(self) -> None:
        """A 5xx failure judged over releases A and B targets what ran before A, and the hold names both."""
        a = self._record(
            "20261023T113000Z-aaaaaaaaa",
            "a" * 40,
            "dpl_a",
            {"release_id": None, "commit": OLD_COMMIT, "workloads": {"vercel/demo-web": {"deployment": "dpl_old"}}},
        )
        b = self._record(
            "20261023T114000Z-ccccccccc",
            COMMIT,
            "dpl_new",
            {
                "release_id": a["release_id"],
                "commit": a["commit"],
                "workloads": {"vercel/demo-web": {"deployment": "dpl_a"}},
            },
        )
        rr.point_latest(self.prod, b)
        regression = {"kind": "metric", "name": "vercel_5xx_share", "text": "5xx share above the limit"}
        scope = {"releases": [b["release_id"], a["release_id"]]}
        with mock.patch.object(rv, "suspected_prs", return_value=[]) as ranked:
            saved = rl._fail(WEB, self.prod, b, "window_15m", [regression], {"scope": scope, "problems": []})
        self.assertEqual([item["sha"] for item in ranked.call_args.args[1]["commits"]], ["a" * 40, COMMIT])
        suggestion = saved["rollback_suggestion"]
        self.assertEqual(suggestion["target"]["rollback_to"], f"previous-of:{a['release_id']}")
        self.assertEqual(suggestion["target"]["commit"], OLD_COMMIT)
        self.assertIn(f"--to previous-of:{a['release_id']}", suggestion["how"][0])
        _, target = rl.resolve_target(self.prod, suggestion["target"]["rollback_to"])
        self.assertEqual(rl.vercel_target(WEB, target), "dpl_old")
        # One release judged alone keeps its own predecessor.
        single = rl._fail(WEB, self.prod, a, "window_15m", [regression], {"scope": {"releases": [a["release_id"]]}})
        self.assertNotIn("rollback_to", single["rollback_suggestion"]["target"])

    def _watch(self, result: dict) -> dict:
        """Run the last checkpoint of the watch with this window result; return the saved record."""
        record = self._record(
            "20261023T114000Z-ccccccccc",
            COMMIT,
            "dpl_new",
            {"release_id": None, "commit": OLD_COMMIT, "workloads": {"vercel/demo-web": {"deployment": "dpl_old"}}},
        )
        with (
            mock.patch.object(rl, "evaluate_window", return_value=result),
            mock.patch.object(rl, "wait_for_deploys", return_value=None),
            mock.patch.object(rl, "replaced_by", return_value=None),
            mock.patch.object(rv, "builds_since_rollback", return_value=[]),
            mock.patch.object(rv, "require_owner", return_value=True),
            mock.patch.object(rv, "start_rollback") as rollback,
            mock.patch.object(rv, "post_warnings"),
        ):
            rl.cmd_watch(_args(release_id=record["release_id"], checkpoints="15"), WEB, self.prod, None)
        self.rollback = rollback
        return rr.read_record(self.prod, record["release_id"])

    def _result(self, **parts) -> dict:
        """A window result with these part statuses (logs checked by default)."""
        statuses = {"logs": "checked", "5xx": "ok", "smoke": "checked", "domains": "checked", **parts}
        return {
            "scope": {"releases": ["20261023T114000Z-ccccccccc"]},
            "parts": {name: {"status": status, "detail": ""} for name, status in statuses.items()},
            "problems": [],
            "regressions": [],
            "warnings": [],
            "complete": all(status != "inconclusive" for status in statuses.values()),
            "judge": statuses["domains"] == "checked",
        }

    def test_2_unfinished_5xx_read_or_moved_domain_leaves_the_release_unchecked(self) -> None:
        """A failed or capped 5xx read, or a domain on another deployment: final verdict inconclusive, unchecked."""
        for parts in ({"5xx": "inconclusive"}, {"domains": "inconclusive", "smoke": "inconclusive"}):
            self.slack.clear()
            record = self._watch(self._result(**parts))
            self.assertEqual(record["verdict"]["status"], "inconclusive", parts)
            self.assertTrue(record["verdict"]["unchecked"], parts)
            self.assertFalse(any(":white_check_mark:" in line for line in self.slack), self.slack)
            self.prod = rr.LocalStore(self.root / f"prod-{len(self.slack)}-{list(parts)[0]}")
        # A capped baseline read makes the 5xx part unfinished.
        self.vercel.aliases = {DOMAIN: "dpl_new"}
        record = self._record(
            "20261023T114000Z-ccccccccc",
            COMMIT,
            "dpl_new",
            {"release_id": None, "commit": OLD_COMMIT, "workloads": {"vercel/demo-web": {"deployment": "dpl_old"}}},
        )
        capped = rvc.Requests("dpl_old", total=2000, errors=40, complete=False)
        with (
            mock.patch.object(rvc, "request_counts", side_effect=[rvc.Requests("dpl_new", 200, 20), capped]),
            mock.patch.object(rv, "check_http_public", return_value=rv.Check("health", "passed", ["ok"])),
        ):
            result = rl.evaluate_vercel_window(WEB, self.prod, record, NOW)
        self.assertEqual(result["parts"]["5xx"]["status"], "inconclusive")
        self.assertFalse(rl.window_checked(result))
        # A passed check keeps its passed verdict.
        self.prod = rr.LocalStore(self.root / "prod-passed")
        self.assertEqual(self._watch(self._result())["verdict"]["status"], "passed")

    def test_3_a_human_rollback_is_never_undone(self) -> None:
        """Smoke failures on domains that serve another deployment never start a rollback; the rollback build
        refuses an automatic rollback when the domains moved."""
        result = self._result(domains="inconclusive", smoke="inconclusive")
        result["regressions"] = [{"kind": "check", "name": "health", "text": "health: GET 502"}]
        record = self._watch(result)
        self.rollback.assert_not_called()
        self.assertNotEqual(record["verdict"]["status"], "failed")
        latest = {
            "release_id": "r1",
            "commit": COMMIT,
            "kind": "promote",
            "artifacts": {"demo-web": "dpl_new"},
            "previous": {"workloads": {"vercel/demo-web": {"deployment": "dpl_old"}}},
        }
        target = {"release_id": None, "commit": OLD_COMMIT, "workloads": {"vercel/demo-web": {"deployment": "dpl_old"}}}
        two = rl.parse_light(
            vercel_data(vercel={**vercel_data()["vercel"], "domains": [DOMAIN, "www.demo.example.com"]})
        )
        mixed = {DOMAIN: "dpl_new", "www.demo.example.com": "dpl_old"}
        # A deployed release whose domains moved to its predecessor: a human did it (no kit evidence).
        for aliases in (mixed, {DOMAIN: "dpl_old", "www.demo.example.com": "dpl_old"}):
            self.vercel.aliases = aliases
            with self.assertRaises(rr.RecordError) as raised:
                rl.vercel_serving_guard(two, self.api, {**latest, "status": "deployed"})
            self.assertIn("human moved it", str(raised.exception))
        # Kit evidence: a failed promote explains the predecessor; a recorded rollback attempt explains its target.
        self.vercel.aliases = mixed
        rl.vercel_serving_guard(two, self.api, {**latest, "status": "deploy_failed"})
        self.vercel.aliases = {DOMAIN: "dpl_old", "www.demo.example.com": "dpl_old"}
        attempted = {**latest, "status": "deployed", rl.ROLLBACK_ATTEMPTS: [{"deployment": "dpl_old"}]}
        rl.vercel_serving_guard(two, self.api, attempted)
        self.vercel.aliases = {DOMAIN: "dpl_new", "www.demo.example.com": "dpl_other"}
        with self.assertRaises(rr.RecordError):
            rl.vercel_serving_guard(two, self.api, attempted)
        args = argparse.Namespace(build_id="rb1", target="previous", dry_run=False, expect_latest="r1")
        self.vercel.aliases = {DOMAIN: "dpl_other"}
        with (
            mock.patch.object(rb, "assert_alone"),
            mock.patch.object(rb, "check_expect_latest"),
            mock.patch.object(rl, "resolve_target", return_value=(latest, target)),
            self.assertRaises(rr.RecordError),
        ):
            rl._rollback_apply(args, WEB, self.prod, None)
        self.assertEqual(self.vercel.calls("POST", "/v9/"), [])
        # A partial promote (one domain moved, one not) still rolls back on its own, and records the attempt first.
        failed = self._record(
            "20261023T115000Z-ccccccccc", COMMIT, "dpl_new", latest["previous"], status="deploy_failed"
        )
        self.vercel.aliases = {DOMAIN: "dpl_new", "www.demo.example.com": "dpl_old"}
        with (
            mock.patch.object(rb, "assert_alone"),
            mock.patch.object(rb, "check_expect_latest"),
            mock.patch.object(rl, "resolve_target", return_value=(failed, target)),
            mock.patch.object(rb, "create_rollback_record", return_value={"release_id": "rb"}),
            mock.patch.object(rb, "finish_rollback", return_value=0),
        ):
            self.assertEqual(rl._rollback_apply(args, two, self.prod, None), 0)
        self.assertEqual(self.vercel.calls("POST", "/v9/"), ["/v9/projects/prj_1/rollback/dpl_old"])
        attempts = rr.read_record(self.prod, failed["release_id"])[rl.ROLLBACK_ATTEMPTS]
        self.assertEqual([(item["deployment"], item["build"]) for item in attempts], [("dpl_old", "rb1")])

    def test_4_capped_baseline_is_insufficient(self) -> None:
        """A baseline read that stopped at its page limit computes no share: insufficient, never ok or exceeded."""
        limit = rvc.ShareLimit()
        new = rvc.Requests("dpl_new", 1000, 100)
        for old in (
            rvc.Requests("dpl_old", 2000, 100, complete=False),
            rvc.Requests("dpl_old", 2000, 100, errors_complete=False),
        ):
            status, line = rvc.judge_share(new, old, limit)
            self.assertEqual(status, "insufficient")
            self.assertIn("no share computed", line)

    def test_5_only_the_ignored_build_step_counts_as_not_affected(self) -> None:
        """A cancel without the Ignored Build Step mark fails the status and the build; with it, success."""
        self.vercel.deployments["dpl_new"]["readyState"] = "CANCELED"
        args = _args(out_dir=str(self.root))
        self.assertEqual(rl.cmd_vercel_wait(args, WEB, self.prod, None), rv.EXIT_ERROR)
        self.assertEqual(self.statuses[-1][2:], ("failure", "Vercel deployment dpl_new is CANCELED"))
        self.vercel.deployments["dpl_new"]["errorLink"] = "https://vercel.com/docs/platform/projects#ignored-build-step"
        self.assertEqual(rl.cmd_vercel_wait(args, WEB, self.prod, None), rvc.EXIT_NOT_AFFECTED)
        self.assertEqual(self.statuses[-1][2], "success")

    def test_6_a_retry_of_the_live_commit_keeps_success(self) -> None:
        """A dev build re-run for the commit prod serves keeps the success status; an older commit still fails."""
        release = self._record(
            "20261023T114000Z-ccccccccc",
            COMMIT,
            "dpl_new",
            {"release_id": None, "commit": OLD_COMMIT},
        )
        rr.point_latest(self.prod, release)
        self.vercel.aliases = {DOMAIN: "dpl_new"}
        stale = {
            "verdict": {"status": "passed"},
            "promote": {"status": "stale", "reason": f"prod already runs {COMMIT[:9]}"},
        }
        self.dev.write(rg.dev_commit_index(COMMIT), stale, 0)
        self.dev.write(rg.dev_commit_index(OLD_COMMIT), stale, 0)
        with mock.patch.object(rl, "open_store", return_value=self.dev):
            rl.cmd_vercel_status(_args(from_dev_index=True), WEB, self.prod, None)
            self.assertEqual(
                self.statuses[-1][2:], ("success", f"already live in prod release {release['release_id']}")
            )
            rl.cmd_vercel_status(_args(commit=OLD_COMMIT, from_dev_index=True), WEB, self.prod, None)
            self.assertEqual((self.statuses[-1][0], self.statuses[-1][2]), (OLD_COMMIT, "failure"))
        skip = self.root / "skip"
        skip.write_text(f"stale: prod already runs {COMMIT[:9]}, which contains {COMMIT[:9]}.\n")
        rl.cmd_vercel_status(_args(from_skip_file=str(skip)), WEB, self.prod, None)
        self.assertEqual(self.statuses[-1][2], "success")
        # After a rollback in the dashboard the record is unchanged, but the domains serve another deployment.
        self.vercel.aliases = {DOMAIN: "dpl_old"}
        rl.cmd_vercel_status(_args(from_skip_file=str(skip)), WEB, self.prod, None)
        self.assertEqual(self.statuses[-1][2], "failure")


if __name__ == "__main__":
    unittest.main()
