"""Tests of kind railway (release_railway.py), kind static_site (release_static.py), their parts of
release_light.py and release_kit.py, and the forward-only hold of release_gate.py.

Standard library only: `python3 -m unittest discover -s release -p 'test_*.py'`.
Every Railway, GitHub and site call goes to an injected fake; the tokens are fake
strings. urllib.request.urlopen and subprocess.run raise in every test, so no
test reaches Railway, GitHub, Secret Manager, a site or a CLI.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
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
import release_railway as rwy  # noqa: E402
import release_record as rr  # noqa: E402
import release_rollback as rb  # noqa: E402
import release_static as rst  # noqa: E402
import release_verify as rv  # noqa: E402

PROJECT = "11111111-1111-4111-8111-111111111111"
SERVICE = "22222222-2222-4222-8222-222222222222"
PROD_ENV = "33333333-3333-4333-8333-333333333333"
DEV_ENV = "44444444-4444-4444-8444-444444444444"
PROD_TOKEN = "prod-token-not-real"
DEV_TOKEN = "dev-token-not-real"
COMMIT = "c" * 40
OLD_COMMIT = "b" * 40
FIX_COMMIT = "d" * 40
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=rr.UTC)


def railway_data(**overrides) -> dict:
    """A kind railway config of tier full: staging is the dev stage."""
    data = {
        "service": "demo-api",
        "title": "demo api",
        "kind": "railway",
        "tier": "full",
        "github_repo": "orbit-search/demo",
        "connected_repository": "orbit-search-demo",
        "railway": {
            "project_id": PROJECT,
            "service_id": SERVICE,
            "environments": {
                "dev": {
                    "name": "staging",
                    "environment_id": DEV_ENV,
                    "token_secret": "railway-token-demo-staging",
                    "url": "https://demo-staging.up.railway.app",
                },
                "prod": {
                    "name": "production",
                    "environment_id": PROD_ENV,
                    "token_secret": "railway-token-demo-production",
                    "url": "https://api.demo.example.com",
                },
            },
            "migrations": {"prefix": "backend/migrations/", "suffix": ".up.sql"},
            "version": {"path": "/health", "field": "version"},
            "log_settle_seconds": 0,
        },
        "triggers": {"dev": ["demo-api-release"], "prod": ["demo-api-promote"], "rollback": "demo-api-rollback"},
        "smoke": [{"type": "http_public", "name": "health", "url": "https://api.demo.example.com/health"}],
        "verify": {"checkpoints": [15, 60]},
        "code": {"source_prefixes": ["backend/"], "extensions": [".go"]},
    }
    data.update(overrides)
    return data


def direct_data() -> dict:
    """The same service in tier check_rollback: production only."""
    data = railway_data(tier="check_rollback", triggers={"prod": ["demo-api-release"], "rollback": "demo-api-rollback"})
    data["railway"] = {**data["railway"], "environments": {"prod": data["railway"]["environments"]["prod"]}}
    return data


API = rl.parse_light(railway_data())
DIRECT = rl.parse_light(direct_data())


def iso(when: datetime) -> str:
    """A Railway timestamp (nanoseconds)."""
    return when.strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z"


def deployment(
    uid: str, env: str, commit: str, status: str = "SUCCESS", created: datetime = NOW, image: str = ""
) -> dict:
    """A deployment as Railway returns it."""
    return {
        "id": uid,
        "status": status,
        "environmentId": env,
        "createdAt": iso(created),
        "statusUpdatedAt": iso(created),
        "canRedeploy": True,
        "canRollback": True,
        "staticUrl": None,
        "url": None,
        "meta": {"commitHash": commit, "imageDigest": image or f"sha256:{uid}", "reason": "deploy"},
    }


class FakeRailway:
    """Railway's GraphQL API for one project: deployments per environment, logs, HTTP logs. Records every call."""

    def __init__(self) -> None:
        """Production serves d_old (OLD_COMMIT); staging serves s_old."""
        self.calls: list[tuple[str, dict, str]] = []
        self.deployments = {
            "d_old": deployment("d_old", PROD_ENV, OLD_COMMIT, created=NOW - timedelta(days=1)),
            "s_old": deployment("s_old", DEV_ENV, OLD_COMMIT, created=NOW - timedelta(days=1)),
        }
        self.active = {PROD_ENV: "d_old", DEV_ENV: "s_old"}
        self.scopes = {PROD_TOKEN: PROD_ENV, DEV_TOKEN: DEV_ENV}
        self.auto = {PROD_ENV: False, DEV_ENV: False}
        self.logs: dict[str, list[dict]] = {}
        self.http: dict[str, list[dict]] = {}
        self.deploy_status = "SUCCESS"
        self.counter = 0

    def __call__(self, request: urllib.request.Request) -> tuple[int, bytes]:
        """Answer one GraphQL request."""
        body = json.loads(request.data)
        query, variables = body["query"], body["variables"]
        token = request.get_header("Project-access-token")
        name = query.split("(", 1)[0].split("{", 1)[0].split()[-1]
        self.calls.append((name, variables, token))
        if token not in self.scopes:
            return 401, b'{"errors": [{"message": "Not Authorized"}]}'
        env = self.scopes[token]
        data = self.answer(name, variables, env)
        return 200, json.dumps({"data": data}).encode()

    def answer(self, name: str, v: dict, env: str) -> dict:
        """The data of one operation."""
        if name == "projectToken":
            return {"projectToken": {"projectId": PROJECT, "environmentId": env}}
        if name == "deploy":
            self.counter += 1
            uid = f"new{self.counter}"
            self.deployments[uid] = deployment(
                uid, v["e"], v["c"], self.deploy_status, NOW + timedelta(seconds=self.counter)
            )
            if self.deploy_status == "SUCCESS":
                self.active[v["e"]] = uid
            return {"serviceInstanceDeployV2": uid}
        if name == "deployment":
            return {"deployment": self.deployments.get(v["id"])}
        if name == "deployments":
            items = [item for item in self.deployments.values() if item["environmentId"] == v["e"]]
            return {
                "deployments": {
                    "edges": [{"node": item} for item in items],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        if name == "cancel":
            self.deployments[v["id"]]["status"] = "REMOVED"
            return {"deploymentCancel": True}
        if name == "active":
            live = self.active.get(v["e"])
            return {"serviceInstance": {"activeDeployments": [self.deployments[live]] if live else []}}
        if name == "auto":
            return {
                "serviceInstanceAutoDeployStatus": {"enabled": self.auto[v["e"]], "canEnable": True, "reason": None}
            }
        if name == "redeploy":
            old = self.deployments[v["id"]]
            self.counter += 1
            uid = f"re{self.counter}"
            new = deployment(uid, old["environmentId"], old["meta"]["commitHash"], image=old["meta"]["imageDigest"])
            self.deployments[uid] = new
            self.active[old["environmentId"]] = uid
            return {"deploymentRedeploy": new}
        if name == "logs":
            start, end = rwy.parse_time(v["s"]), rwy.parse_time(v["e"])
            rows = [r for r in self.logs.get(v["d"], []) if start <= rwy.parse_time(r["timestamp"]) < end]
            if v.get("f") == "@level:error":
                rows = [r for r in rows if r["severity"] == "error"]
            return {"deploymentLogs": rows[-v["n"] :]}
        if name == "http":
            anchor = rwy.parse_time(v["a"])
            rows = sorted(
                (r for r in self.http.get(v["d"], []) if rwy.parse_time(r["timestamp"]) >= anchor),
                key=lambda r: r["timestamp"],
            )
            return {"httpLogs": rows[: v["n"]]}
        raise AssertionError(f"unexpected Railway operation {name}")

    def mutations(self) -> list[str]:
        """The names of the mutations sent."""
        return [name for name, _, _ in self.calls if name in ("deploy", "redeploy", "cancel")]


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
    }
    defaults.update(values)
    return argparse.Namespace(**defaults)


class RailwayCase(unittest.TestCase):
    """A fake Railway, recorded Slack lines, local stores, and no network."""

    def setUp(self) -> None:
        """Start the fakes and patches."""
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.railway = FakeRailway()
        self.slack: list[str] = []
        self.apis = {
            "dev": rwy.RailwayApi(lambda: DEV_TOKEN, http=self.railway, sleep=lambda _: None),
            "prod": rwy.RailwayApi(lambda: PROD_TOKEN, http=self.railway, sleep=lambda _: None),
        }

        def slack(text, blocks=None):
            """Record a Slack line."""
            self.slack.append(text + (json.dumps(blocks) if blocks else ""))
            return True

        self.patches = [
            mock.patch.dict(
                os.environ, {"RAILWAY_TOKEN_DEV": "", "RAILWAY_TOKEN_PROD": "", "GITHUB_TOKEN": "", "WEBHOOK_ORBIT": ""}
            ),
            mock.patch.object(urllib.request, "urlopen", side_effect=_no_network),
            mock.patch.object(subprocess, "run", side_effect=_no_network),
            mock.patch.object(rl, "railway_api", side_effect=lambda service, env: self.apis[env]),
            mock.patch.object(rl, "GoogleApi"),
            mock.patch.object(rr, "github_token", side_effect=guard.GuardError("no token")),
            mock.patch.object(rr, "build_info", return_value={"id": "b1"}),
            mock.patch.object(rr, "post_slack", side_effect=slack),
            mock.patch.object(rwy.time, "sleep", side_effect=lambda _: None),
            mock.patch.object(rl.time, "sleep", side_effect=lambda _: None),
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


class RailwayConfigTest(unittest.TestCase):
    def test_parse(self) -> None:
        """Tier full has a dev stage; tier check_rollback has none but rolls back and passes the gate."""
        self.assertTrue(API.has_gate and API.has_dev and API.auto_rollback)
        self.assertTrue(DIRECT.has_gate and DIRECT.auto_rollback)
        self.assertFalse(DIRECT.has_dev)
        self.assertIsNone(DIRECT.dev)
        self.assertEqual(dict(API.railway_tokens)["prod"], "railway-token-demo-production")
        self.assertEqual(API.migrations_prefix, ("backend/migrations/",))
        multi = rl.parse_light(
            railway_data(railway={**railway_data()["railway"], "migrations": {"prefix": ["src/store.js", "db/"]}})
        )
        self.assertEqual(multi.migrations_prefix, ("src/store.js", "db/"))
        self.assertEqual(
            rwy.migration_files([{"filename": "src/store.js", "status": "modified"}], multi.migrations_prefix, ""),
            ["src/store.js"],
        )
        self.assertEqual(API.dev.deploy_trigger, "demo-api-release")
        self.assertEqual(rl.dev_smoke(API)[0]["url"], "https://demo-staging.up.railway.app/health")
        self.assertEqual(rl.dev_smoke(DIRECT), [])

    def test_bad_configs(self) -> None:
        """Wrong tiers, environments that do not match the tier, a shared token, bad ids and smoke URLs."""
        envs = railway_data()["railway"]["environments"]
        same_secret = {**envs, "dev": {**envs["dev"], "token_secret": envs["prod"]["token_secret"]}}
        bad = [
            railway_data(tier="check_only"),
            railway_data(railway={**railway_data()["railway"], "environments": {"prod": envs["prod"]}}),
            railway_data(railway={**railway_data()["railway"], "environments": same_secret}),
            railway_data(railway={**railway_data()["railway"], "project_id": "demo"}),
            railway_data(smoke=[{"type": "http_public", "url": "https://other.example.com/health"}]),
            railway_data(smoke=[]),
            railway_data(deploy_triggers=["x"]),
            {**direct_data(), "triggers": {"dev": ["x"], "prod": ["y"]}},
            {**railway_data(), "kind": "static_site", "tier": "full"},
        ]
        for data in bad:
            with self.assertRaises(rc.ConfigError, msg=json.dumps(data)[:200]):
                rl.parse_light(data)

    def test_rendered_builds_post_nothing_to_slack(self) -> None:
        """No rendered Railway build posts to Slack (Nicholas, 2026-10-29); the reasons stay in the build log."""
        for service in (API, DIRECT):
            files = kit.rendered(service)
            posts = [line for text in files.values() for line in text.splitlines() if "curl " in line]
            self.assertEqual(posts, [], service.name)
            self.assertNotIn("hooks.slack.com", "".join(files.values()))

    def test_a_failed_deploy_posts_nothing(self) -> None:
        """A failed prod deploy posts nothing to Slack (tier full: the promote build; check_rollback: the merge
        build)."""
        import test_release_kit as tk

        for service, build in ((API, "promote"), (DIRECT, "release")):
            with self.subTest(service=build):
                text = kit.rendered(service)[f"release/services/demo-api.{build}.yaml"]
                self.assertIn('id: "notify-result"', text)
                tk.assert_no_slack_post(self, text)

    def test_rendering_ignores_release_ops_url(self) -> None:
        """The rendered builds link the fixed ops board: a local RELEASE_OPS_URL never makes check reject them."""
        for service in (API, DIRECT, DOCS):
            with self.subTest(service=service.name):
                with mock.patch.dict(os.environ, {"RELEASE_OPS_URL": ""}):
                    default = kit.rendered(service)
                with mock.patch.dict(os.environ, {"RELEASE_OPS_URL": "https://ops.example.com"}):
                    self.assertEqual(kit.rendered(service), default)

    def test_render(self) -> None:
        """Tier full renders the merge (dev), promote, window and rollback builds; check_rollback has no promote."""
        files = kit.rendered(API)
        self.assertEqual(
            sorted(files),
            sorted(
                f"release/services/demo-api.{name}.yaml"
                for name in ("promote", "release", "release-verify", "rollback")
            ),
        )
        release = files["release/services/demo-api.release.yaml"]
        self.assertIn("railway-deploy --env dev", release)
        self.assertIn("secrets/railway-token-demo-staging/versions/latest", release)
        self.assertNotIn("railway-token-demo-production", release)
        self.assertIn("request-promote", release)
        for name in ("promote", "release-verify", "rollback"):
            text = files[f"release/services/demo-api.{name}.yaml"]
            self.assertIn("secrets/railway-token-demo-production/versions/latest", text, name)
            self.assertNotIn("railway-token-demo-staging", text, name)
        self.assertIn("check-prod", files["release/services/demo-api.promote.yaml"])
        direct = kit.rendered(DIRECT)
        self.assertEqual(len(direct), 3)
        self.assertIn("check-direct", direct["release/services/demo-api.release.yaml"])
        self.assertIn('id: "release-record-begin"', direct["release/services/demo-api.release.yaml"])
        for text in [*files.values(), *direct.values()]:
            self.assertNotIn(PROD_TOKEN, text)
            self.assertNotIn("kubectl", text)

    def test_kit_files(self) -> None:
        """The new modules and their tests are pinned kit files."""
        for name in ("release_railway.py", "release_static.py", "test_release_railway.py"):
            self.assertIn(name, kit.KIT_FILES)


# --------------------------------------------------------------------------- the API


class RailwayApiTest(RailwayCase):
    def test_token_header_and_errors(self) -> None:
        """The project token goes in Project-Access-Token; a GraphQL error raises without the token in it."""
        self.assertEqual(self.apis["prod"].token_scope()["environmentId"], PROD_ENV)
        self.assertEqual(self.railway.calls[0][2], PROD_TOKEN)
        bad = rwy.RailwayApi(lambda: "wrong", http=self.railway, sleep=lambda _: None)
        with self.assertRaises(rwy.RailwayError) as raised:
            bad.token_scope()
        self.assertNotIn("wrong", str(raised.exception))
        self.assertIsInstance(raised.exception, rr.RecordError)
        answers = iter([(429, b""), (502, b""), (200, b'{"errors": [{"message": "boom"}]}')])
        flaky = rwy.RailwayApi(lambda: PROD_TOKEN, http=lambda _: next(answers), sleep=lambda _: None)
        with self.assertRaises(rwy.RailwayError) as raised:
            flaky.token_scope()
        self.assertIn("boom", str(raised.exception))

    def test_deploy_answer_lost_is_reconciled_not_sent_again(self) -> None:
        """A 502 after Railway started the deploy: the kit adopts that deployment and sends the mutation once."""
        lost = {"done": False}

        def http(request):
            status, raw = self.railway(request)
            if b"serviceInstanceDeployV2" in request.data and not lost["done"]:
                lost["done"] = True
                return 502, b""
            return status, raw

        api = rwy.RailwayApi(lambda: PROD_TOKEN, http=http, sleep=lambda _: None)
        self.assertEqual(api.deploy("svc", PROD_ENV, COMMIT), "new1")
        self.assertEqual(self.railway.mutations(), ["deploy"])

    def test_deploy_without_an_answer_and_no_new_deployment_raises(self) -> None:
        """A timeout before Railway ran it: no new deployment is listed, the kit raises and never sends it again."""

        def http(request):
            if b"serviceInstanceDeployV2" in request.data:
                self.railway.calls.append(("deploy", {}, PROD_TOKEN))
                raise TimeoutError("timed out")
            return self.railway(request)

        api = rwy.RailwayApi(lambda: PROD_TOKEN, http=http, sleep=lambda _: None)
        with self.assertRaisesRegex(rwy.RailwayError, "nothing was sent again"):
            api.deploy("svc", PROD_ENV, COMMIT)
        self.assertEqual(self.railway.mutations(), ["deploy"])

    def test_redeploy_answer_lost_is_reconciled(self) -> None:
        """A rollback redeploy whose answer is lost returns the new deployment of the old commit, sent once."""
        lost = {"done": False}

        def http(request):
            status, raw = self.railway(request)
            if b"deploymentRedeploy" in request.data and not lost["done"]:
                lost["done"] = True
                return 503, b""
            return status, raw

        api = rwy.RailwayApi(lambda: PROD_TOKEN, http=http, sleep=lambda _: None)
        found = api.redeploy("d_old", "svc", PROD_ENV)
        self.assertEqual(rwy.commit_of(found), OLD_COMMIT)
        self.assertNotEqual(found["id"], "d_old")
        self.assertEqual(self.railway.mutations(), ["redeploy"])

    def test_deploy_reply_cut_off_is_reconciled(self) -> None:
        """A reply cut off mid-body (IncompleteRead) after Railway started the deploy is adopted, sent once."""
        import http.client

        def http_cut(request):
            status, raw = self.railway(request)
            if b"serviceInstanceDeployV2" in request.data:
                raise http.client.IncompleteRead(raw[:5])
            return status, raw

        api = rwy.RailwayApi(lambda: PROD_TOKEN, http=http_cut, sleep=lambda _: None)
        self.assertEqual(api.deploy("svc", PROD_ENV, COMMIT), "new1")
        self.assertEqual(self.railway.mutations(), ["deploy"])

    def test_recovered_redeploy_keeps_its_id_when_the_last_read_fails(self) -> None:
        """After a lost redeploy answer, the recovered id is returned even when reading it fails."""
        state = {"lost": False}

        def http(request):
            if state["lost"] and b"query deployment(" in request.data:
                return 500, b""
            status, raw = self.railway(request)
            if b"deploymentRedeploy" in request.data:
                state["lost"] = True
                return 503, b""
            return status, raw

        api = rwy.RailwayApi(lambda: PROD_TOKEN, http=http, sleep=lambda _: None, attempts=1)
        found = api.redeploy("d_old", "svc", PROD_ENV)
        self.assertTrue(found["id"].startswith("re"))
        self.assertEqual(self.railway.mutations(), ["redeploy"])

    def test_reconcile_survives_a_failed_read(self) -> None:
        """A list read that fails after a lost deploy answer uses one attempt; the next read adopts the deployment."""
        state = {"lost": False, "failed_reads": 0}

        def http(request):
            if state["lost"] and b"query deployments(" in request.data and state["failed_reads"] < 1:
                state["failed_reads"] += 1
                return 500, b""
            status, raw = self.railway(request)
            if b"serviceInstanceDeployV2" in request.data:
                state["lost"] = True
                return 502, b""
            return status, raw

        api = rwy.RailwayApi(lambda: PROD_TOKEN, http=http, sleep=lambda _: None, attempts=1)
        self.assertEqual(api.deploy("svc", PROD_ENV, COMMIT), "new1")
        self.assertEqual((state["failed_reads"], self.railway.mutations()), (1, ["deploy"]))

    def test_redeploy_recovery_ignores_a_rebuild_of_the_same_commit(self) -> None:
        """After a lost redeploy answer, a new deployment of the old commit with another image is not adopted."""

        def http(request):
            if b"deploymentRedeploy" in request.data:
                self.railway.calls.append(("redeploy", {}, PROD_TOKEN))
                rebuild = deployment("rebuild", PROD_ENV, OLD_COMMIT, image="sha256:other")
                self.railway.deployments["rebuild"] = rebuild
                return 504, b""
            return self.railway(request)

        api = rwy.RailwayApi(lambda: PROD_TOKEN, http=http, sleep=lambda _: None)
        with self.assertRaisesRegex(rwy.RailwayError, "lists no new deployment"):
            api.redeploy("d_old", "svc", PROD_ENV)
        self.assertEqual(self.railway.mutations(), ["redeploy"])

    def test_redeploy_of_a_target_without_an_image_is_not_reconciled(self) -> None:
        """A target whose image is unknown: a lost answer stops; no new deployment is adopted."""
        self.railway.deployments["d_old"]["meta"]["imageDigest"] = ""

        def http(request):
            status, raw = self.railway(request)
            if b"deploymentRedeploy" in request.data:
                return 502, b""
            return status, raw

        api = rwy.RailwayApi(lambda: PROD_TOKEN, http=http, sleep=lambda _: None)
        with self.assertRaisesRegex(rwy.RailwayError, "nothing was sent again"):
            api.redeploy("d_old", "svc", PROD_ENV)
        self.assertEqual(self.railway.mutations(), ["redeploy"])

    def test_cancel_is_sent_again_after_a_lost_answer(self) -> None:
        """A cancel is safe to repeat: a 502 is tried again, so the deployment does not stay running."""
        self.railway.deployments["new9"] = deployment("new9", PROD_ENV, COMMIT, "BUILDING")
        answers = {"n": 0}

        def http(request):
            if b"deploymentCancel" in request.data and answers["n"] == 0:
                answers["n"] += 1
                return 502, b""
            return self.railway(request)

        api = rwy.RailwayApi(lambda: PROD_TOKEN, http=http, sleep=lambda _: None)
        api.cancel("new9")
        self.assertEqual(self.railway.deployments["new9"]["status"], "REMOVED")

    def test_reads_are_still_tried_again(self) -> None:
        """A 502 on a read is tried again (only mutations are sent once)."""
        answers = iter([(502, b""), (200, b'{"data": {"projectToken": {"projectId": "p", "environmentId": "e"}}}')])
        api = rwy.RailwayApi(lambda: PROD_TOKEN, http=lambda _: next(answers), sleep=lambda _: None)
        self.assertEqual(api.token_scope()["environmentId"], "e")

    def test_token_scope_guard(self) -> None:
        """A dev token in the prod slot is refused before any deploy."""
        wrong = rwy.RailwayApi(lambda: DEV_TOKEN, http=self.railway, sleep=lambda _: None)
        _, problems = rl.railway_deploy(API, wrong, "prod", COMMIT)
        self.assertIn("belongs to project", problems[0])
        self.assertEqual(self.railway.mutations(), [])

    def test_http_logs_page_forward(self) -> None:
        """HTTP logs page forward from the anchor and keep only [start, end)."""
        start = NOW - timedelta(minutes=30)
        rows = [
            {"requestId": f"r{i}", "timestamp": iso(start + timedelta(seconds=i)), "httpStatus": 500 if i < 3 else 200}
            for i in range(30)
        ]
        self.railway.http["d1"] = rows
        with mock.patch.object(rwy, "HTTP_PAGE", 7):
            got, complete = self.apis["prod"].http_logs("d1", start, start + timedelta(seconds=20))
            self.assertEqual((len(got), complete), (20, True))
            _, complete = self.apis["prod"].http_logs("d1", start, NOW, max_pages=2)
            self.assertFalse(complete)
        counts = rwy.request_counts(self.apis["prod"], "d1", start, NOW)
        self.assertEqual((counts.total, counts.errors), (30, 3))

    def test_logs_page_back(self) -> None:
        """Deployment logs page back from the end time until a page is short."""
        start = NOW - timedelta(minutes=30)
        self.railway.logs["d1"] = [
            {"timestamp": iso(start + timedelta(seconds=i)), "severity": "error", "message": f"e{i}"} for i in range(25)
        ]
        with mock.patch.object(rwy, "LOG_PAGE", 10):
            rows, complete = self.apis["prod"].logs("d1", start, NOW, filter_text="@level:error")
            self.assertEqual((len(rows), complete), (25, True))
            self.assertEqual(rows[0]["message"], "e0")
            _, complete = self.apis["prod"].logs("d1", start, NOW, max_pages=1)
            self.assertFalse(complete)

    def test_parse_time_and_baseline(self) -> None:
        """Nanosecond times parse; the baseline takes deployments that served in the span."""
        self.assertEqual(rwy.parse_time("2026-10-23T22:11:15.990676684Z").microsecond, 990676)
        start, end = NOW - timedelta(hours=24), NOW - timedelta(hours=1)
        removed_early = {**deployment("a", PROD_ENV, OLD_COMMIT, "REMOVED", NOW - timedelta(days=3))}
        removed_early["statusUpdatedAt"] = iso(NOW - timedelta(days=2))
        removed_late = {**deployment("b", PROD_ENV, OLD_COMMIT, "REMOVED", NOW - timedelta(days=2))}
        removed_late["statusUpdatedAt"] = iso(NOW - timedelta(hours=5))
        live = deployment("c", PROD_ENV, OLD_COMMIT, "SUCCESS", NOW - timedelta(hours=5))
        later = deployment("d", PROD_ENV, COMMIT, "SUCCESS", NOW - timedelta(minutes=10))
        self.assertEqual(rwy.baseline_deployments([removed_early, removed_late, live, later], start, end, ["c"]), ["b"])

    def test_migrations(self) -> None:
        """Added or changed .up.sql files under the prefix count; tests and removals do not; unknown counts."""
        files = [
            {"filename": "backend/migrations/0083_x.up.sql", "status": "added"},
            {"filename": "backend/migrations/0083_x.down.sql", "status": "added"},
            {"filename": "backend/migrations/registry_test.go", "status": "modified"},
            {"filename": "backend/migrations/0001_old.up.sql", "status": "removed"},
        ]
        self.assertEqual(
            rwy.migration_files(files, "backend/migrations/", ".up.sql"), ["backend/migrations/0083_x.up.sql"]
        )
        with mock.patch.object(rr, "github_json", return_value={"files": files}):
            state = rwy.migrations_between("o/r", OLD_COMMIT, COMMIT, "backend/migrations/", ".up.sql", "t")
        self.assertTrue(rwy.forward_only(state))
        with mock.patch.object(rr, "github_json", return_value={"files": files[2:3]}):
            self.assertFalse(
                rwy.forward_only(
                    rwy.migrations_between("o/r", OLD_COMMIT, COMMIT, "backend/migrations/", ".up.sql", "t")
                )
            )
        self.assertTrue(rwy.forward_only(rwy.migrations_between("o/r", OLD_COMMIT, COMMIT, "p/", "", None)))
        self.assertFalse(rwy.forward_only(rwy.migrations_between("o/r", COMMIT, COMMIT, "p/", "", None)))


# --------------------------------------------------------------------------- dev stage and prod deploy


class RailwayFlowTest(RailwayCase):
    def _dev_record(self) -> str:
        """railway-deploy to staging, then begin and finish the dev record; return its release id."""
        self.assertEqual(rl.cmd_railway_deploy(_args(env="dev", out_dir=str(self.root)), API, self.dev, None), 0)
        id_file = self.root / "release_id"
        images = str(self.root / "railway_deployment.json")
        self.assertEqual(
            rl.cmd_begin(_args(env="dev", images_file=images, release_id_file=str(id_file)), API, self.dev, None), 0
        )
        release_id = id_file.read_text().strip()
        self.assertEqual(rl.cmd_finish(_args(env="dev", release_id=release_id), API, self.dev, None), 0)
        return release_id

    def _verify(self, release_id: str, body: bytes = b'{"status": "ok", "version": "ccccccc"}') -> int:
        """Dev verify with a fake GET."""
        seen = []

        def get(request):
            """Record the URL; answer the health JSON."""
            seen.append(request.full_url)
            return 200, body

        self.seen = seen
        real = rl.railway_dev_checks
        with (
            mock.patch.object(rl, "railway_dev_checks", lambda s, r, a: real(s, r, a, get=get, sleep=lambda _: None)),
            mock.patch.object(rv, "newer_dev_build", return_value=None),
        ):
            args = _args(env="dev", release_id=release_id, failed_marker=str(self.root / "failed"))
            return rl.cmd_dev_verify(args, API, self.dev, None)

    def test_dev_stage_passes_and_indexes_the_commit(self) -> None:
        """The dev deploy goes to staging only; dev verify checks the staging URL; the artifact is the commit."""
        release_id = self._dev_record()
        self.assertEqual([v["e"] for name, v, _ in self.railway.calls if name == "deploy"], [DEV_ENV])
        record = rr.read_record(self.dev, release_id)
        self.assertEqual(record["artifacts"], {"demo-api": COMMIT})
        self.assertEqual(record["images"], {"demo-api": "new1"})
        self.assertEqual(self._verify(release_id), rv.EXIT_OK)
        self.assertTrue(all(url.startswith("https://demo-staging.up.railway.app/") for url in self.seen))
        index, _ = self.dev.read(rg.dev_commit_index(COMMIT))
        self.assertEqual(index["verdict"]["status"], "passed")
        self.assertNotIn(DEV_TOKEN, json.dumps(rr.read_record(self.dev, release_id)))

    def test_dev_verify_fails_on_auto_deploy_errors_or_wrong_version(self) -> None:
        """Railway auto deploy on or unreadable, an error log line, or a version that is not the commit fail dev verify."""
        release_id = self._dev_record()
        self.railway.auto[DEV_ENV] = True
        self.assertEqual(self._verify(release_id), rv.EXIT_FAILED)
        self.assertIn("auto deploy is ON", " ".join(rr.read_record(self.dev, release_id)["verdict"]["reasons"]))
        self.railway.auto[DEV_ENV] = False
        real_auto = rwy.RailwayApi.auto_deploy
        with mock.patch.object(rwy.RailwayApi, "auto_deploy", side_effect=rwy.RailwayError("Not Authorized")):
            self.assertEqual(self._verify(release_id), rv.EXIT_FAILED)
        self.assertIn("may be on", " ".join(rr.read_record(self.dev, release_id)["verdict"]["reasons"]))
        self.assertIs(rwy.RailwayApi.auto_deploy, real_auto)
        with mock.patch.object(rwy.RailwayApi, "auto_deploy", return_value={}):
            self.assertEqual(self._verify(release_id), rv.EXIT_FAILED)
        self.assertIn("it answered None", " ".join(rr.read_record(self.dev, release_id)["verdict"]["reasons"]))
        self.assertEqual(self._verify(release_id, b'{"version": "bbbbbbb"}'), rv.EXIT_FAILED)
        recent = datetime.now(rr.UTC) - timedelta(seconds=5)
        self.railway.logs["new1"] = [{"timestamp": iso(recent), "severity": "error", "message": "db.connect.failed"}]
        self.assertEqual(self._verify(release_id), rv.EXIT_FAILED)
        self.assertIn("db.connect.failed", " ".join(rr.read_record(self.dev, release_id)["verdict"]["reasons"]))

    def test_failed_dev_deploy(self) -> None:
        """A FAILED staging deployment fails railway-deploy; nothing reaches production."""
        self.railway.deploy_status = "FAILED"
        self.assertEqual(rl.cmd_railway_deploy(_args(env="dev", out_dir=str(self.root)), API, self.dev, None), 1)
        self.assertEqual(self.railway.active[PROD_ENV], "d_old")

    def _prod_release(self, service=API, migrations: list | None = None) -> str:
        """begin, apply and finish a prod release of COMMIT; return its release id."""
        artifacts = self.root / "artifacts.json"
        artifacts.write_text(json.dumps({"demo-api": COMMIT}))
        id_file = self.root / "prod_release_id"
        state = {"migrations": migrations or []}
        with mock.patch.object(rwy, "migrations_between", return_value=state):
            args = _args(artifacts_file=str(artifacts), release_id_file=str(id_file), previous_commit=OLD_COMMIT)
            self.assertEqual(rl.cmd_begin(args, service, self.prod, None), 0)
        release_id = id_file.read_text().strip()
        self.assertEqual(rl.cmd_apply(_args(artifacts_file=str(artifacts)), service, self.prod, None), 0)
        with mock.patch.object(rr, "release_lock"):
            self.assertEqual(rl.cmd_finish(_args(release_id=release_id), service, self.prod, None), 0)
        return release_id

    def test_prod_deploy_and_deploy_check(self) -> None:
        """The prod build deploys the commit to production; the deploy check reads the new deployment."""
        release_id = self._prod_release()
        record = rr.read_record(self.prod, release_id)
        self.assertEqual(record["previous"]["workloads"]["railway/demo-api"]["deployment"], "d_old")
        self.assertEqual(record["workloads"]["railway/demo-api"]["commit"], COMMIT)
        self.assertEqual(record["migrations"], {"migrations": []})
        get = mock.patch.object(rv, "check_http_public", return_value=rv.Check("health", "passed", ["ok"]))
        version = mock.patch.object(rl, "check_railway_version", return_value=None)
        with get, version:
            self.assertEqual(rl.cmd_check(_args(release_id=release_id), API, self.prod, None), rv.EXIT_OK)
            self.railway.active[PROD_ENV] = "d_old"
            with mock.patch.object(rr, "release_lock"):
                rl.cmd_finish(_args(release_id=release_id), API, self.prod, None)
            self.assertEqual(rl.cmd_check(_args(release_id=release_id), API, self.prod, None), rv.EXIT_FAILED)
        self.assertIn("not cccccccc", " ".join(rr.read_record(self.prod, release_id)["verdict"]["reasons"]))

    def test_check_rollback_tier_deploys_without_an_artifacts_file(self) -> None:
        """Tier check_rollback: begin takes the commit as the artifact, apply deploys it to production."""
        id_file = self.root / "release_id"
        with mock.patch.object(rwy, "migrations_between", return_value={"migrations": []}):
            self.assertEqual(rl.cmd_begin(_args(release_id_file=str(id_file)), DIRECT, self.prod, None), 0)
        record = rr.read_record(self.prod, id_file.read_text().strip())
        self.assertEqual(record["artifacts"], {"demo-api": COMMIT})
        self.assertEqual(rl.promote_artifacts(DIRECT, record["artifacts"], None, self.prod), [])
        self.assertEqual([v["e"] for name, v, _ in self.railway.calls if name == "deploy"], [PROD_ENV])

    def test_failed_prod_deploy(self) -> None:
        """A CRASHED production deployment is a failed apply; Railway keeps the old deployment live."""
        self.railway.deploy_status = "CRASHED"
        failures = rl.promote_artifacts(API, {"demo-api": COMMIT}, None, self.prod)
        self.assertIn("CRASHED", failures[0])
        self.assertEqual(self.railway.active[PROD_ENV], "d_old")


# --------------------------------------------------------------------------- window, forward-only, rollback


class RailwayWindowTest(RailwayCase):
    def setUp(self) -> None:
        """A deployed release (d_new of COMMIT) that replaced d_old at NOW - 20 min."""
        super().setUp()
        self.railway.deployments["d_new"] = deployment("d_new", PROD_ENV, COMMIT, created=NOW - timedelta(minutes=21))
        self.railway.active[PROD_ENV] = "d_new"
        self.record = {
            "release_id": "20261024T114000Z-ccccccccc",
            "kind": "promote",
            "commit": COMMIT,
            "status": "deployed",
            "artifacts": {"demo-api": COMMIT},
            "commits": [{"sha": COMMIT, "title": "Change the API (#7)", "pr": 7, "author": "dev"}],
            "previous": {
                "release_id": None,
                "commit": OLD_COMMIT,
                "workloads": {"railway/demo-api": {"deployment": "d_old"}},
            },
            "workloads": {"railway/demo-api": {"deployment": "d_new", "commit": COMMIT, "status": "SUCCESS"}},
            "times": {"deploy_started": "2026-10-24T11:40:00Z", "deploy_finished": "2026-10-24T11:40:00Z"},
            "verdict": {"status": "pending"},
            "migrations": {"migrations": []},
        }
        self.ok = mock.patch.object(rv, "check_http_public", return_value=rv.Check("health", "passed", ["ok"]))
        self.ok.start()
        self.version = mock.patch.object(rl, "check_railway_version", return_value=None)
        self.version.start()

    def tearDown(self) -> None:
        """Stop the window patches."""
        self.version.stop()
        self.ok.stop()
        super().tearDown()

    def _traffic(self, dpl: str, start: datetime, count: int, errors: int) -> None:
        """count requests of a deployment from start on, errors of them 5xx."""
        rows = self.railway.http.setdefault(dpl, [])
        for i in range(count):
            when = start + timedelta(seconds=i)
            rows.append(
                {
                    "requestId": f"{dpl}{when.timestamp()}",
                    "timestamp": iso(when),
                    "httpStatus": 500 if i < errors else 200,
                    "path": "/v1/x",
                }
            )

    def test_quiet_release_passes(self) -> None:
        """No new error line, a 5xx share like before, the smoke checks pass."""
        self._traffic("d_old", NOW - timedelta(minutes=40), 100, 1)
        self._traffic("d_new", NOW - timedelta(minutes=15), 100, 1)
        result = rl.evaluate_railway_window(API, self.prod, self.record, NOW)
        self.assertEqual(result["regressions"], [])
        self.assertTrue(result["judge"])
        self.assertEqual(result["parts"]["deployment"]["status"], "checked")
        self.assertEqual(result["parts"]["logs"]["status"], "checked")

    def test_5xx_regression_and_new_error_line(self) -> None:
        """A 5xx share far above the previous deployment's fails a runtime release; a new error line is a problem."""
        self._traffic("d_old", NOW - timedelta(minutes=40), 100, 1)
        self._traffic("d_new", NOW - timedelta(minutes=15), 100, 40)
        self.railway.logs["d_old"] = [
            {
                "timestamp": iso(NOW - timedelta(hours=3)),
                "severity": "error",
                "message": "push.dispatch.failed for token",
            }
        ]
        self.railway.logs["d_new"] = [
            {
                "timestamp": iso(NOW - timedelta(minutes=m)),
                "severity": "error",
                "message": "search_workflow.load.failed: relation missing",
            }
            for m in range(1, 6)
        ]
        with mock.patch.object(rv, "runtime_change", return_value=(True, "it changes backend/x.go")):
            result = rl.evaluate_railway_window(API, self.prod, self.record, NOW)
        self.assertEqual(result["parts"]["5xx"]["status"], "exceeded")
        self.assertEqual(result["regressions"][0]["name"], "railway_5xx_share")
        self.assertIn("search_workflow.load.failed", result["problems"][0]["text"])

    def test_crash_fails_and_a_moved_deployment_judges_nothing(self) -> None:
        """A crashed release deployment is a regression; production on another deployment is inconclusive."""
        self.railway.deployments["d_new"]["status"] = "CRASHED"
        self.railway.active[PROD_ENV] = None
        result = rl.evaluate_railway_window(API, self.prod, self.record, NOW)
        self.assertEqual(result["regressions"][0]["name"], "deployment")
        self.assertTrue(result["judge"])
        self.railway.deployments["d_new"]["status"] = "REMOVED"
        self.railway.deployments["d_manual"] = deployment("d_manual", PROD_ENV, OLD_COMMIT)
        self.railway.active[PROD_ENV] = "d_manual"
        result = rl.evaluate_railway_window(API, self.prod, self.record, NOW)
        self.assertFalse(result["judge"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["parts"]["smoke"]["status"], "inconclusive")

    def _failed(self, migrations: list) -> dict:
        """The record as a failed release, saved in the prod store and named by latest.json."""
        record = {**self.record, "verdict": {"status": "failed", "reasons": ["5xx share above the limit"]}}
        record["migrations"] = {"migrations": migrations}
        rr.create_record(self.prod, record, "deploy_started")
        rr.point_latest(self.prod, record)
        return record

    def test_forward_only_release_holds_and_pings(self) -> None:
        """A failed release with a migration does not roll back: it holds on its own record and pings once."""
        record = self._failed(["backend/migrations/0083_x.up.sql"])
        with (
            mock.patch.dict(os.environ, {"RELEASE_AUTO_ROLLBACK": "1"}),
            mock.patch.object(rv, "start_rollback") as start,
            contextlib.redirect_stdout(io.StringIO()) as log,
        ):
            rl.on_failed(API, self.prod, record)
            rl.on_failed(API, self.prod, rr.read_record(self.prod, record["release_id"]))
        start.assert_not_called()
        saved = rr.read_record(self.prod, record["release_id"])
        self.assertTrue(saved["promote_hold"]["forward_only"])
        self.assertEqual(saved["auto_rollback"]["status"], "skipped")
        self.assertEqual(len(self.slack), 1)
        self.assertTrue(
            self.slack[0].startswith(rr.status_line("on hold", API.name, release=rr.release_label(record))),
            self.slack[0],
        )
        self.assertIn("Fixes-release: " + record["release_id"], log.getvalue())
        self.assertEqual(self.railway.mutations(), [])

    def test_release_without_migration_rolls_back(self) -> None:
        """Without a migration the automatic rollback starts as for every kind."""
        record = self._failed([])
        with mock.patch.object(rv, "start_rollback") as start:
            rl.on_failed(API, self.prod, record)
        start.assert_called_once()

    def test_gate_holds_until_fixes_release(self) -> None:
        """The forward hold holds later commits; a revert does not end it; Fixes-release and resume do."""
        record = self._failed(["backend/migrations/0083_x.up.sql"])
        with mock.patch.dict(os.environ, {"RELEASE_AUTO_ROLLBACK": "1"}):
            rl.on_failed(API, self.prod, record)
        stores = rg.Stores(service=API, prod=self.prod, dev=self.dev)
        pointer, _ = self.prod.read("latest.json")
        revert = [(FIX_COMMIT, f'Revert "Change the API (#7)"\n\nThis reverts commit {COMMIT}.')]
        fix = [(FIX_COMMIT, f"Fix the query\n\nFixes-release: {record['release_id']}")]
        with mock.patch.object(rg, "contains", return_value=True):
            with mock.patch.object(rg, "commit_messages", return_value=revert):
                held = rg._hold(stores, FIX_COMMIT, "t", pointer)
            self.assertEqual(held.status, "held")
            self.assertIn("forward-only", held.reason)
            self.assertIn("Fixes-release", held.resume_hint)
            with mock.patch.object(rg, "commit_messages", return_value=fix):
                self.assertTrue(rg._hold(stores, FIX_COMMIT, "t", pointer).go)
            args = argparse.Namespace(reason="schema fixed by hand", by="nick")
            self.assertEqual(rg.cmd_resume(args, stores), rg.EXIT_GO)
            with mock.patch.object(rg, "commit_messages", return_value=revert):
                self.assertTrue(rg._hold(stores, FIX_COMMIT, "t", pointer).go)
        self.assertEqual(rr.read_record(self.prod, record["release_id"])["promote_hold"]["status"], "released")

    def test_a_promote_without_hold_is_no_hold(self) -> None:
        """A deployed release without a forward hold holds nothing."""
        record = self._failed([])
        pointer, _ = self.prod.read("latest.json")
        stores = rg.Stores(service=API, prod=self.prod, dev=self.dev)
        self.assertIsNone(rg.holding_record(stores, pointer))
        self.assertTrue(rg._hold(stores, FIX_COMMIT, "t", pointer).go)
        self.assertEqual(record["kind"], "promote")


class RailwayRollbackTest(RailwayCase):
    def setUp(self) -> None:
        """Production serves d_new (COMMIT) after a release that replaced d_old (OLD_COMMIT)."""
        super().setUp()
        self.railway.deployments["d_new"] = deployment("d_new", PROD_ENV, COMMIT)
        self.railway.active[PROD_ENV] = "d_new"
        self.latest = {
            "release_id": "r1",
            "kind": "promote",
            "commit": COMMIT,
            "status": "deployed",
            "workloads": {"railway/demo-api": {"deployment": "d_new"}},
        }
        self.target = {
            "release_id": None,
            "commit": OLD_COMMIT,
            "workloads": {"railway/demo-api": {"deployment": "d_old"}},
        }

    def _plan(self, migrations: dict | None = None) -> rb.Plan:
        """The rollback plan with a mocked migration read."""
        with mock.patch.object(rwy, "migrations_between", return_value=migrations or {"migrations": []}):
            return rl.rollback_plan(API, self.latest, self.target, None)

    def test_plan_apply_and_check(self) -> None:
        """The previous deployment is redeployed with its image; the check reads the target commit."""
        plan = self._plan()
        self.assertEqual(plan.problems, [])
        self.assertEqual((plan.steps[0].current, plan.steps[0].target, plan.steps[0].action), ("d_new", "d_old", "set"))
        self.assertEqual(rl.apply_rollback(API, plan, None), [])
        self.assertEqual(self.railway.mutations(), ["redeploy"])
        after = rl.snapshot_railway(API, self.apis["prod"])
        self.assertEqual(after["railway/demo-api"]["image_digest"], "sha256:d_old")
        self.assertEqual(rl.check_rollback(API, plan, after).status, "passed")
        self.assertEqual(self._plan().steps[0].action, "none")

    def test_image_gone_rebuilds_the_commit(self) -> None:
        """A target that cannot be redeployed is rebuilt from its commit."""
        self.railway.deployments["d_old"]["canRedeploy"] = False
        self.assertEqual(rl.apply_rollback(API, self._plan(), None), [])
        self.assertEqual(self.railway.mutations(), ["deploy"])

    def test_blocks(self) -> None:
        """A migration in the rolled-back range or a deployment outside the kit blocks the rollback."""
        plan = self._plan({"migrations": ["backend/migrations/0083_x.up.sql"]})
        self.assertIn("Forward-only", plan.problems[0])
        self.railway.deployments["d_manual"] = deployment("d_manual", PROD_ENV, COMMIT)
        self.railway.active[PROD_ENV] = "d_manual"
        self.assertIn("a human decides", " ".join(self._plan().problems))
        self.target = {"workloads": {}}
        self.assertIn("recorded no deployment", " ".join(self._plan().problems))


# --------------------------------------------------------------------------- kind static_site


def static_data(**overrides) -> dict:
    """A kind static_site config of the docs site."""
    data = {
        "service": "docs",
        "title": "Docs",
        "kind": "static_site",
        "tier": "check_only",
        "github_repo": "orbit-search/docs",
        "connected_repository": "orbit-search-docs",
        "deploy_triggers": ["docs-release"],
        "static_site": {
            "url": "https://docs.example.com",
            "publisher_check": "Mintlify Deployment",
            "required_paths": ["/llms.txt"],
        },
        "smoke": [{"type": "http_public", "name": "home", "url": "https://docs.example.com/"}],
        "verify": {"checkpoints": [15, 60]},
        "code": {"source_prefixes": ["api/", "guides/"], "extensions": [".mdx"]},
    }
    data.update(overrides)
    return data


DOCS = rl.parse_light(static_data())
SITEMAP = (
    b'<?xml version="1.0"?><urlset><url><loc>https://docs.example.com/</loc></url>'
    b"<url><loc>https://docs.example.com/api/search/search</loc></url>"
    b"<url><loc>https://docs.example.com/guides/setup</loc></url></urlset>"
)


class FakeSite:
    """The live site: path -> (status, body); records every URL."""

    def __init__(self) -> None:
        """A healthy site of three pages."""
        self.seen: list[str] = []
        self.pages = {
            "/sitemap.xml": (200, SITEMAP),
            "/llms.txt": (200, b"# docs"),
            "/": (200, b"<title>Home - Docs</title><a href='/guides/setup'>x</a>"),
            "/api/search/search": (
                200,
                b'<title>Start a People Search - Docs</title><a href="/guides/setup#x">a</a>'
                b"<a href='/api/missing'>b</a><a href='https://other.example.com/x'>c</a><a href='/logo.png'>d</a>",
            ),
            "/guides/setup": (200, b"<title>Setup - Docs</title>"),
        }

    def __call__(self, request: urllib.request.Request) -> tuple[int, bytes]:
        """Answer one GET."""
        self.seen.append(request.full_url)
        path = urllib.parse.urlsplit(request.full_url).path
        return self.pages.get(path, (404, b"<title>Page not found</title>"))


import urllib.parse  # noqa: E402


class StaticSiteTest(unittest.TestCase):
    def setUp(self) -> None:
        """Block the network. Render as a private repository (a vendored people map), so the expected secret
        lists hold in a public repository's copy too (sync deletes the map there)."""
        self.patches = [
            mock.patch.object(urllib.request, "urlopen", side_effect=_no_network),
            mock.patch.object(subprocess, "run", side_effect=_no_network),
            mock.patch.object(kit, "people_vendored", return_value=True),
        ]
        for patch in self.patches:
            patch.start()
        self.site = FakeSite()

    def tearDown(self) -> None:
        """Stop the patches."""
        for patch in reversed(self.patches):
            patch.stop()

    def test_parse_and_render(self) -> None:
        """Tier check_only only; by default the builds read GitHub with the connection link's read token."""
        self.assertFalse(DOCS.has_gate or DOCS.auto_rollback)
        self.assertEqual(DOCS.site.publisher_check, "Mintlify Deployment")
        for bad in (
            static_data(tier="full"),
            static_data(static_site={"url": "https://docs.example.com/x", "publisher_check": "M"}),
            static_data(smoke=[{"type": "http_public", "url": "https://other.example.com/"}]),
        ):
            with self.assertRaises(rc.ConfigError):
                rl.parse_light(bad)
        files = kit.rendered(DOCS)
        self.assertEqual(
            sorted(files), ["release/services/docs.release-verify.yaml", "release/services/docs.release.yaml"]
        )
        for text in files.values():
            self.assertNotIn("GITHUB_TOKEN", text)
            self.assertIn("secrets/slack-webhook-workspace-orbit/versions/latest", text)
            self.assertIn('secretEnv: ["WEBHOOK_ORBIT"]', text)
            self.assertNotIn("{", text.split("steps:")[0])  # every template field was filled
        self.assertIn("static-wait --commit", files["release/services/docs.release.yaml"])
        self.assertEqual(DOCS.connected_repository, "orbit-search-docs")
        explicit = static_data()
        explicit["static_site"] = {**explicit["static_site"], "github_token_secret": "connection"}
        self.assertEqual(kit.rendered(rl.parse_light(explicit)), files)

    def test_named_github_secret_renders_into_every_step(self) -> None:
        """A named github_token_secret goes into GITHUB_TOKEN of every step that reads GitHub."""
        data = static_data()
        data["static_site"] = {**data["static_site"], "github_token_secret": "docs-github-token"}
        files = kit.rendered(rl.parse_light(data))
        release = files["release/services/docs.release.yaml"]
        self.assertEqual(release.count('secretEnv: ["GITHUB_TOKEN"]'), 3)
        self.assertEqual(release.count('secretEnv: ["WEBHOOK_ORBIT", "GITHUB_TOKEN"]'), 1)
        for text in files.values():
            self.assertIn("secrets/docs-github-token/versions/latest", text)

    def test_routes_and_titles(self) -> None:
        """Repository files map to routes; snippets and scripts are not pages; frontmatter titles parse."""
        site = DOCS.site
        self.assertEqual(rst.route_of("api/search/search.mdx", site), "/api/search/search")
        self.assertEqual(rst.route_of("index.mdx", site), "/")
        self.assertEqual(rst.route_of("guides/index.mdx", site), "/guides")
        self.assertIsNone(rst.route_of("snippets/x.mdx", site))
        self.assertIsNone(rst.route_of("README.md", site))
        self.assertEqual(
            rst.front_title('---\ntitle: "Start a People Search"\nx: y\n---\nbody'), "Start a People Search"
        )
        files = [
            {"filename": "api/search/search.mdx", "status": "modified"},
            {"filename": "guides/new.mdx", "status": "renamed", "previous_filename": "guides/old.mdx"},
            {"filename": "guides/gone.mdx", "status": "removed"},
            {"filename": "scripts/x.mjs", "status": "modified"},
        ]
        trees = {
            OLD_COMMIT: ["api/search/search.mdx", "guides/old.mdx", "guides/gone.mdx", "scripts/x.mjs"],
            COMMIT: ["api/search/search.mdx", "guides/new.mdx", "scripts/x.mjs"],
        }

        def github(path, token):
            """The compare, the trees, and no ignore file."""
            if "/compare/" in path:
                return {"files": files}
            if "/git/trees/" in path:
                commit = path.split("/git/trees/")[1].split("?")[0]
                return {"tree": [{"path": item, "type": "blob"} for item in trees[commit]]}
            raise guard.GuardError("HTTP 404")

        with mock.patch.object(rr, "github_json", side_effect=github):
            changes = rst.changed_pages("o/r", OLD_COMMIT, COMMIT, site, "t", lambda path: "---\ntitle: T\n---")
        self.assertEqual(sorted(changes.pages), ["/api/search/search", "/guides/new"])
        self.assertEqual(sorted(changes.removed), ["/guides/gone", "/guides/old"])
        self.assertFalse(rst.changed_pages("o/r", None, COMMIT, site, "t", lambda _: None).known)

    def test_page_checks(self) -> None:
        """Every sitemap page, the required paths and the changed pages' internal links are read, cache-busted."""
        changes = rst.Changes(pages={"/api/search/search": "Start a People Search"})
        result = rst.check_pages(DOCS.site, COMMIT, changes, 3, strict_content=True, get=self.site)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("/api/missing: HTTP 404", result.failures[0])
        self.assertTrue(all("release-check=cccccccccccc" in url for url in self.site.seen))
        self.assertFalse(any("other.example.com" in url or "logo.png" in url for url in self.site.seen))
        self.site.pages["/api/search/search"] = (200, b"<title>Old Title - Docs</title>")
        soft = rst.check_pages(DOCS.site, COMMIT, changes, 3, strict_content=False, get=self.site)
        self.assertEqual(soft.failures, [])
        self.assertIn("Old Title", soft.warnings[0])
        hard = rst.check_pages(DOCS.site, COMMIT, changes, 3, strict_content=True, get=self.site)
        self.assertIn("Old Title", hard.failures[0])

    def test_page_count_and_broken_pages(self) -> None:
        """A sitemap that lost pages this release did not remove fails; a 500 page fails; llms.txt must answer."""
        result = rst.check_pages(DOCS.site, COMMIT, rst.Changes(), 5, strict_content=True, get=self.site)
        self.assertIn("previous release listed 5", result.failures[0])
        self.assertEqual(
            rst.check_pages(
                DOCS.site, COMMIT, rst.Changes(removed=["/a", "/b"]), 5, strict_content=True, get=self.site
            ).failures,
            [],
        )
        self.site.pages["/guides/setup"] = (500, b"")
        self.site.pages["/llms.txt"] = (404, b"")
        failures = rst.check_pages(DOCS.site, COMMIT, rst.Changes(), None, strict_content=True, get=self.site).failures
        self.assertTrue(any("/llms.txt: HTTP 404" in item for item in failures))
        self.assertTrue(any("/guides/setup: HTTP 500" in item for item in failures))

    def test_wait_for_publish(self) -> None:
        """success is published, skipped is nothing, failure fails, no run in time is missing."""
        runs = {"conclusion": "success", "status": "completed", "name": "Mintlify Deployment", "started_at": "1"}
        clock = iter(range(0, 10000, 100))
        cases = [
            ({**runs}, "published"),
            ({**runs, "conclusion": "skipped"}, "nothing"),
            ({**runs, "conclusion": "failure"}, "failed"),
        ]
        for run, outcome in cases:
            with mock.patch.object(rr, "github_json", return_value={"check_runs": [run]}):
                self.assertEqual(rst.wait_for_publish("o/r", COMMIT, DOCS.site, "t", sleep=lambda _: None)[0], outcome)
        with mock.patch.object(rr, "github_json", return_value={"check_runs": []}):
            got = rst.wait_for_publish("o/r", COMMIT, DOCS.site, "t", sleep=lambda _: None, clock=lambda: next(clock))
        self.assertEqual(got[0], "missing")

    def test_revert_command_and_alert(self) -> None:
        """The failed-release ping carries the exact revert command of the release's commits."""
        command = rl.rollback_command(DOCS, {"commit": COMMIT, "previous": {"commit": OLD_COMMIT}})
        self.assertIn(f"git revert --no-edit {OLD_COMMIT}..{COMMIT}", command)
        self.assertIn("gh pr create --repo orbit-search/docs --base main", command)
        self.assertIn(f"git revert --no-edit {COMMIT} ", rl.rollback_command(DOCS, {"commit": COMMIT, "previous": {}}))
        record = {"release_id": "r1", "commit": COMMIT, "status": "deployed", "verdict": {"reasons": ["/x: HTTP 404"]}}
        state, _, detail = rl.static_alert(DOCS, record, "gs://x/r1.json", command)
        self.assertEqual(state, "down")
        self.assertIn(OLD_COMMIT, detail)
        self.assertIn("nothing reverts by itself", detail)
        # Nothing went live: the old site still serves, so the line is a warning.
        self.assertEqual(rl.static_alert(DOCS, {**record, "status": "deploy_failed"}, "gs://x", command)[0], "warning")


class OwnerMentionTest(unittest.TestCase):
    def test_static_alert_mentions_the_owner_of_an_unclaimed_release(self) -> None:
        """Docs failures use static_alert: it mentions the PR author of a release no agent claimed, even when
        an earlier PR ranks first, and the fallback people only when nobody is in the map."""
        commits = [
            {"sha": OLD_COMMIT, "title": "Old page (#11)", "pr": 11, "author": "mapped-other"},
            {"sha": COMMIT, "title": "New page (#12)", "pr": 12, "author": "jonnybcw"},
        ]
        record = {
            "release_id": "r1",
            "commit": COMMIT,
            "status": "deployed",
            "commits": commits,
            "followup": {},
            "verdict": {"reasons": ["/x: HTTP 404"]},
        }
        people = {"slack_ids": {"jonnybcw": "UJONNY", "mapped-other": "UOTHER"}, "fallback": ["UFALLBACK"]}
        with (
            mock.patch.object(rl.rv, "load_people", return_value=people),
            mock.patch.object(rl.rv, "likely_prs", return_value=commits[:1]),
        ):
            _, mentions, _ = rl.static_alert(DOCS, record, "gs://x/r1.json", "git revert")
            self.assertEqual(mentions, ["<@UOTHER>", "<@UJONNY>"])
            claimed = {**record, "followup": {"owner": {"thread_url": "codex://t", "pr_url": "https://x/pull/12"}}}
            self.assertNotIn("<@UJONNY>", rl.static_alert(DOCS, claimed, "gs://x", "git revert")[1])
        with (
            mock.patch.object(rl.rv, "load_people", return_value={"slack_ids": {}, "fallback": ["UFALLBACK"]}),
            mock.patch.object(rl.rv, "likely_prs", return_value=commits[:1]),
        ):
            self.assertIn("<@UFALLBACK>", rl.static_alert(DOCS, record, "gs://x", "git revert")[1])


class ReviewFixesTest(RailwayCase):
    def test_timeout_cancels_the_unfinished_deployment(self) -> None:
        """A deployment still building when the wait runs out is cancelled, so it never goes live unchecked."""
        self.railway.deploy_status = "BUILDING"
        ticks = iter(range(0, 100000, 600))
        _, problems = rl.railway_deploy(
            API, self.apis["prod"], "prod", COMMIT, sleep=lambda _: None, clock=lambda: next(ticks)
        )
        self.assertIn("cancelled", problems[0])
        self.assertEqual(self.railway.mutations(), ["deploy", "cancel"])

    def test_unreadable_status_cancels_and_keeps_the_deployment_id(self) -> None:
        """A status read that fails after the deploy started cancels the kit's deployment and keeps its id."""
        with mock.patch.object(rwy, "wait_for_deployment", side_effect=rr.RecordError("Railway HTTP 502")):
            started, problems = rl.railway_deploy(API, self.apis["prod"], "prod", COMMIT)
        self.assertEqual(started["id"], "new1")
        self.assertIn("status unreadable", problems[0])
        self.assertIn("cancelled", problems[0])
        self.assertEqual(self.railway.mutations(), ["deploy", "cancel"])

    def test_rollback_with_unreadable_status_cancels_and_fails(self) -> None:
        """The rollback's redeploy is cancelled too when its status cannot be read; the rollback fails."""
        self.railway.deployments["d_new"] = deployment("d_new", PROD_ENV, COMMIT)
        self.railway.active[PROD_ENV] = "d_new"
        with mock.patch.object(rwy, "wait_for_deployment", side_effect=rr.RecordError("Railway HTTP 502")):
            failures = rl.railway_rollback(API, self.apis["prod"], "d_old", OLD_COMMIT)
        self.assertIn("status unreadable", failures[0])
        self.assertEqual(self.railway.mutations()[-1], "cancel")

    def test_unknown_changed_pages_leave_the_checkpoint_incomplete(self) -> None:
        """Unknown changed pages: their titles and links went unchecked, so the checkpoint is not complete."""
        unknown = rst.Changes(known=False, count_known=True, note="300 or more changed files")
        result = rst.check_pages(DOCS.site, COMMIT, unknown, None, strict_content=True, get=FakeSite())
        self.assertFalse(result.complete)
        self.assertTrue(any("changed pages are unknown" in item for item in result.warnings))

    def test_rollback_waits_for_a_deployment_in_flight(self) -> None:
        """A production deployment that still builds blocks the rollback plan."""
        self.railway.deployments["d_new"] = deployment("d_new", PROD_ENV, COMMIT)
        self.railway.deployments["d_fly"] = deployment("d_fly", PROD_ENV, FIX_COMMIT, "BUILDING")
        self.railway.active[PROD_ENV] = "d_new"
        latest = {"release_id": "r1", "commit": COMMIT, "workloads": {"railway/demo-api": {"deployment": "d_new"}}}
        target = {"commit": OLD_COMMIT, "workloads": {"railway/demo-api": {"deployment": "d_old"}}}
        with mock.patch.object(rwy, "migrations_between", return_value={"migrations": []}):
            plan = rl.rollback_plan(API, latest, target, None)
        self.assertIn("still BUILDING", " ".join(plan.problems))

    def test_log_pages_keep_rows_at_the_boundary(self) -> None:
        """Rows that share the oldest timestamp of a page are read once, none lost to rounding."""
        start = NOW - timedelta(minutes=10)
        rows = []
        for i in range(12):
            when = start + timedelta(seconds=i // 2, microseconds=123800)
            rows.append({"timestamp": iso(when).replace("123Z", "Z"), "severity": "error", "message": f"e{i}"})
        self.railway.logs["d1"] = rows

        def inclusive(name, v, env, real=self.railway.answer):
            """deploymentLogs with an inclusive end at full precision."""
            if name != "logs":
                return real(name, v, env)
            end = rwy.parse_time(v["e"])
            hits = [r for r in self.railway.logs[v["d"]] if rwy.parse_time(r["timestamp"]) <= end]
            return {"deploymentLogs": hits[-v["n"] :]}

        with mock.patch.object(rwy, "LOG_PAGE", 5), mock.patch.object(self.railway, "answer", side_effect=inclusive):
            got, complete = self.apis["prod"].logs("d1", start, NOW)
        self.assertEqual(sorted(row["message"] for row in got), sorted(f"e{i}" for i in range(12)))
        self.assertTrue(complete)

    def test_failed_migration_release_holds_before_the_hold_is_written(self) -> None:
        """The gate holds a failed release whose range changes the schema even before its promote_hold exists."""
        record = {
            "release_id": "r9",
            "kind": "promote",
            "commit": COMMIT,
            "status": "deployed",
            "verdict": {"status": "failed"},
            "migrations": {"migrations": ["backend/migrations/0083_x.up.sql"]},
            "times": {"deploy_started": "2026-10-24T11:40:00Z"},
        }
        rr.create_record(self.prod, record, "deploy_started")
        rr.point_latest(self.prod, record)
        stores = rg.Stores(service=API, prod=self.prod, dev=self.dev)
        pointer, _ = self.prod.read("latest.json")
        with (
            mock.patch.object(rg, "contains", return_value=True),
            mock.patch.object(rg, "commit_messages", return_value=[]),
        ):
            self.assertEqual(rg._hold(stores, FIX_COMMIT, "t", pointer).status, "held")
            rg.cmd_resume(argparse.Namespace(reason="checked by hand", by="nick"), stores)
            self.assertTrue(rg._hold(stores, FIX_COMMIT, "t", pointer).go)

    def test_grouped_failure_reads_the_whole_rollback_range(self) -> None:
        """A rollback past an older release reads migrations over the whole range, not the newest release's own."""
        record = {
            "release_id": "r2",
            "commit": COMMIT,
            "previous": {"commit": FIX_COMMIT},
            "migrations": {"migrations": []},
            "rollback_suggestion": {"target": {"commit": OLD_COMMIT}},
        }
        with mock.patch.object(
            rwy, "migrations_between", return_value={"migrations": ["backend/migrations/1.up.sql"]}
        ) as read:
            self.assertIsNotNone(rl.forward_state(API, record))
        self.assertEqual(read.call_args.args[1:3], (OLD_COMMIT, COMMIT))
        plain = {**record, "rollback_suggestion": {"target": {"commit": FIX_COMMIT}}}
        self.assertIsNone(rl.forward_state(API, plain))

    def test_relative_links_resolve_against_the_page(self) -> None:
        """A relative link resolves against the page's own URL, as a browser does."""
        body = "<a href='setup'>a</a><a href='../api/x'>b</a><a href='/c'>c</a>"
        links = rst.internal_links(body, "https://docs.example.com/guides/start")
        self.assertEqual(
            links,
            ["https://docs.example.com/api/x", "https://docs.example.com/c", "https://docs.example.com/guides/setup"],
        )

    def test_previous_commit_is_what_serves(self) -> None:
        """After a failed deploy the next release starts its range at the commit that serves, not the failed one."""
        failed = {
            "release_id": "rA",
            "kind": "promote",
            "commit": FIX_COMMIT,
            "status": "deploy_failed",
            "times": {"deploy_started": "2026-10-24T11:00:00Z"},
        }
        rr.create_record(self.prod, failed, "deploy_started")
        rr.point_latest(self.prod, failed)
        id_file = self.root / "release_id"
        with mock.patch.object(rwy, "migrations_between", return_value={"migrations": []}) as read:
            rl.cmd_begin(_args(release_id_file=str(id_file)), DIRECT, self.prod, None)
        record = rr.read_record(self.prod, id_file.read_text().strip())
        self.assertEqual(record["previous"]["commit"], OLD_COMMIT)
        self.assertEqual(record["previous"]["recorded_commit"], FIX_COMMIT)
        self.assertEqual(read.call_args.args[1], OLD_COMMIT)

    def test_first_release_compares_with_its_parent(self) -> None:
        """No earlier release: the changed pages are the release commit's own (its parent is the base), so the
        check is complete; a later window that judges it compares from the oldest commit's parent to the newest."""
        record = {"release_id": "r1", "commit": COMMIT, "status": "deployed", "previous": {}}
        seen = []

        def github(path, token):
            """The parent, the compare and the trees."""
            seen.append(path)
            if path.endswith(f"/commits/{COMMIT}"):
                return {"parents": [{"sha": OLD_COMMIT}]}
            if "/compare/" in path:
                return {"files": [{"filename": "guides/setup.mdx", "status": "modified"}]}
            return {"tree": [{"path": "guides/setup.mdx", "type": "blob"}]}

        with (
            mock.patch.object(rr, "github_token", return_value="t"),
            mock.patch.object(rr, "github_json", side_effect=github),
        ):
            result = rl.static_pages(DOCS, self.prod, record, strict_content=True, get=FakeSite())
        self.assertIn(f"repos/orbit-search/docs/compare/{OLD_COMMIT}...{COMMIT}?per_page=300", seen)
        self.assertTrue(result.complete)
        self.assertFalse([w for w in result.warnings if "changed pages are unknown" in w])
        # A later window that judges the first release: the parent of the oldest commit, up to the newest.
        newer = {"release_id": "r2", "commit": FIX_COMMIT, "status": "deployed", "previous": {"release_id": "r1"}}
        seen.clear()
        with (
            mock.patch.object(rr, "github_token", return_value="t"),
            mock.patch.object(rr, "github_json", side_effect=github),
        ):
            later = rl.static_pages(DOCS, self.prod, newer, strict_content=True, get=FakeSite(), base=record)
        self.assertIn(f"repos/orbit-search/docs/commits/{COMMIT}", seen)
        self.assertIn(f"repos/orbit-search/docs/compare/{OLD_COMMIT}...{FIX_COMMIT}?per_page=300", seen)
        self.assertTrue(later.complete)
        with mock.patch.object(rr, "github_json", side_effect=guard.GuardError("HTTP 500")):
            self.assertIsNone(rst.parent_commit("orbit-search/docs", COMMIT, "t"))

    def test_static_base_skips_failed_publishes(self) -> None:
        """A failed publish is never the comparison base of the next release; the merge build has single flight."""
        good = {"release_id": "r1", "commit": OLD_COMMIT, "status": "deployed", "previous": {}}
        bad = {"release_id": "r2", "commit": FIX_COMMIT, "status": "deploy_failed", "previous": {"release_id": "r1"}}
        for item in (good, bad):
            rr.create_record(
                self.prod, {**item, "kind": "promote", "times": {"deploy_started": "2026-10-24T11:00:00Z"}}, "x"
            )
        self.assertEqual(rl.last_published(self.prod, rr.read_record(self.prod, "r2"))["release_id"], "r1")
        self.assertIn("static-turn", kit.rendered(DOCS)["release/services/docs.release.yaml"])

    def test_resume_or_fix_before_the_hold_is_saved_sticks(self) -> None:
        """resume and a seen fix save the inferred hold; forward_hold never overwrites them later."""
        record = {
            "release_id": "r7",
            "kind": "promote",
            "commit": COMMIT,
            "status": "deployed",
            "verdict": {"status": "failed", "reasons": ["x"]},
            "migrations": {"migrations": ["backend/migrations/1.up.sql"]},
            "times": {"deploy_started": "2026-10-24T11:40:00Z"},
        }
        rr.create_record(self.prod, record, "deploy_started")
        rr.point_latest(self.prod, record)
        stores = rg.Stores(service=API, prod=self.prod, dev=self.dev)
        rg.note_fix(stores, "r7", "commit d has Fixes-release: r7", FIX_COMMIT, "prod build")
        saved = rr.read_record(self.prod, "r7")["promote_hold"]
        self.assertTrue(saved["forward_only"])
        self.assertEqual(saved["fixed_by"]["commit"], FIX_COMMIT)
        rg.cmd_resume(argparse.Namespace(reason="checked", by="nick"), stores)
        with mock.patch.dict(os.environ, {"RELEASE_AUTO_ROLLBACK": "1"}):
            rl.forward_hold(API, self.prod, rr.read_record(self.prod, "r7"))
        hold = rr.read_record(self.prod, "r7")["promote_hold"]
        self.assertEqual((hold["status"], hold["forward_only"]), ("released", True))

    def test_wait_only_turn_never_supersedes(self) -> None:
        """A wait-only turn waits for older builds and ignores newer ones."""
        payload = {"id": "b2", "createTime": "2026-10-24T12:00:00Z", "status": "WORKING", "substitutions": {}}
        own = guard.Build.from_api({**payload, "substitutions": {"TRIGGER_NAME": "docs-release", "COMMIT_SHA": COMMIT}})
        newer = guard.Build.from_api({**payload, "id": "b3", "createTime": "2026-10-24T12:05:00Z"})
        older = guard.Build.from_api({**payload, "id": "b1", "createTime": "2026-10-24T11:55:00Z"})
        waiting = guard.Guard(api=None, build_id="b2", commit=COMMIT, wait_only=True)
        self.assertEqual(guard.decide_turn(own, [newer, older], waiting._can_supersede(own))[0], "wait")
        self.assertEqual(guard.decide_turn(own, [newer], waiting._can_supersede(own))[0], "proceed")
        self.assertEqual(guard.decide_turn(own, [newer, older])[0], "superseded")
        self.assertIn("turn --wait-only", kit.rendered(DOCS)["release/services/docs.release.yaml"])

    def test_rendered_builds_keep_the_prod_build_contract(self) -> None:
        """Every rendered Railway and static build passes release_kit's prod build contract."""
        for service in (API, DIRECT, DOCS):
            for name, text in kit.rendered(service).items():
                self.assertEqual(kit.prod_build_problems(name, text), [], name)

    def test_rollback_wait_cancels_and_partial_logs_fail(self) -> None:
        """A rollback still building after the wait is cancelled and fails; a missing changed page fails later."""
        self.railway.deployments["d_new"] = deployment("d_new", PROD_ENV, COMMIT)
        self.railway.active[PROD_ENV] = "d_new"
        stuck = deployment("re9", PROD_ENV, OLD_COMMIT, "DEPLOYING")
        with (
            mock.patch.object(self.apis["prod"], "redeploy", return_value=stuck),
            mock.patch.object(rwy, "wait_for_deployment", return_value=stuck),
        ):
            self.railway.deployments["re9"] = stuck
            failures = rl.railway_rollback(API, self.apis["prod"], "d_old", OLD_COMMIT)
        self.assertIn("cancelled", failures[0])
        self.assertIn("cancel", self.railway.mutations())
        site = FakeSite()
        changes = rst.Changes(pages={"/guides/new": "New"})
        soft = rst.check_pages(DOCS.site, COMMIT, changes, None, strict_content=False, get=site)
        self.assertIn("HTTP 404", soft.warnings[0])
        hard = rst.check_pages(DOCS.site, COMMIT, changes, None, strict_content=True, get=site)
        self.assertIn("/guides/new: changed, not in the sitemap, and HTTP 404", hard.failures)

    def test_removals_are_the_page_sets_difference(self) -> None:
        """Removed = pages at base minus pages at head, each with its own ignore file; titles only for live pages."""
        trees = {
            OLD_COMMIT: ["guides/a.mdx", "guides/b.mdx", "api/search/search.mdx", "drafts/x.mdx", ".mintignore"],
            COMMIT: [
                "guides/b.mdx",
                "guides/c.mdx",
                "drafts/x.mdx",
                "drafts/new.mdx",
                "api/search/search.mdx",
                ".mintignore",
            ],
        }
        ignores = {OLD_COMMIT: "drafts/\n", COMMIT: "drafts/\napi/search/\n"}
        files = [
            {"filename": "guides/a.mdx", "status": "removed"},
            {"filename": "guides/c.mdx", "status": "added"},
            {"filename": "drafts/new.mdx", "status": "added"},
            {"filename": ".mintignore", "status": "modified"},
        ]

        def github(path, token):
            """The compare, the trees and the ignore files."""
            if "/compare/" in path:
                return {"files": files}
            if "/git/trees/" in path:
                commit = path.split("/git/trees/")[1].split("?")[0]
                return {"tree": [{"path": item, "type": "blob"} for item in trees[commit]]}
            commit = path.split("ref=")[1]
            return {"content": base64.b64encode(ignores[commit].encode()).decode()}

        with mock.patch.object(rr, "github_json", side_effect=github):
            changes = rst.changed_pages("o/r", OLD_COMMIT, COMMIT, DOCS.site, "t", lambda _: "")
        self.assertEqual(changes.removed, ["/api/search/search", "/guides/a"])
        self.assertEqual(sorted(changes.pages), ["/guides/c"])
        self.assertTrue(rst.ignored("guides/x.draft.mdx", rst.ignore_patterns("*.draft.mdx")))
        with mock.patch.object(
            rr,
            "github_json",
            side_effect=lambda path, token: (
                {"files": files} if "/compare/" in path else (_ for _ in ()).throw(guard.GuardError("HTTP 500"))
            ),
        ):
            unknown = rst.changed_pages(
                "o/r",
                OLD_COMMIT,
                COMMIT,
                DOCS.site,
                "t",
                lambda path: "drafts/\napi/search/\n" if path == ".mintignore" else "",
            )
        self.assertFalse(unknown.count_known)
        result = rst.check_pages(DOCS.site, COMMIT, unknown, 9, strict_content=True, get=FakeSite())
        self.assertFalse(any("sitemap.xml lists" in item for item in result.failures))
        self.assertTrue(any("count is not judged" in item for item in result.warnings))
        self.assertFalse(result.complete)


if __name__ == "__main__":
    unittest.main()
