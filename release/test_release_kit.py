"""Tests of the release kit: config, guards, records, gate, rollback, release-verify and the new-error checks.

Standard library only: `python3 -m unittest discover -s release -p 'test_*.py'`.
They use a fixture service, never the repository's own configs, so they pass in
every repository that vendors the kit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
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
import release_record as rr  # noqa: E402
import release_rollback as rb  # noqa: E402
import release_signals as rs  # noqa: E402
import release_verify as rv  # noqa: E402


def setUpModule():
    """Keep suite polling deterministic; explicit retry tests inject their own clock."""
    rv.ro.MAX_SAMPLES = 1


REPO = "us-central1-docker.pkg.dev/orbit-search/demo/demo"
NEW = "sha256:" + "1" * 64
OLD = "sha256:" + "2" * 64
COMMIT = "a" * 40
PREVIOUS = "b" * 40


def service_data(**overrides) -> dict:
    """A full-tier service config."""
    data = {
        "service": "demo",
        "title": "demo",
        "tier": "full",
        "github_repo": "orbit-search/demo",
        "connected_repository": "orbit-search-demo",
        "image": {
            "repository": REPO,
            "registry_location": "us-central1",
            "registry_repository": "demo",
            "package": "demo",
        },
        "triggers": {"dev": ["demo-dev", "demo-deploy-dev"], "prod": ["demo-prod"], "rollback": "demo-rollback"},
        "environments": {
            "prod": {
                "namespace": "demo",
                "deployments": [
                    {"name": "prod-demo", "container": "app", "selector": "app=prod-demo"},
                    {"name": "prod-demo-worker", "container": "worker", "selector": "app=prod-demo-worker"},
                ],
                "smoke": [
                    {"type": "http_pod", "name": "health", "deployment": "prod-demo", "port": 3000, "path": "/healthz"},
                    {"type": "pods_ready", "deployment": "prod-demo-worker"},
                ],
            },
            "dev": {
                "namespace": "demo",
                "deployments": [{"name": "dev-demo", "container": "app", "selector": "app=dev-demo"}],
            },
        },
        "verify": {
            "metrics": [
                {
                    "name": "http_error_rate",
                    "kind": "rate",
                    "numerator": "sum:errors{x} by {resource_name}.as_count()",
                    "denominator": "sum:hits{x} by {resource_name}.as_count()",
                    "ratio": 2,
                    "absolute": 0.01,
                    "min_samples": 100,
                    "judged": True,
                },
                {
                    "name": "http_p95_seconds",
                    "kind": "value",
                    "value": "p95:latency{x}",
                    "samples": "sum:hits{x}.as_count()",
                    "ratio": 1.5,
                    "absolute": 0.5,
                    "min_samples": 100,
                    "spread": True,
                },
            ],
            "min_judged": 100,
            "signals": {"log_containers": ["app"]},
        },
        "code": {"source_prefixes": ["src/", "app/"], "extensions": [".ts", ".tsx", ".go"]},
    }
    data.update(overrides)
    return data


SERVICE = rc.parse_service(service_data())
PROD = SERVICE.prod


# --------------------------------------------------------------------------- fake cluster


def _deployment(name: str, container: str, image: str, replicas: int = 2) -> dict:
    """A Deployment as kubectl returns it."""
    return {
        "metadata": {"name": name, "generation": 3},
        "spec": {"replicas": replicas, "template": {"spec": {"containers": [{"name": container, "image": image}]}}},
        "status": {"observedGeneration": 3, "updatedReplicas": replicas, "availableReplicas": replicas},
    }


def _pod(name: str, rs_name: str, container: str, digest: str, **extra) -> dict:
    """A pod as kubectl returns it."""
    status = {"name": container, "imageID": f"{REPO}@{digest}", "restartCount": 0}
    if extra.get("waiting"):
        status["state"] = {"waiting": {"reason": extra["waiting"]}}
    return {
        "metadata": {"name": name, "ownerReferences": [{"kind": "ReplicaSet", "name": rs_name}]},
        "status": {
            "phase": "Running",
            "containerStatuses": [status],
            "conditions": [{"type": "Ready", "status": "False" if extra.get("not_ready") else "True"}],
        },
    }


class FakeCluster:
    """Answers the kubectl reads of snapshot_workloads; records every command."""

    def __init__(self, env: rc.Environment = PROD, digest: str = NEW, **pod_extra) -> None:
        """Keep the fake's state."""
        self.env = env
        self.commands: list[list[str]] = []
        self.deployments = {
            item.name: _deployment(item.name, item.container, f"{REPO}:{COMMIT}@{digest}") for item in env.deployments
        }
        self.pods = [
            _pod(f"{item.name}-{index}", f"{item.name}-rs", item.container, digest, **pod_extra)
            for item in env.deployments
            for index in range(2)
        ]
        self.replica_sets = [
            {
                "metadata": {
                    "name": f"{item.name}-rs",
                    "annotations": {rr.REVISION_ANNOTATION: "4"},
                    "ownerReferences": [{"kind": "Deployment", "name": item.name}],
                },
                "spec": {"template": {"spec": {"containers": [{"name": item.container, "image": f"{REPO}@{digest}"}]}}},
            }
            for item in env.deployments
        ]

    def _selected(self, items: list[dict], selector: str) -> list[dict]:
        """The items of the Deployment that the selector names."""
        name = next(item.name for item in self.env.deployments if item.selector == selector)
        return [item for item in items if item["metadata"]["name"].startswith(f"{name}-")]

    def __call__(self, command: list[str], timeout: float = 120) -> str:
        """Answer one recorded command."""
        self.commands.append(command)
        args = [part for part in command[1:] if not part.startswith("--request-timeout")]
        args = args[args.index("-n") + 2 :] if "-n" in args else args
        if args[:2] == ["get", "pods"]:
            return json.dumps({"items": self._selected(self.pods, args[3])})
        if args[:2] == ["get", "replicasets"]:
            return json.dumps({"items": self._selected(self.replica_sets, args[3])})
        if args[:2] == ["get", "deployment"]:
            return json.dumps(self.deployments[args[2]])
        if args[:2] == ["set", "image"]:
            name = args[2].split("/", 1)[1]
            container, image = args[3].split("=", 1)
            self.deployments[name]["spec"]["template"]["spec"]["containers"][0]["image"] = image
            digest = rr.digest_of(image)
            for pod in self.pods:
                if pod["metadata"]["name"].startswith(f"{name}-"):
                    pod["status"]["containerStatuses"][0]["imageID"] = f"{REPO}@{digest}"
            return ""
        raise AssertionError(f"unexpected command {command}")


def _record(**overrides) -> dict:
    """A deployed prod release record."""
    record = {
        "schema_version": 1,
        "service": "demo",
        "environment": "prod",
        "followup": {"owner": {"thread_url": "codex://threads/test-owner", "pr_url": "https://github.com/o/r/pull/1"}},
        "release_id": "20261021T100000Z-aaaaaaaaa",
        "kind": "promote",
        "commit": COMMIT,
        "status": "deployed",
        "times": {"deploy_started": "2026-10-21T10:00:00Z", "deploy_finished": "2026-10-21T10:05:00Z"},
        "images": {"app": f"{REPO}@{NEW}"},
        "previous": {
            "release_id": "20261020T100000Z-bbbbbbbbb",
            "commit": PREVIOUS,
            "images": {"app": f"{REPO}@{OLD}"},
            "workloads": {
                "deployment/prod-demo": {"digest": OLD},
                "deployment/prod-demo-worker": {"digest": OLD},
            },
            "deploy_finished": "2026-10-20T10:05:00Z",
        },
        "commits": [{"sha": COMMIT, "title": "Change the parser (#12)", "pr": 12, "author": "jonnybcw"}],
        "checks": {},
        "verdict": {"status": "pending"},
        "rollback_suggestion": None,
        "rolled_back_by": None,
    }
    record.update(overrides)
    return record


def _store(tmp: str) -> rr.LocalStore:
    """A local record store in a temporary directory."""
    return rr.LocalStore(Path(tmp))


def _save(store: rr.LocalStore, record: dict, latest: bool = True) -> None:
    """Write a record, and point latest.json at it."""
    rr.create_record(store, record, "test")
    if latest:
        rr.point_latest(store, record)


# --------------------------------------------------------------------------- config


class ConfigTest(unittest.TestCase):
    def test_parse(self) -> None:
        """Parse."""
        self.assertEqual(SERVICE.prod.deploy_triggers, ("demo-prod",))
        self.assertEqual(SERVICE.dev.deploy_trigger, "demo-dev")
        self.assertEqual(SERVICE.rollback_build_tag, "demo-rollback")
        self.assertEqual(SERVICE.store, "gs://orbit-search-release-records/demo")
        self.assertEqual(PROD.kube_context, "gke_orbit-search_us-central1_prod")
        self.assertTrue(SERVICE.auto_rollback)

    def test_tiers_and_dev(self) -> None:
        """Tiers and dev."""
        with self.assertRaises(rc.ConfigError):
            rc.parse_service(service_data(tier="check_only"))  # has a dev environment
        data = service_data(tier="check_only")
        del data["environments"]["dev"]
        data["triggers"] = {"prod": ["demo-prod"]}
        service = rc.parse_service(data)
        self.assertFalse(service.auto_rollback)
        self.assertIsNone(service.dev)
        data["tier"] = "full"
        with self.assertRaises(rc.ConfigError):
            rc.parse_service(data)

    def test_code_paths(self) -> None:
        """Code paths."""
        code = SERVICE.code
        self.assertTrue(code.is_source("src/parser.ts"))
        self.assertFalse(code.is_source("src/parser.test.ts"))
        self.assertFalse(code.is_source("src/__tests__/a.ts"))
        self.assertFalse(code.is_source("docs/a.ts"))
        self.assertTrue(code.is_test("pkg/server_test.go"))
        self.assertTrue(code.is_deploy("k8/prod/deployment.yaml"))

    def test_bad_metric(self) -> None:
        """Bad metric."""
        data = service_data()
        data["verify"]["metrics"] = [{"name": "x", "kind": "rate", "ratio": 2, "absolute": 1, "min_samples": 1}]
        with self.assertRaises(rc.ConfigError):
            rc.parse_service(data)


# --------------------------------------------------------------------------- guard


def _build(build_id: str, created: str, status: str = "SUCCESS", **extra) -> guard.Build:
    """A Cloud Build build as the API returns it."""
    return guard.Build(
        id=build_id,
        status=status,
        create_time=created,
        commit=extra.get("commit", COMMIT),
        trigger_name=extra.get("trigger", "demo-prod"),
        start_time=extra.get("start", created),
        finish_time=extra.get("finish", created),
        auto_promote=extra.get("auto_promote", True),
    )


class FirstDeployTest(unittest.TestCase):
    def test_missing_deployment_before_the_first_deploy(self) -> None:
        """Before the first deploy of an environment its Deployments do not exist: the snapshot leaves them out."""
        cluster = FakeCluster()

        def runner(command: list[str], timeout: float = 120) -> str:
            """A cluster where prod-demo-worker does not exist yet."""
            if "prod-demo-worker" in command and "deployment" in command:
                raise rr.RecordError(
                    'kubectl get deployment exited 1: Error from server (NotFound): "prod-demo-worker"'
                )
            return cluster(command)

        clients = rr.Clients(env=PROD, runner=runner)
        self.assertEqual(sorted(rr.snapshot_workloads(clients, missing_ok=True)), ["deployment/prod-demo"])
        with self.assertRaises(rr.RecordError):
            rr.snapshot_workloads(clients)


class GuardTest(unittest.TestCase):
    def test_turn(self) -> None:
        """Turn."""
        own = _build("b", "2026-10-21T10:00:00Z", "WORKING")
        older = _build("a", "2026-10-21T09:00:00Z", "WORKING")
        newer = _build("c", "2026-10-21T11:00:00Z", "QUEUED")
        self.assertEqual(guard.decide_turn(own, [own, older])[0], "wait")
        self.assertEqual(guard.decide_turn(own, [own, older, newer])[0], "superseded")
        self.assertEqual(guard.decide_turn(own, [own])[0], "proceed")
        manual = _build("d", "2026-10-21T11:00:00Z", "QUEUED", auto_promote=False, trigger="demo-dev")
        merge = _build("b", "2026-10-21T10:00:00Z", "WORKING", trigger="demo-dev")
        g = guard.Guard(api=None, build_id="b", commit=COMMIT, promoting_trigger="demo-dev")  # type: ignore[arg-type]
        self.assertEqual(guard.decide_turn(merge, [merge, manual], g._can_supersede(merge))[0], "proceed")

    def test_prod_turn_checks_branch(self) -> None:
        """Prod turn checks branch."""
        data = service_data(tier="check_rollback")
        del data["environments"]["dev"]
        data["triggers"] = {"prod": ["demo-prod"]}
        service = rc.parse_service(data)
        api = mock.Mock()
        api.get_build.return_value = _build("b", "2026-10-21T10:00:00Z", "WORKING")
        api.trigger_builds_around.return_value = []
        api.ongoing_rollbacks.return_value = []
        args = mock.Mock(env="prod", build_id="b", commit=COMMIT, poll_seconds=0, older_timeout_seconds=1)
        with mock.patch.object(guard, "github_compare_status", return_value="diverged"):
            with self.assertRaises(guard.GuardError):
                guard.run_turn(args, service, api)
        with mock.patch.object(guard, "github_compare_status", return_value="behind"):
            guard.run_turn(args, service, api)
        api.ongoing_rollbacks.assert_called_with("demo-rollback")

    def test_dev(self) -> None:
        """Dev."""
        ok = _build("a", "2026-10-21T10:00:00Z", start="2026-10-21T10:00:01Z", finish="2026-10-21T10:09:00Z")
        self.assertEqual(guard.decide_dev([ok])[0], "ok")
        self.assertEqual(guard.decide_dev([])[0], "wait")
        self.assertEqual(guard.decide_dev([_build("a", "2026-10-21T10:00:00Z", "FAILURE")])[0], "failed")
        older = _build("z", "2026-10-21T09:59:00Z", start="2026-10-21T09:59:01Z", finish="2026-10-21T10:05:00Z")
        self.assertEqual(guard.decide_dev([ok, older])[0], "overlap")

    def test_recorded_digest(self) -> None:
        """Recorded digest."""
        import base64

        output = base64.b64encode(json.dumps({"image_digest": NEW}).encode()).decode()
        build = guard.Build.from_api(
            {"id": "x", "createTime": "2026-10-21T10:00:00Z", "results": {"buildStepOutputs": ["", output]}}
        )
        self.assertEqual(build.recorded_digest(), NEW)
        refs = guard.references(SERVICE.image, COMMIT, NEW)
        self.assertEqual(refs[guard.K8S_IMAGE_FILE], f"{REPO}:{COMMIT}@{NEW}")


# --------------------------------------------------------------------------- record


class RecordTest(unittest.TestCase):
    def test_snapshot_and_previous(self) -> None:
        """Snapshot and previous."""
        cluster = FakeCluster(digest=OLD)
        clients = rr.Clients(env=PROD, runner=cluster)
        snapshot = rr.snapshot_workloads(clients)
        self.assertEqual(snapshot["deployment/prod-demo"]["digest"], OLD)
        self.assertEqual(snapshot["deployment/prod-demo"]["live_pods"], 2)
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            previous = rr.previous_release(store, PROD, snapshot)
            self.assertIsNone(previous["release_id"])
            self.assertEqual(previous["images"]["app"], f"{REPO}@{OLD}")
            record = _record(images={"app": f"{REPO}@{OLD}"})
            _save(store, record)
            previous = rr.previous_release(store, PROD, snapshot)
            self.assertEqual(previous["release_id"], record["release_id"])

    def test_update_keeps_other_writes(self) -> None:
        """Update keeps other writes."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _save(store, _record())
            rr.update_record(store, _record()["release_id"], lambda r: r.__setitem__("rolled_back_by", "x"), "a")
            saved = rr.update_record(store, _record()["release_id"], lambda r: r.__setitem__("status", "y"), "b")
            self.assertEqual(saved["rolled_back_by"], "x")
            self.assertEqual([event["event"] for event in saved["history"]], ["test", "a", "b"])
            with self.assertRaises(rr.RecordError):
                rr.create_record(store, _record(), "again")


# --------------------------------------------------------------------------- gate


def _stores(tmp: str) -> rg.Stores:
    """Local prod and dev record stores."""
    return rg.Stores(service=SERVICE, prod=rr.LocalStore(Path(tmp) / "prod"), dev=rr.LocalStore(Path(tmp) / "dev"))


def _verified(stores: rg.Stores, commit: str = COMMIT, digest: str = NEW) -> None:
    """Write a dev verify index that marks the commit passed."""
    stores.dev.write(
        rg.dev_commit_index(commit),
        {"commit": commit, "verdict": {"status": "passed"}, "images": {"app": f"{REPO}@{digest}"}},
        0,
    )


class GateTest(unittest.TestCase):
    def setUp(self) -> None:
        """Prepare the fakes and the temporary store."""
        self.tmp = tempfile.TemporaryDirectory()
        self.stores = _stores(self.tmp.name)
        self.compare = mock.patch.object(rg, "github_compare_status", return_value="ahead")
        self.compare.start()

    def tearDown(self) -> None:
        """Remove what the test created."""
        self.compare.stop()
        self.tmp.cleanup()

    def test_unverified_then_go(self) -> None:
        """Unverified then go."""
        self.assertEqual(rg.decide(self.stores, COMMIT, "t").status, "unverified")
        _verified(self.stores)
        self.assertTrue(rg.decide(self.stores, COMMIT, "t").go)
        moved = rg.decide(self.stores, COMMIT, "t", images={"app": f"{REPO}@{OLD}"})
        self.assertEqual(moved.status, "unverified")

    def test_freeze(self) -> None:
        """Freeze."""
        _verified(self.stores)
        self.stores.prod.write(rg.FREEZE_OBJECT, {"frozen": True, "by": "n", "reason": "incident"}, 0)
        decision = rg.decide(self.stores, COMMIT, "t")
        self.assertEqual(decision.status, "frozen")
        self.assertIn("unfreeze", decision.resume_hint)
        self.assertEqual(rg.decide_direct(self.stores, COMMIT, "t").status, "frozen")

    def test_hold_and_trailer(self) -> None:
        """Hold and trailer."""
        _verified(self.stores)
        bad = _record(commit=PREVIOUS, release_id="20261021T090000Z-bbbbbbbbb")
        rollback = _record(
            release_id="20261021T093000Z-ccccccccc",
            kind="rollback",
            commit="c" * 40,
            promote_hold={
                "status": "held",
                "rolled_back_release": bad["release_id"],
                "rolled_back_commit": PREVIOUS,
                "prs": [{"pr": 12, "sha": PREVIOUS, "title": "Change the parser (#12)"}],
                "resume": "x",
            },
        )
        _save(self.stores.prod, bad, latest=False)
        _save(self.stores.prod, rollback)
        with mock.patch.object(rg, "commit_messages", return_value=[(COMMIT, "unrelated")]):
            self.assertEqual(rg.decide(self.stores, COMMIT, "t").status, "held")
            self.assertEqual(rg.decide_direct(self.stores, COMMIT, "t").status, "held")
        trailer = [(COMMIT, f"Fix the parser\n\nFixes-release: {bad['release_id']}")]
        with mock.patch.object(rg, "commit_messages", return_value=trailer):
            decision = rg.decide(self.stores, COMMIT, "t")
            self.assertTrue(decision.go)
            self.assertEqual(decision.fix_seen[0], rollback["release_id"])
        revert = [(COMMIT, 'Revert "Change the parser (#12)"\n\nThis reverts commit ' + PREVIOUS[:12])]
        with mock.patch.object(rg, "commit_messages", return_value=revert):
            self.assertTrue(rg.decide_direct(self.stores, COMMIT, "t").go)
        rg.release_hold(self.stores, rollback["release_id"], "test", "n")
        with mock.patch.object(rg, "commit_messages", return_value=[]):
            self.assertTrue(rg.decide(self.stores, COMMIT, "t").go)

    def test_request_promote_dry_run(self) -> None:
        """Request promote dry run."""
        _verified(self.stores)
        args = mock.Mock(commit=COMMIT, build_id="b1", trigger_name="demo-dev", auto_promote="true", dry_run=True)
        with mock.patch.object(rr, "github_token", return_value="t"), mock.patch.object(rr, "post_slack") as slack:
            self.assertEqual(rg.cmd_request_promote(args, self.stores), rg.EXIT_GO)
        self.assertTrue(slack.call_args[0][0].startswith(":test_tube: TEST RUN, nothing is wrong"))
        index, _ = self.stores.dev.read(rg.dev_commit_index(COMMIT))
        self.assertNotIn("promote", index)

    def test_stale(self) -> None:
        """Stale."""
        _verified(self.stores)
        _save(self.stores.prod, _record(commit="d" * 40))
        with mock.patch.object(rg, "contains", return_value=True):
            self.assertEqual(rg.decide(self.stores, COMMIT, "t").status, "stale")


# --------------------------------------------------------------------------- rollback


class RollbackTest(unittest.TestCase):
    def test_plan_and_apply(self) -> None:
        """Plan and apply."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            _save(store, record)
            cluster = FakeCluster(digest=NEW)
            clients = rr.Clients(env=PROD, runner=cluster)
            latest, target = rb.resolve_target(store, "previous")
            plan = rb.build_plan(SERVICE, latest, target, rr.snapshot_workloads(clients), None)
            self.assertEqual([step.action for step in plan.steps], ["set", "set"])
            self.assertEqual(plan.steps[0].target, f"{REPO}:{PREVIOUS}@{OLD}")
            with mock.patch.object(rr, "wait_rollouts", return_value=[]):
                self.assertEqual(rb.apply_plan(plan, clients), [])
            after = rr.snapshot_workloads(clients)
            self.assertEqual(after["deployment/prod-demo"]["digest"], OLD)
            targets = {step.workload: rr.digest_of(step.target) for step in plan.steps}
            self.assertEqual(rv.check_images(record, after, targets=targets).status, "passed")

    def test_expect_latest_refuses(self) -> None:
        """Expect latest refuses."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _save(store, _record())
            clients = rr.Clients(env=PROD, runner=FakeCluster())
            args = mock.Mock(dry_run=True, reason="", target="previous", build_id="x", expect_latest="wrong-id")
            with (
                mock.patch.object(rb, "assert_alone"),
                mock.patch.object(rb, "missing_images", return_value=[]),
                mock.patch.object(rb, "_token", return_value=None),
            ):
                with self.assertRaises(rr.RecordError) as raised:
                    rb.cmd_apply(args, SERVICE, store, clients)
                self.assertIn("prod moved on", str(raised.exception))
                args.expect_latest = _record()["release_id"]
                self.assertEqual(rb.cmd_apply(args, SERVICE, store, clients), 0)


class LockTest(unittest.TestCase):
    def test_unlock_cli_is_idempotent_and_preserves_other_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            with mock.patch.object(rr, "open_from_args", return_value=(SERVICE, SERVICE.prod, store, mock.Mock())):
                self.assertEqual(rr.main(["unlock", "--env", "prod", "--build-id", "own"]), 0)
                rr.acquire_lock(store, "other", "deploy", lambda _: True, wait_seconds=1)
                self.assertEqual(rr.main(["unlock", "--env", "prod", "--build-id", "own"]), 0)
                self.assertEqual(store.read(rr.LOCK_OBJECT)[0]["holder"], "other")
                self.assertEqual(rr.main(["unlock", "--env", "prod", "--build-id", "other"]), 0)
                self.assertEqual(rr.main(["unlock", "--env", "prod", "--build-id", "other"]), 0)
                self.assertFalse(store.read(rr.LOCK_OBJECT)[0]["holder"])

    """One build at a time changes prod: deploys from the gate to `finish`, rollbacks during apply."""

    def test_acquire_wait_take_over_release(self) -> None:
        """Acquire wait take over release."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            running = {"deploy1"}
            alive = lambda build_id: build_id in running  # noqa: E731
            rr.acquire_lock(store, "deploy1", "deploy", alive, wait_seconds=60)
            self.assertEqual(rr.lock_holder(store, alive)["holder"], "deploy1")
            # A second build waits while the holder runs and gives up at its deadline.
            ticks = iter(range(0, 1000, 30))
            with self.assertRaises(rr.RecordError) as raised:
                rr.acquire_lock(
                    store, "rb1", "rollback", alive, wait_seconds=60, sleep=lambda _: None, clock=lambda: next(ticks)
                )
            self.assertIn("deploy build deploy1 still holds", str(raised.exception))
            # The holder ended without freeing the lock: the next build takes it over.
            running.clear()
            self.assertIsNone(rr.lock_holder(store, alive))
            running.add("rb1")
            rr.acquire_lock(store, "rb1", "rollback", alive, wait_seconds=60)
            lock, _ = store.read(rr.LOCK_OBJECT)
            self.assertEqual((lock["holder"], lock["took_over_from"]), ("rb1", "deploy1"))
            rr.release_lock(store, "deploy1")  # not the holder: nothing changes
            self.assertEqual(rr.lock_holder(store, alive)["holder"], "rb1")
            rr.release_lock(store, "rb1")
            self.assertIsNone(rr.lock_holder(store, alive))

    def test_only_one_of_two_racing_builds_wins(self) -> None:
        """Only one of two racing builds wins."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            real_write = store.write

            def racing_write(name, data, generation):
                # Another build writes first, at the same generation this one read.
                """Let another build write first, at the same generation."""
                if data.get("holder") == "b":
                    real_write(name, {"holder": "a", "kind": "deploy", "at": "x"}, generation)
                return real_write(name, data, generation)

            ticks = iter(range(0, 1000, 30))
            with mock.patch.object(store, "write", racing_write), self.assertRaises(rr.RecordError):
                rr.acquire_lock(
                    store,
                    "b",
                    "deploy",
                    lambda _: True,
                    wait_seconds=60,
                    sleep=lambda _: None,
                    clock=lambda: next(ticks),
                )
            self.assertEqual(store.read(rr.LOCK_OBJECT)[0]["holder"], "a")

    def test_gate_keeps_the_lock_only_on_go(self) -> None:
        """Gate keeps the lock only on go."""
        with tempfile.TemporaryDirectory() as tmp:
            stores = _stores(tmp)
            api = mock.Mock()
            api.ongoing_rollbacks.return_value = []
            waits = {"api": api, "alive": lambda _: True, "sleep": lambda _: None}
            with rg.gate_lock(stores, "deploy1", **waits) as lock:
                self.assertEqual(stores.prod.read(rr.LOCK_OBJECT)[0]["holder"], "deploy1")
            self.assertFalse(stores.prod.read(rr.LOCK_OBJECT)[0]["holder"])
            with rg.gate_lock(stores, "deploy2", **waits) as lock:
                lock.keep()
            self.assertEqual(stores.prod.read(rr.LOCK_OBJECT)[0]["holder"], "deploy2")

    def test_deploy_gives_the_lock_to_a_new_rollback(self) -> None:
        """Deploy gives the lock to a new rollback."""
        with tempfile.TemporaryDirectory() as tmp:
            stores = _stores(tmp)
            api = mock.Mock()
            rollback = mock.Mock(describe=lambda: "rb1")
            # The rollback starts after the wait and before the lock: the deploy lets it go first.
            api.ongoing_rollbacks.side_effect = [[], [rollback], [rollback], [], []]
            holders: list = []
            real_release = rr.release_lock

            def release(store, holder):
                """Note the lock holder, then free the lock."""
                holders.append(store.read(rr.LOCK_OBJECT)[0]["holder"])
                real_release(store, holder)

            with mock.patch.object(rr, "release_lock", release):
                rg.take_prod_lock(stores, "deploy1", api=api, alive=lambda _: True, sleep=lambda _: None)
            self.assertEqual(holders, ["deploy1"])
            self.assertEqual(stores.prod.read(rr.LOCK_OBJECT)[0]["holder"], "deploy1")

    def test_rollback_apply_holds_the_lock(self) -> None:
        """Rollback apply holds the lock."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _save(store, _record())
            clients = rr.Clients(env=PROD, runner=FakeCluster(digest=NEW))
            args = mock.Mock(dry_run=False, reason="bad", target="previous", build_id="rb1", expect_latest="")
            args.requested_by = "test"
            seen: list = []

            def apply(*_args):
                """Note the lock holder while the apply runs."""
                seen.append(store.read(rr.LOCK_OBJECT)[0]["holder"])
                return 0

            with mock.patch.object(rb, "_apply", apply):
                self.assertEqual(rb.cmd_apply(args, SERVICE, store, clients, alive=lambda _: True), 0)
            self.assertEqual(seen, ["rb1"])
            self.assertFalse(store.read(rr.LOCK_OBJECT)[0]["holder"])
            args.build_id = ""
            with self.assertRaises(rr.RecordError):
                rb.cmd_apply(args, SERVICE, store, clients, alive=lambda _: True)

    def test_finish_frees_the_deploy_lock(self) -> None:
        """Finish frees the deploy lock."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(status="deploying", build={"id": "deploy1"})
            _save(store, record, latest=False)
            rr.acquire_lock(store, "deploy1", "deploy", lambda _: True, wait_seconds=60)
            args = mock.Mock(release_id_file=None, release_id=record["release_id"], status="deployed")
            clients = rr.Clients(env=PROD, runner=FakeCluster(digest=NEW))
            rr.cmd_finish(args, PROD, store, clients)
            self.assertFalse(store.read(rr.LOCK_OBJECT)[0]["holder"])

    def test_watch_waits_for_a_lock_holder(self) -> None:
        """Watch waits for a lock holder."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(build={"id": "own"})
            _save(store, record)
            rr.acquire_lock(store, "deploy2", "deploy", lambda _: True, wait_seconds=60)
            api = mock.Mock()
            api.ongoing_rollbacks.return_value = []
            api.list_builds.return_value = []
            with (
                mock.patch.object(rv, "GoogleApi", return_value=api),
                mock.patch.object(rr, "build_alive", return_value=lambda _: True),
            ):
                ended, busy = rv.prod_state(SERVICE, store, record)
            self.assertIsNone(ended)
            self.assertIn("deploy2 holds the prod lock", busy)


# --------------------------------------------------------------------------- release-verify


class SmokeTest(unittest.TestCase):
    def test_images(self) -> None:
        """Images."""
        snapshot = rr.snapshot_workloads(rr.Clients(env=PROD, runner=FakeCluster(digest=NEW)))
        self.assertEqual(rv.check_images(_record(), snapshot).status, "passed")
        snapshot = rr.snapshot_workloads(rr.Clients(env=PROD, runner=FakeCluster(digest=OLD)))
        self.assertEqual(rv.check_images(_record(), snapshot).status, "failed")
        bad = rr.snapshot_workloads(rr.Clients(env=PROD, runner=FakeCluster(waiting="CrashLoopBackOff")))
        self.assertIn("CrashLoopBackOff", " ".join(rv.check_images(_record(), bad).details))

    def test_contains_text_deep_in_a_large_page(self) -> None:
        """A title at byte 254,765 of an 840 KB page (developer.orbitsearch.com, 10-26) is found; past the read cap
        the failure says how much of the body it read."""
        page = b"<html><head><style>" + b"x" * 254_700 + b"</style><title>Orbit for Developers</title>" + b"y" * 585_000

        class Response:
            """A urlopen response that honors read(n)."""

            status = 200

            def __init__(self, body: bytes) -> None:
                self.body = body

            def read(self, size: int = -1) -> bytes:
                return self.body if size < 0 else self.body[:size]

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                return None

        with mock.patch.object(urllib.request, "urlopen", return_value=Response(page)):
            status, body = rv.http_get(urllib.request.Request("https://developer.example.com/"))
        self.assertEqual(len(body), len(page))
        self.assertTrue(rv._expect_ok(status, body, {"contains": "Orbit for Developers"})[0])
        huge = b"z" * (rv.HTTP_READ_BYTES + 10) + b"Orbit for Developers"
        with mock.patch.object(urllib.request, "urlopen", return_value=Response(huge)):
            status, body = rv.http_get(urllib.request.Request("https://developer.example.com/"))
        ok, line = rv._expect_ok(status, body, {"contains": "Orbit for Developers"})
        self.assertFalse(ok)
        self.assertIn(f"in its first {rv.HTTP_READ_BYTES} bytes", line)

    def test_service_smoke(self) -> None:
        """Service smoke."""
        clients = rr.Clients(env=PROD, runner=FakeCluster())
        snapshot = rr.snapshot_workloads(clients)
        calls = []

        def pod_get(namespace: str, pod: str, port: int, path: str) -> tuple[int, bytes]:
            """Answer as the pod proxy does."""
            calls.append((namespace, pod, port, path))
            return (200, b"ok") if pod.endswith("-0") else (503, b"down")

        checks = rv.smoke_checks(clients, snapshot, pod_get=pod_get)
        self.assertEqual([check.name for check in checks], ["health", "prod-demo-worker ready"])
        self.assertEqual(checks[0].status, "failed")
        self.assertEqual(len(calls), 2)
        self.assertEqual(checks[1].status, "passed")

    def test_expect(self) -> None:
        """Expect."""
        self.assertTrue(rv._expect_ok(204, b"", {})[0])
        self.assertFalse(rv._expect_ok(200, b'{"status":"down"}', {"expect_json": {"status": "ok"}})[0])
        self.assertFalse(rv._expect_ok(200, b"<html>", {"contains": "Orbit"})[0])

    def test_public(self) -> None:
        """Public."""

        def get(request: urllib.request.Request) -> tuple[int, bytes]:
            """Answer with the next canned response."""
            return 200, b"<title>Orbit</title>"

        check = rv.check_http_public({"url": "https://example.test/", "contains": "Orbit"}, get)
        self.assertEqual(check.status, "passed")


class FakeDatadog:
    """Scalar answers by query and window: baseline (before 10:00) or candidate."""

    def __init__(self, base: dict[str, dict[str, float]], cand: dict[str, dict[str, float]], hourly=None) -> None:
        """Keep the fake's state."""
        self.base, self.cand, self.hourly = base, cand, hourly or {}

    def scalar(self, query: str, aggregator: str, start: datetime, end: datetime) -> dict[str, float]:
        """The canned value of a query."""
        release = datetime(2026, 10, 21, 10, tzinfo=rr.UTC)
        if end <= release and (end - start) <= timedelta(hours=1) and query in self.hourly:
            return self.hourly[query]
        source = self.cand if start >= release else self.base
        return source.get(query, {})


ERRORS, HITS = "sum:errors{x} by {resource_name}.as_count()", "sum:hits{x} by {resource_name}.as_count()"
P95, P95_N = "p95:latency{x}", "sum:hits{x}.as_count()"
BASELINE = (datetime(2026, 10, 20, 10, tzinfo=rr.UTC), datetime(2026, 10, 21, 10, tzinfo=rr.UTC))
WINDOW = (datetime(2026, 10, 21, 10, 5, tzinfo=rr.UTC), datetime(2026, 10, 21, 11, 5, tzinfo=rr.UTC))


class MetricsTest(unittest.TestCase):
    def test_per_group_rate(self) -> None:
        """Per group rate."""
        dd = FakeDatadog(
            {ERRORS: {"GET /a": 1, "GET /b": 1}, HITS: {"GET /a": 1000, "GET /b": 1000}},
            {ERRORS: {"GET /a": 1, "GET /b": 300}, HITS: {"GET /a": 1000, "GET /b": 1000}},
        )
        with mock.patch.object(rv, "runtime_change", return_value=(True, "src")):
            findings = rv.judge_metrics(SERVICE, dd, _record(), BASELINE, WINDOW, None)
        rates = {f.scope: f.status for f in findings if f.metric == "http_error_rate"}
        self.assertEqual(rates, {"demo GET /a": "ok", "demo GET /b": "exceeded"})

    def test_spread_and_runtime(self) -> None:
        """Spread and runtime."""
        dd = FakeDatadog({P95: {"*": 1.0}, P95_N: {"*": 1000}}, {P95: {"*": 3.0}, P95_N: {"*": 1000}})
        dd.hourly = {P95: {"*": 2.5}, P95_N: {"*": 100}}
        with mock.patch.object(rv, "runtime_change", return_value=(True, "src")):
            findings = rv.judge_metrics(SERVICE, dd, _record(), BASELINE, WINDOW, None)
        self.assertEqual([f.status for f in findings if f.metric == "http_p95_seconds"], ["within_spread"])
        dd.hourly = {P95: {"*": 1.1}, P95_N: {"*": 100}}
        with mock.patch.object(rv, "runtime_change", return_value=(False, "docs only")):
            findings = rv.judge_metrics(SERVICE, dd, _record(), BASELINE, WINDOW, None)
        self.assertEqual([f.status for f in findings if f.metric == "http_p95_seconds"], ["no_runtime_change"])

    def test_runtime_files(self) -> None:
        """Runtime files."""
        files = [
            {"filename": "release/release_verify.py", "patch": "+x"},
            {"filename": "src/a.test.ts", "patch": "+x"},
            {"filename": "README.md", "patch": "+x"},
            {"filename": "k8/prod/deployment.yaml", "patch": '+          value: "unreleased"'},
            {
                "filename": "cloudbuild.prod.yaml",
                "patch": "+              python3 release/release_gate.py check-direct \\",
            },
        ]
        self.assertEqual(rv.runtime_files(SERVICE.code, files), [])
        self.assertEqual(rv.runtime_files(SERVICE.code, [{"filename": "src/a.ts", "patch": "+x"}]), ["src/a.ts"])


class VerdictTest(unittest.TestCase):
    def _result(self, **overrides) -> dict:
        """A check result, with defaults for what the test leaves out."""
        result = {
            "phase": "window",
            "at": "2026-10-21T11:05:00Z",
            "candidate_minutes": 60,
            "checks": [],
            "regressions": [],
            "warnings": [],
            "judged": 500,
            "signals_checked": ["logs"],
            "result": "passed",
        }
        result.update(overrides)
        return result

    def test_availability_uncertainty_cannot_pass_with_healthy_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            _save(store, record)
            result = self._result(availability={"status": "unconfirmed"}, result="inconclusive")
            saved = rv.apply_result(SERVICE, store, record, result, final=True, token=None)
            self.assertEqual(saved["verdict"]["status"], "inconclusive")
            self.assertTrue(saved["verdict"]["unchecked"])
            self.assertFalse(saved.get("rollback_suggestion"))
            self.assertEqual(rv.final_verdict(SERVICE, result)[0], "inconclusive")
            # An independent error still fails, even while the cluster's capacity is uncertain.
            result.update(result="failed", regressions=[{"kind": "metric", "text": "error rate regressed"}])
            saved = rv.apply_result(SERVICE, store, saved, result, final=True, token=None)
            self.assertEqual(saved["verdict"]["status"], "failed")

    def test_late_checkpoint_keeps_the_scheduled_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            _save(store, record)
            result = self._result(candidate_minutes=18, checkpoint_minutes=15)
            saved = rv.apply_result(SERVICE, store, record, result, final=False, token=None)
            self.assertIn("window_15m", saved["checks"])
            self.assertEqual(saved["followup"]["checkpoints"]["window_15m"]["state"], "completed")
            self.assertNotIn("window_18m", saved["checks"])

    def _run_owned_watch(
        self,
        store,
        record,
        *,
        result=None,
        error=None,
        history=None,
        dd=None,
        checkpoints="15,60",
    ):
        args = argparse.Namespace(
            release_id=record["release_id"],
            checkpoints=checkpoints,
            extend_minutes=15,
            max_window_minutes=180,
            retry_completed=False,
        )
        with (
            mock.patch.object(rv, "_datadog", return_value=(dd, None)),
            mock.patch.object(rv, "_token", return_value=None),
            mock.patch.object(rv, "wait_until_settled", return_value=None),
            mock.patch.object(rv, "prod_state", return_value=(None, None)),
            mock.patch.object(rv, "builds_since_rollback", return_value=history or []),
            mock.patch.object(rv, "evaluate", side_effect=error, return_value=result or self._result()) as evaluate,
            mock.patch.object(rv, "print_result"),
            mock.patch.object(rr, "post_slack", return_value=True),
        ):
            outcome = rv.cmd_watch(args, SERVICE, SERVICE.prod, store, mock.Mock())
            return outcome, evaluate.call_count

    def test_unclaimed_release_runs_both_checkpoints_and_the_author_owns_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(followup={})
            _save(store, record)
            outcome, count = self._run_owned_watch(store, record)
            self.assertEqual(count, 2)
            self.assertEqual(outcome, rv.EXIT_OK)
            saved = rr.read_record(store, record["release_id"])
            self.assertEqual(saved["verdict"]["status"], "passed")
            self.assertEqual(saved["followup"]["state"], "author_owned")

    def test_resumed_watch_preserves_completed_15m_and_only_checks_60m(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            rv.ro.merge_plan(record, [15, 60])
            rv.ro.note_checkpoint(record, {"at": "original", "result": "passed"}, "window_15m")
            record["checks"]["window_15m"] = {"at": "original", "result": "passed"}
            _save(store, record)
            outcome, count = self._run_owned_watch(store, record)
            self.assertEqual((outcome, count), (rv.EXIT_OK, 1))
            saved = rr.read_record(store, record["release_id"])
            self.assertEqual(saved["checks"]["window_15m"]["at"], "original")
            self.assertEqual(saved["followup"]["checkpoints"]["window_15m"]["completed_at"], "original")
            self.assertIn("window_60m", saved["checks"])

    def test_final_checker_exception_is_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            _save(store, record)
            outcome, count = self._run_owned_watch(store, record, error=rr.RecordError("cluster unavailable"))
            self.assertEqual((outcome, count), (rv.EXIT_FAILED, 2))
            self.assertEqual(rr.read_record(store, record["release_id"])["verdict"]["status"], "inconclusive")

    def test_terminal_mutation_during_observation_never_rolls_back_the_old_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            _save(store, record)
            with mock.patch.object(rv, "on_failure") as rollback:
                outcome, _ = self._run_owned_watch(store, record, history=["CANCELLED build that mutated prod"])
                rollback.assert_not_called()
            self.assertEqual(outcome, rv.EXIT_FAILED)
            saved = rr.read_record(store, record["release_id"])
            self.assertEqual(saved["verdict"]["status"], "inconclusive")
            self.assertIn("CANCELLED", saved["verdict"]["reason"])

    def test_final_checker_and_receipt_outage_is_still_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            _save(store, record)
            with mock.patch.object(rv, "_record_checkpoint_error"):
                outcome, _ = self._run_owned_watch(store, record, error=rr.RecordError("GCS unavailable"))
            self.assertEqual(outcome, rv.EXIT_FAILED)
            self.assertEqual(rr.read_record(store, record["release_id"])["verdict"]["status"], "pending")

    def test_failed_deploy_of_an_unclaimed_release_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(verdict={"status": "failed"}, followup={})
            _save(store, record)
            with mock.patch.object(rv, "on_failure") as rollback:
                outcome, count = self._run_owned_watch(store, record)
                rollback.assert_called_once()
            self.assertEqual((outcome, count), (rv.EXIT_OK, 0))
            self.assertEqual(rr.read_record(store, record["release_id"])["followup"]["state"], "author_owned")

    def test_manual_final_health_pass_of_an_unclaimed_release_is_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(followup={})
            _save(store, record)
            args = argparse.Namespace(
                release_id=record["release_id"],
                phase="window",
                no_metrics=True,
                now=None,
                wait_rollouts_seconds=0,
                force_fail="",
                final=True,
                failed_marker="",
            )
            with (
                mock.patch.object(rv, "_token", return_value=None),
                mock.patch.object(rv, "prod_state", return_value=(None, None)),
                mock.patch.object(rv, "builds_since_rollback", return_value=[]),
                mock.patch.object(rv, "evaluate", return_value=self._result()),
                mock.patch.object(rv, "print_result"),
                mock.patch.object(rr, "post_slack", return_value=True),
            ):
                self.assertEqual(rv.cmd_check(args, SERVICE, SERVICE.prod, store, mock.Mock()), rv.EXIT_OK)
            saved = rr.read_record(store, record["release_id"])
            self.assertEqual(saved["verdict"]["status"], "passed")
            self.assertEqual(saved["followup"]["state"], "author_owned")

    def test_owner_receipt_outage_posts_nothing(self):
        """The author owns an unclaimed release even when the record write fails; the alerts still mention them."""
        with (
            mock.patch.object(rr, "update_record", side_effect=rr.RecordError("GCS down")),
            mock.patch.object(rr, "post_slack", return_value=True) as slack,
            mock.patch.object(rv, "load_people", return_value=PEOPLE),
        ):
            self.assertTrue(rv.require_owner(mock.Mock(), _record(followup={})))
            self.assertEqual(rv.owner_mentions(_record(followup={})), ["<@UJONNY>"])
        slack.assert_not_called()

    def test_reverse_resume_cannot_erase_a_completed_60m_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(verdict={"status": "passed", "phase": "window_60m"})
            rv.ro.merge_plan(record, [15, 60])
            rv.ro.note_checkpoint(record, {"at": "original", "result": "passed"}, "window_60m")
            _save(store, record)
            outcome, count = self._run_owned_watch(store, record)
            self.assertEqual((outcome, count), (rv.EXIT_OK, 1))
            saved = rr.read_record(store, record["release_id"])
            self.assertEqual(saved["verdict"], {"status": "passed", "phase": "window_60m"})
            self.assertEqual(saved["followup"]["checkpoints"]["window_60m"]["completed_at"], "original")

    def test_adaptive_checkpoint_survives_process_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            _save(store, record)
            with self.assertRaises(KeyboardInterrupt):
                self._run_owned_watch(
                    store,
                    record,
                    checkpoints="60",
                    dd=object(),
                    error=[
                        self._result(judged=0),
                        KeyboardInterrupt("simulated process stop"),
                    ],
                )
            saved = rr.read_record(store, record["release_id"])
            self.assertEqual(saved["verdict"]["status"], "pending")
            self.assertEqual(saved["followup"]["extensions_minutes"], [75])
            self.assertEqual(saved["followup"]["checkpoints"]["window_60m"]["state"], "completed")
            outcome, count = self._run_owned_watch(store, saved, checkpoints="60")
            self.assertEqual((outcome, count), (rv.EXIT_OK, 1))
            final = rr.read_record(store, record["release_id"])
            self.assertEqual(
                (final["verdict"]["status"], final["verdict"]["phase"]),
                ("passed", "window_75m"),
            )

    def test_completed_manual_attempt_still_requires_a_final_decision(self):
        for judged, expected in [(0, rv.EXIT_FAILED), (500, rv.EXIT_OK)]:
            with self.subTest(judged=judged), tempfile.TemporaryDirectory() as tmp:
                store = _store(tmp)
                record = _record()
                record["checks"]["window_60m"] = self._result(judged=judged)
                rv.ro.note_checkpoint(record, {"at": "manual", "result": "passed"}, "window_60m")
                _save(store, record)
                outcome, count = self._run_owned_watch(
                    store, record, checkpoints="60", result=self._result(judged=judged)
                )
                self.assertEqual((outcome, count), (expected, 1))
                saved = rr.read_record(store, record["release_id"])
                self.assertNotEqual(saved["verdict"]["status"], "pending")
                prior = saved["followup"]["checkpoints"]["window_60m"]["attempt_history"][0]
                self.assertEqual(prior["completed_at"], "manual")
                self.assertEqual(prior["check"]["judged"], judged)

    def test_historical_manual_check_never_writes_current_recovery_or_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            _save(store, record)
            args = argparse.Namespace(
                release_id=record["release_id"],
                phase="window",
                no_metrics=True,
                now="2026-10-21T10:20:00Z",
                wait_rollouts_seconds=0,
                force_fail="",
                final=True,
                failed_marker="",
            )
            result = self._result(recovered_signals=["active-today"])
            with (
                mock.patch.object(rv, "_token", return_value=None),
                mock.patch.object(rv, "evaluate", return_value=result) as evaluate,
                mock.patch.object(rv, "print_result"),
                mock.patch.object(rr, "update_record") as update,
                mock.patch.object(rv, "note_recovered") as recovery,
            ):
                self.assertEqual(
                    rv.cmd_check(args, SERVICE, SERVICE.prod, store, mock.Mock()),
                    rv.EXIT_OK,
                )
            update.assert_not_called()
            recovery.assert_not_called()
            self.assertIsNone(evaluate.call_args.kwargs["observation_state"])

    def test_final(self) -> None:
        """Final."""
        self.assertEqual(rv.final_verdict(SERVICE, self._result())[0], "passed")
        self.assertEqual(rv.final_verdict(SERVICE, self._result(judged=10))[0], "inconclusive")
        data = service_data()
        data["verify"] = {"signals": {"log_containers": ["app"]}}
        quiet = rc.parse_service(data)
        self.assertEqual(rv.final_verdict(quiet, self._result(judged=0))[0], "passed")
        self.assertEqual(rv.final_verdict(quiet, self._result(signals_checked=[]))[0], "inconclusive")

    def test_apply_failed_and_warnings(self) -> None:
        """Apply failed and warnings."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _save(store, _record())
            warning = {"name": "log:x", "text": "new line", "suspects": [], "candidates": []}
            saved = rv.apply_result(
                SERVICE, store, _record(), self._result(warnings=[warning]), final=False, token=None
            )
            self.assertEqual(saved["verdict"]["status"], "pending")
            regression = {"kind": "check", "name": "health", "text": "health: HTTP 503"}
            saved = rv.apply_result(
                SERVICE,
                store,
                _record(),
                self._result(result="failed", regressions=[regression]),
                final=True,
                token=None,
            )
            self.assertEqual(saved["verdict"]["status"], "failed")
            self.assertEqual(saved["rollback_suggestion"]["target"]["commit"], PREVIOUS)
            self.assertIn("log:x", saved["warnings"])

    def test_metric_over_a_chain_rolls_back_past_the_oldest(self) -> None:
        """A new error names the newer release, but a check failed over both: the target is before the oldest."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            older = _record(release_id="20261021T080000Z-ddddddddd", commit="d" * 40, previous={"commit": "e" * 40})
            newer = _record(previous={"release_id": older["release_id"], "commit": "d" * 40})
            _save(store, older, latest=False)
            _save(store, newer)
            regressions = [
                {"kind": "new_error", "name": "log:x", "text": "new line"},
                {"kind": "check", "name": "health", "text": "health: HTTP 503"},
            ]
            result = self._result(
                result="failed", regressions=regressions, chain=[newer["release_id"], older["release_id"]]
            )
            named = {"release_id": older["release_id"], "commit": "d" * 40, "rollback_to": None}
            with mock.patch.object(rs, "rollback_target", return_value=named):
                saved = rv.apply_result(SERVICE, store, newer, result, final=True, token=None)
            target = saved["rollback_suggestion"]["target"]
            self.assertEqual(target["rollback_to"], f"previous-of:{older['release_id']}")
            self.assertEqual(target["commit"], "e" * 40)


class AlertTest(unittest.TestCase):
    def test_rollback_alert_pings_author(self) -> None:
        """Rollback alert pings author."""
        record = _record(
            verdict={"status": "failed", "phase": "window_15m", "reasons": ["new log line"]},
            rollback_suggestion={
                "target": {"commit": PREVIOUS},
                "suspected_prs": [
                    {"sha": COMMIT, "pr": 12, "title": "Change the parser (#12)", "author": "jonnybcw", "score": 6}
                ],
            },
        )
        people = {"slack_ids": {"jonnybcw": "U095N7BBQKD"}, "fallback": []}
        # The same in a private repository (vendored map) and a public one (no file, the secret's env).
        with (
            mock.patch.object(rv, "PEOPLE_FILE", Path(tempfile.gettempdir()) / "no-release-people.json"),
            mock.patch.dict(os.environ, {"RELEASE_PEOPLE_JSON": json.dumps(people)}),
        ):
            text, blocks = rv.rollback_alert(SERVICE, record, "https://build", "gs://x")
        self.assertIn("<@U095N7BBQKD>", text)
        self.assertIn("Fixes-release: 20261021T100000Z-aaaaaaaaa", json.dumps(blocks))
        text, _ = rv.rollback_alert(SERVICE, record, None, "gs://x")
        self.assertIn("failed release-verify", text)

    def test_dev_alert_test_run(self) -> None:
        """Dev alert test run."""
        record = _record(verdict={"status": "failed", "reasons": ["forced"]})
        text, blocks = rv.dev_verify_alert(SERVICE, record, "https://build", "gs://x", test_run=True)
        self.assertTrue(text.startswith(rv.TEST_RUN_LINE))
        self.assertNotIn("<@", json.dumps(blocks))


class AutoRollbackTest(unittest.TestCase):
    def test_claim_and_trigger(self) -> None:
        """Claim and trigger."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"RELEASE_AUTO_ROLLBACK": "1"}):
            store = _store(tmp)
            record = _record(verdict={"status": "failed", "phase": "deploy"}, rollback_suggestion={"summary": "x"})
            _save(store, record)
            commands = []

            def runner(command: list[str], timeout: float = 120) -> str:
                """Record the command and answer with a build id."""
                commands.append(command)
                return "rollback-build-1\n"

            with mock.patch.object(rr, "post_slack", return_value=True):
                build = rv.start_rollback(
                    SERVICE, store, record, runner=runner, builds=lambda *_: (False, []), follow=False
                )
                self.assertEqual(build, "rollback-build-1")
                self.assertIn("_EXPECT_LATEST=20261021T100000Z-aaaaaaaaa", commands[0][-2])
                self.assertIn("demo-rollback", commands[0])
                # A second watcher sees the started claim and starts nothing.
                self.assertIsNone(
                    rv.start_rollback(
                        SERVICE, store, record, runner=runner, builds=lambda *_: (False, []), follow=False
                    )
                )
            self.assertEqual(len(commands), 1)

    def test_waits_for_a_gate_that_skips(self) -> None:
        """A deploy build holds the lock at its gate, then skips: the rollback still starts."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"RELEASE_AUTO_ROLLBACK": "1"}):
            store = _store(tmp)
            record = _record(verdict={"status": "failed", "phase": "deploy"}, rollback_suggestion={"summary": "x"})
            _save(store, record)
            rr.acquire_lock(store, "deploy2", "deploy", lambda _: True, wait_seconds=60)

            def sleep(_):
                """Move the clock instead of sleeping."""
                rr.release_lock(store, "deploy2")  # the gate said "frozen" and freed the lock

            with (
                mock.patch.object(rr, "post_slack", return_value=True),
                mock.patch.object(rr, "build_alive", return_value=lambda _: True),
            ):
                build = rv.start_rollback(
                    SERVICE,
                    store,
                    record,
                    runner=lambda _, timeout=120: "rb1\n",
                    builds=lambda *_: (False, []),
                    follow=False,
                    sleep=sleep,
                )
            self.assertEqual(build, "rb1")

    def test_claim_blocks(self) -> None:
        """Claim blocks."""
        now = datetime(2026, 10, 21, 12, tzinfo=rr.UTC)
        self.assertFalse(rv.claim_blocks({}, "me", now))
        self.assertTrue(rv.claim_blocks({"rolled_back_by": "x"}, "me", now))
        starting = {"auto_rollback": {"status": "starting", "claim": "other", "at": "2026-10-21T11:59:00Z"}}
        self.assertTrue(rv.claim_blocks(starting, "me", now))
        starting["auto_rollback"]["at"] = "2026-10-21T11:00:00Z"
        self.assertFalse(rv.claim_blocks(starting, "me", now))

    def test_off_for_test_runs(self) -> None:
        """Off for test runs."""
        with mock.patch.dict(os.environ, {"RELEASE_AUTO_ROLLBACK": ""}):
            self.assertIsNone(rv.start_rollback(SERVICE, None, _record()))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- new errors


class LogTest(unittest.TestCase):
    def test_pattern_and_site(self) -> None:
        """Pattern and site."""
        pattern = rs.log_pattern(
            "Failed to load profile 8f1e2d3c-1111-2222-3333-444455556666 after 3 tries", "ERROR", "app"
        )
        self.assertEqual(pattern, "ERROR:app:Failed to load profile <id> after # tries")
        self.assertEqual(rs.site_fragment(pattern), "ERROR:app:Failed to load profile")
        self.assertIsNone(rs.log_pattern("    at Object.<anonymous> (src/a.ts:1:1)", "ERROR", "app"))
        self.assertIsNone(rs.log_pattern("hello", "INFO", "app"))
        self.assertEqual(rs.log_pattern("ERROR:root:boom happened", "DEFAULT", "app"), "ERROR:root:boom happened")
        self.assertFalse(rs.is_error("WARNING:app:cache warmed slowly"))
        self.assertTrue(rs.is_error("WARNING:app:request failed"))

    def test_entry_text(self) -> None:
        """Entry text."""
        self.assertEqual(rs.entry_text({"jsonPayload": {"msg": "x failed"}}), "x failed")
        self.assertEqual(rs.entry_text({"jsonPayload": {"error": {"message": "y"}}}), "y")

    def test_structured_console_error_keeps_its_source(self) -> None:
        """A dynamic database error retains the literal prefix used to find its handler."""
        text = rs.entry_text(
            {
                "jsonPayload": {
                    "message": "connection refused",
                    "metadata": {"consoleArgs": ["[users] GET Error:", {"message": "connection refused"}]},
                    "error": {
                        "message": "connection refused",
                        "stack": "Error: connection refused\n    at GET (src/users.ts:3:1)",
                    },
                    "request": {"method": "GET", "path": "/api/users"},
                }
            }
        )
        self.assertEqual(text.splitlines()[0], "[users] GET Error:")
        self.assertIn("connection refused", text)
        self.assertIn("at GET (src/users.ts:3:1)", text)
        self.assertIn("GET /api/users", text)
        site = rs.site_fragment(rs.log_pattern(text, "ERROR", "app"))
        fragment = site.split(":", 2)[2]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src/users.ts").write_text(
                'export async function GET() {\n  console.error("[users] GET Error:", error);\n}\n'
            )
            problem = rs.Problem(kind="log", key="log:x", text="error", literals=[fragment])
            self.assertEqual(
                [function.name for function in rs.origins(problem, rs.SourceTree(root, SERVICE.code))], ["GET"]
            )
        self.assertIn(f'jsonPayload.metadata.consoleArgs:"{fragment}"', rs.message_match(fragment))

    def test_short_console_prefix_does_not_hide_the_message(self) -> None:
        """A generic Error: prefix cannot suppress a searchable structured message."""
        text = rs.entry_text(
            {
                "jsonPayload": {
                    "message": "database connection failed",
                    "metadata": {"consoleArgs": ["Error:", {"message": "database connection failed"}]},
                }
            }
        )
        self.assertEqual(text.splitlines()[0], "database connection failed")
        sites = rs.count_sites(rs.count_patterns([("2026-10-21", "pod", "app", "ERROR", text)]))
        self.assertTrue(any("database connection failed" in site for site in sites))

    def test_unrelated_metadata_does_not_replace_the_message(self) -> None:
        """Malformed or non-console metadata keeps the existing message reader."""
        for metadata in (None, "other", {"consoleArgs": {}}, {"consoleArgs": [None, {}]}):
            self.assertEqual(rs.entry_text({"jsonPayload": {"message": "original", "metadata": metadata}}), "original")
        self.assertEqual(
            rs.entry_text({"jsonPayload": {"error": {"stack": "Error: x"}, "request": {"path": "/x"}}}), ""
        )

    def test_count_sites(self) -> None:
        """Count sites."""
        lines = [
            ("2026-10-21T10:30:00Z", f"pod-{index % 3}", "app", "ERROR", f"Failed to load profile {index}")
            for index in range(12)
        ]
        sites = rs.count_sites(rs.count_patterns(lines))
        item = sites["ERROR:app:Failed to load profile"]
        self.assertEqual((item.count, item.spread), (12, 3))


class FailureSeriesTest(unittest.TestCase):
    def test_new_series_and_rate(self) -> None:
        """New series and rate."""
        base_fail = {"GET /a|timeout": 1}
        cand_fail = {"GET /b|http_500": 20, "GET /a|timeout": 40, "GET /c|cancelled": 50}
        base_all = {"GET /a": 1000}
        cand_all = {"GET /a": 100}
        problems = rs.judge_failures(base_fail, cand_fail, base_all, cand_all, fields=["route", "type"])
        keys = sorted(problem.key for problem in problems)
        self.assertEqual(keys, ["failure_rate:GET /a", "failure_series:GET /b|http_500"])


TS_SOURCE = """\
import { fetchProfile } from "./client";

export async function loadProfile(id: string) {
  const profile = await fetchProfile(id);
  if (!profile) {
    console.error(`Failed to load profile ${id}`);
  }
  return profile;
}

export const unrelated = (x: number) => {
  return x + 1;
};
"""
TS_CLIENT = """\
export async function fetchProfile(id: string) {
  return fetch(`/p/${id}`).then((r) => r.json());
}
"""
GO_SOURCE = """\
package server

func (s *Server) handleSearch(w http.ResponseWriter, r *http.Request) {
	if err := s.search(r); err != nil {
		log.Error("search request failed", "err", err)
	}
}

func helper() int {
	return 1
}
"""


class SourceTest(unittest.TestCase):
    def test_functions(self) -> None:
        """Functions."""
        names = [f.name for f in rs.functions_of("src/profile.ts", TS_SOURCE)]
        self.assertEqual(names, ["loadProfile", "unrelated"])
        go = rs.functions_of("src/server/search.go", GO_SOURCE)
        self.assertEqual([(f.name, f.start, f.end) for f in go], [("handleSearch", 3, 7), ("helper", 9, 11)])

    def test_trace_clear_suspect(self) -> None:
        """Trace clear suspect."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "profile.ts").write_text(TS_SOURCE)
            (root / "src" / "client.ts").write_text(TS_CLIENT)
            tree = rs.SourceTree(root, SERVICE.code)
            problem = rs.Problem(kind="log", key="log:x", text="new log line", literals=["Failed to load profile"])
            found = rs.origins(problem, tree)
            self.assertEqual([(f.path, f.name) for f in found], [("src/profile.ts", "loadProfile")])
            callee_change = rs.parse_change(
                "c1",
                [
                    {
                        "filename": "src/client.ts",
                        "patch": "@@ -1,3 +1,3 @@\n export async function fetchProfile(id: string) {\n-  return fetch(`/p/${id}`).then((r) => r.json());\n+  return fetch(`/q/${id}`).then((r) => r.json());\n }",
                    }
                ],
                {"src/client.ts": TS_CLIENT},
            )
            self.assertIn(("src/client.ts", "fetchProfile"), callee_change.functions)
            score, reasons, linked, adds = rs.score_commit(problem, {"title": "x"}, callee_change, found, tree)
            self.assertEqual(score, rs.CALLEE)
            self.assertTrue(linked)
            other = rs.parse_change("c2", [{"filename": "docs/a.md", "patch": "+x"}], {})
            scope = rs.Scope(
                releases=[_record()],
                commits=[
                    {"sha": "c1", "title": "Change the client (#5)", "pr": 5},
                    {"sha": "c2", "title": "Docs (#6)", "pr": 6},
                ],
                baseline=[BASELINE],
                window=WINDOW,
                release_start=BASELINE[1],
            )
            with mock.patch.object(rs, "commit_changes", return_value={"c1": callee_change, "c2": other}):
                rs.trace_suspects([problem], scope, token="t", repo="r", tree=tree, code=SERVICE.code)
            self.assertTrue(problem.clear)
            self.assertEqual(problem.suspects[0]["pr"], 5)

    def test_adds_line_is_not_clear(self) -> None:
        """Adds line is not clear."""
        change = rs.parse_change(
            "c1",
            [
                {
                    "filename": "src/profile.ts",
                    "patch": "@@ -5,0 +6,1 @@\n+    console.error(`Failed to load profile ${id}`);",
                }
            ],
            {"src/profile.ts": TS_SOURCE},
        )
        problem = rs.Problem(kind="log", key="log:x", text="t", literals=["Failed to load profile"])
        scope = rs.Scope([_record()], [{"sha": "c1", "title": "Log it", "pr": 7}], [BASELINE], WINDOW, BASELINE[1])
        with mock.patch.object(rs, "commit_changes", return_value={"c1": change}):
            rs.trace_suspects([problem], scope, token="t", repo="r", tree=None, code=SERVICE.code)
        self.assertFalse(problem.clear)
        self.assertIn("adds this log line", problem.suspects[0]["reason"])


class ScopeTest(unittest.TestCase):
    def test_chain_and_holes(self) -> None:
        """Chain and holes."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            older = _record(
                release_id="20261021T080000Z-ddddddddd",
                commit="d" * 40,
                times={"deploy_started": "2026-10-21T08:00:00Z", "deploy_finished": "2026-10-21T08:05:00Z"},
                previous={},
                commits=[{"sha": "d" * 40, "title": "old (#3)", "pr": 3}],
            )
            newer = _record(previous={"release_id": older["release_id"], "commit": "d" * 40})
            _save(store, older, latest=False)
            _save(store, newer)
            scope = rs.release_scope(store, newer, WINDOW[1])
            self.assertEqual([r["release_id"] for r in scope.releases], [newer["release_id"], older["release_id"]])
            self.assertEqual(len(scope.commits), 2)
            self.assertEqual(scope.window[0], datetime(2026, 10, 21, 8, 5, tzinfo=rr.UTC))


class KitTest(unittest.TestCase):
    def test_rendered(self) -> None:
        """Rendered."""
        files = kit.rendered(SERVICE)
        rollback = files["release/services/demo.rollback.yaml"]
        self.assertIn('tags: ["demo-rollback"]', rollback)
        self.assertIn("--service demo", rollback)
        self.assertIn("get-credentials prod --region us-central1", rollback)
        window = files["release/services/demo.release-verify.yaml"]
        self.assertIn('tags: ["demo-release-verify"]', window)
        self.assertIn('_CHECKPOINTS: "15 60"', window)
        self.assertNotIn("{", window.replace("${", "").replace("$${", "").replace("{{", ""))

    def test_kit_files_exist(self) -> None:
        """Kit files exist."""
        for name in kit.KIT_FILES:
            self.assertTrue((HERE / name).exists(), name)


# --------------------------------------------------------------------------- review fixes


class ReviewFixTest(unittest.TestCase):
    def test_overlap_ignored_with_recorded_digest(self) -> None:
        """Overlap ignored with recorded digest."""
        import base64

        output = base64.b64encode(json.dumps({"image_digest": NEW}).encode()).decode()
        newest = guard.Build.from_api(
            {
                "id": "new",
                "status": "SUCCESS",
                "createTime": "2026-10-21T10:00:00Z",
                "startTime": "2026-10-21T10:00:01Z",
                "finishTime": "2026-10-21T10:09:00Z",
                "results": {"buildStepOutputs": [output]},
            }
        )
        older = _build("old", "2026-10-21T09:59:00Z", start="2026-10-21T09:59:01Z", finish="2026-10-21T10:05:00Z")
        self.assertEqual(guard.decide_dev([newest, older])[0], "ok")

    def test_changing_cluster(self) -> None:
        """Changing cluster."""

        def build(status: str | None) -> guard.Build:
            """Answer as the build API does."""
            steps = [{"id": "build-image", "status": "SUCCESS"}]
            if status is not None:
                steps.append({"id": "release-record-begin", "status": status})
            return guard.Build.from_api({"id": "x", "createTime": "2026-10-21T10:00:00Z", "steps": steps})

        self.assertFalse(build("QUEUED").changing_cluster)
        self.assertTrue(build("WORKING").changing_cluster)
        self.assertTrue(build(None).changing_cluster)  # no marker step: it may change prod

    def test_smoke_validation(self) -> None:
        """Smoke validation."""
        for bad in (
            {"type": "http_pod", "deployment": "prod-demo", "path": "/x"},
            {"type": "http_pod", "deployment": "other", "port": 80, "path": "/x"},
            {"type": "http_public", "url": "http://plain"},
            {"type": "nope"},
            {"type": "pods_ready", "deployment": "prod-demo", "min_share": 2},
        ):
            data = service_data()
            data["environments"]["prod"]["smoke"] = [bad]
            with self.assertRaises(rc.ConfigError, msg=str(bad)):
                rc.parse_service(data)

    def test_backfill_skips_current_revision(self) -> None:
        """Backfill skips current revision."""
        cluster = FakeCluster(digest=NEW)
        current = cluster.replica_sets[0]
        older = json.loads(json.dumps(current))
        older["metadata"]["name"] = "prod-demo-rs-old"
        older["metadata"]["annotations"][rr.REVISION_ANNOTATION] = "3"
        older["spec"]["template"]["spec"]["containers"][0]["image"] = f"{REPO}@{OLD}"
        current["spec"]["template"]["spec"]["containers"][0]["image"] = f"{REPO}@{'9' * 64}".replace("@9", "@sha256:9")
        cluster.replica_sets.append(older)
        clients = rr.Clients(env=PROD, runner=cluster)
        self.assertEqual(rr.digest_before(clients, PROD.deployments[0], NEW), OLD)

    def test_go_declarations_between_functions(self) -> None:
        """Go declarations between functions."""
        source = "package x\n\nfunc a() {\n\tif ok {\n\t\tb()\n\t}\n}\n\nvar limit = 3\n\nfunc c() { return }\n"
        functions = rs.functions_of("internal/x.go", source)
        self.assertEqual([(f.name, f.start, f.end) for f in functions], [("a", 3, 7), ("c", 11, 11)])
        change = rs.parse_change(
            "s",
            [{"filename": "internal/x.go", "patch": "@@ -9,1 +9,1 @@\n-var limit = 2\n+var limit = 3"}],
            {"internal/x.go": source},
        )
        self.assertEqual(change.functions, set())

    def test_ts_ranges(self) -> None:
        """Ts ranges."""
        source = (
            'export function a(x: string) {\n  const s = "{";\n  return s;\n}\n'
            "export const b = (y: number) => y + 1;\n"
            "class K {\n  method(z: string): string {\n    return z;\n  }\n}\n"
        )
        ranges = {f.name: (f.start, f.end) for f in rs.functions_of("src/a.ts", source)}
        self.assertEqual(ranges, {"a": (1, 4), "b": (5, 5), "method": (7, 9)})

    def test_braces_in_signatures(self) -> None:
        """Braces in parameters and return types are not the body (orbit-search-web, embeddings-api, mobile)."""
        source = (
            "export async function fetchProfile(id: string, options = {}) {\n"  # 1
            "  log.error('x');\n"
            "  return call(id);\n"
            "}\n"  # 4
            "async function handle(msg: Msg): Promise<{ shouldAck: boolean }> {\n"  # 5
            "  if (!msg) {\n"
            "    return { shouldAck: true };\n"
            "  }\n"
            "  return { shouldAck: false };\n"
            "}\n"  # 10
            "export async function fetchJson<T>(\n"  # 11
            "  url: string,\n"
            "  init: { headers?: Record<string, string> } = {},\n"
            "): Promise<{ ok: boolean; body: T }> {\n"
            "  return request(url, init);\n"
            "}\n"  # 16
            "function oneLine() { return 1; }\n"  # 17
            "const build = (rows: Row[]) =>\n"  # 18
            "  rows.map((row) => {\n"
            "    return row.id;\n"
            "  });\n"  # 21
        )
        ranges = {f.name: (f.start, f.end) for f in rs.functions_of("src/a.ts", source)}
        self.assertEqual(ranges["fetchProfile"], (1, 4))
        self.assertEqual(ranges["handle"], (5, 10))
        self.assertEqual(ranges["fetchJson"], (11, 16))
        self.assertEqual(ranges["oneLine"], (17, 17))
        self.assertEqual(ranges["build"], (18, 21))
        go = (
            "func objectList(names []string, allowed map[string]struct{}, limit int) ([]Object, error) {\n"  # 1
            "\tfor _, n := range names {\n"
            "\t\tuse(n)\n"
            "\t}\n"
            "\treturn nil, nil\n"
            "}\n"  # 6
            "func small() int { return 1 }\n"  # 7
            "func (s *Server) object(\n"  # 8
            "\tctx context.Context,\n"
            "\tid string,\n"
            ") (map[string]struct{}, error) {\n"
            "\treturn load(ctx, id)\n"
            "}\n"  # 13
        )
        ranges = {f.name: (f.start, f.end) for f in rs.functions_of("internal/x.go", go)}
        self.assertEqual(ranges, {"objectList": (1, 6), "small": (7, 7), "object": (8, 13)})

    def test_python_ranges(self) -> None:
        """Python ranges."""
        source = "def a():\n    x = 1\n\n    return x\n\nLIMIT = 3\n"
        self.assertEqual([(f.start, f.end) for f in rs.functions_of("src/a.py", source)], [(1, 4)])

    def test_message_match_covers_entry_fields(self) -> None:
        """Message match covers entry fields."""
        match = rs.message_match("boom")
        for field in ("textPayload", "jsonPayload.msg", "jsonPayload.error", "jsonPayload.err.message"):
            self.assertIn(f'{field}:"boom"', match)

    def test_rollback_target_without_recorded_predecessor(self) -> None:
        """Rollback target without recorded predecessor."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            first = _record(
                release_id="20261021T080000Z-ddddddddd",
                commit="d" * 40,
                previous={
                    "release_id": None,
                    "commit": PREVIOUS,
                    "images": {"app": f"{REPO}@{OLD}"},
                    "workloads": {
                        "deployment/prod-demo": {"digest": OLD},
                        "deployment/prod-demo-worker": {"digest": OLD},
                    },
                },
            )
            second = _record(previous={"release_id": first["release_id"], "commit": "d" * 40})
            _save(store, first, latest=False)
            _save(store, second)
            problems = [{"clear": True, "suspects": [{"release_id": first["release_id"]}]}]
            scope = {"releases": [second["release_id"], first["release_id"]]}
            target = rs.rollback_target(second, store, problems, scope)
            self.assertEqual(target["rollback_to"], f"previous-of:{first['release_id']}")
            latest, resolved = rb.resolve_target(store, target["rollback_to"])
            self.assertEqual(latest["release_id"], second["release_id"])
            self.assertEqual(resolved["workloads"]["deployment/prod-demo"]["digest"], OLD)

    def test_no_spread_is_a_warning(self) -> None:
        """No spread is a warning."""
        finding = rv.Finding("m", "s", 1.0, 3.0, 1000, 1000, "limit", "exceeded")
        rv.apply_spread([finding], {"m": {"s": [1.0, 1.1]}})
        self.assertEqual(finding.status, "no_spread")

    def test_prod_state(self) -> None:
        """Prod state."""

        def build(build_id: str, marker: str) -> guard.Build:
            """Answer as the build API does."""
            return guard.Build.from_api(
                {
                    "id": build_id,
                    "status": "WORKING",
                    "createTime": "2026-10-21T10:00:00Z",
                    "steps": [{"id": "release-record-begin", "status": marker}],
                }
            )

        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _save(store, _record())
            api = mock.Mock()
            api.ongoing_rollbacks.return_value = []
            api.list_builds.return_value = [build("queued", "QUEUED")]
            with mock.patch.object(rv, "GoogleApi", return_value=api):
                self.assertEqual(rv.prod_state(SERVICE, store, _record()), (None, None))
                api.list_builds.return_value = [build("busy", "WORKING")]
                self.assertIn("changing prod", rv.prod_state(SERVICE, store, _record())[1])
                api.ongoing_rollbacks.return_value = [build("rb", "WORKING")]
                self.assertIn("rollback build", rv.prod_state(SERVICE, store, _record())[0])

    def test_assert_alone_ignores_waiting_builds(self) -> None:
        """Assert alone ignores waiting builds."""
        waiting = guard.Build.from_api(
            {
                "id": "w",
                "status": "WORKING",
                "createTime": "2026-10-21T10:00:00Z",
                "steps": [{"id": "release-record-begin", "status": "QUEUED"}],
            }
        )
        api = mock.Mock()
        api.list_builds.return_value = [waiting]
        api.ongoing_rollbacks.return_value = []
        with mock.patch.object(rb, "GoogleApi", return_value=api):
            rb.assert_alone(SERVICE, "own")

    def test_failed_rollback_frees_the_claim(self) -> None:
        """Failed rollback frees the claim."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(
                auto_rollback={"status": "started", "claim": "c1", "build_id": "rb1", "at": "2026-10-21T10:00:00Z"}
            )
            _save(store, record)
            with mock.patch.object(rr, "post_slack", return_value=True) as slack:
                status = rv.follow_rollback(
                    SERVICE,
                    store,
                    record["release_id"],
                    "rb1",
                    "c1",
                    target="previous-of:r1",
                    sleep=lambda _: None,
                    get_build=lambda _: mock.Mock(status="FAILURE"),
                )
            self.assertEqual(status, "FAILURE")
            self.assertIn("--to previous-of:r1", slack.call_args[0][0])
            saved = rr.read_record(store, record["release_id"])
            self.assertEqual(saved["auto_rollback"]["status"], "failed")
            self.assertFalse(rv.claim_blocks(saved, "other", datetime(2026, 10, 21, 11, tzinfo=rr.UTC)))
            self.assertIn("Roll back by hand", slack.call_args[0][0])

    def test_red_cleanup_build_does_not_recommend_a_second_rollback(self) -> None:
        """A restored, held rollback can end red for cleanup without reversing to the failed release."""
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(rr, "post_slack", return_value=True) as slack,
        ):
            store = _store(tmp)
            record = _record(auto_rollback={"status": "started", "claim": "c1", "build_id": "rb1"})
            _save(store, record)
            rollback = _record(
                release_id="20261021T110000Z-bbbbbbbbb",
                kind="rollback",
                status="deploying",
                build={"id": "rb1"},
                rollback={"rolled_back_release": record["release_id"]},
            )
            _save(store, rollback, latest=False)
            code = rb.finish_rollback(
                SERVICE,
                store,
                argparse.Namespace(requested_by="test", reason="restore prod"),
                rollback,
                record,
                {"commit": PREVIOUS},
                {},
                [],
                [],
                cleanup_failures=["failed candidate tag remains"],
            )
            self.assertEqual(code, 1)
            written = rr.read_record(store, rollback["release_id"])
            self.assertEqual(written["status"], "deployed")
            self.assertEqual(written["promote_hold"]["status"], "held")
            self.assertEqual(
                written["checks"]["rollback"]["cleanup_failures"],
                ["failed candidate tag remains"],
            )
            status = rv.follow_rollback(
                SERVICE,
                store,
                record["release_id"],
                "rb1",
                "c1",
                sleep=lambda _: None,
                get_build=lambda _: mock.Mock(status="FAILURE"),
            )
            self.assertEqual(status, "FAILURE")
            self.assertEqual(rr.read_record(store, record["release_id"])["auto_rollback"]["status"], "started")
            self.assertIn("do not roll back again", slack.call_args.args[0])
            self.assertNotIn("Roll back by hand", slack.call_args.args[0])

    def test_single_pod_spread(self) -> None:
        """Single pod spread."""
        data = service_data()
        data["verify"]["signals"] = {"log_containers": ["app"], "log_min_spread": 1}
        self.assertEqual(rc.parse_service(data).signals.log_min_spread, 1)
        self.assertEqual(SERVICE.signals.log_min_spread, 2)

    def test_alert_command_names_the_target(self) -> None:
        """Alert command names the target."""
        record = _record(
            verdict={"status": "failed", "phase": "window_15m", "reasons": ["x"]},
            rollback_suggestion={
                "target": {
                    "commit": PREVIOUS,
                    "release_id": None,
                    "rollback_to": "previous-of:20261021T080000Z-ddddddddd",
                },
                "suspected_prs": [],
            },
        )
        self.assertIn("--to previous-of:20261021T080000Z-ddddddddd", rv.suggested_command(SERVICE, record))
        plain = _record(rollback_suggestion={"target": {"release_id": _record()["previous"]["release_id"]}})
        self.assertNotIn("--to", rv.suggested_command(SERVICE, plain))
        suggestion = rv.rollback_suggestion(SERVICE, record, [], "window_15m", None, {"rollback_to": "previous-of:x"})
        self.assertIn("--to previous-of:x", suggestion["how"][0])

    def test_printed_commands_parse(self) -> None:
        """With several services, the copied commands put --service and --to where each parser takes them."""
        record = _record(
            rollback_suggestion={"target": {"commit": PREVIOUS, "release_id": None, "rollback_to": "previous-of:x"}}
        )
        seen: list = []
        parse = argparse.ArgumentParser.parse_args

        def capture(self, argv=None, namespace=None):
            """Keep the parsed arguments, then stop the command."""
            seen.append(parse(self, argv, namespace))
            raise KeyError("stop")

        with mock.patch.object(rc, "single_service", return_value=False):
            commands = [
                rv.suggested_command(SERVICE, record) + " --reason why",
                rv.suggested_command(SERVICE, record, "plan"),
                rv.rollback_suggestion(SERVICE, record, [], "window_15m", None, {"rollback_to": "previous-of:x"})[
                    "how"
                ][1].split("#")[0],
                SERVICE.kit_command("release_gate.py", "resume") + " --reason why",
            ]
        for command in commands:
            words = command.replace("'<why>'", "why").split()
            self.assertEqual(words[:2], ["python3", words[1]])
            script = words[1].rsplit("/", 1)[-1]
            module = {"release_rollback.py": rb, "release_gate.py": rg}[script]
            with mock.patch.object(argparse.ArgumentParser, "parse_args", capture), self.assertRaises(KeyError):
                module.main(words[2:])
            self.assertEqual(seen[-1].service, "demo", command)
            if script == "release_rollback.py":
                self.assertEqual(seen[-1].to, "previous-of:x", command)

    def test_default_retry_after_a_rollback_refuses(self) -> None:
        """Default retry after a rollback refuses."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _save(store, _record(kind="rollback", rollback={"rolled_back_release": "bad"}))
            with self.assertRaises(rr.RecordError):
                rb.resolve_target(store, "previous")

    def test_gate_waits_for_rollbacks(self) -> None:
        """Gate waits for rollbacks."""
        running = [guard.Build.from_api({"id": "rb", "status": "WORKING", "createTime": "2026-10-21T10:00:00Z"})]
        api = mock.Mock()
        api.ongoing_rollbacks.side_effect = [running, running, []]
        sleeps = []
        rg.wait_for_rollbacks(SERVICE, sleep=sleeps.append, api=api)
        self.assertEqual(len(sleeps), 2)


# --------------------------------------------------------------------------- light kinds (Cloud Run, Composer)

import release_light as rl  # noqa: E402


def light_data(kind: str) -> dict:
    """A light service config of the given kind."""
    data = {
        "service": f"demo-{kind.replace('_', '-')}",
        "kind": kind,
        "tier": "check_only",
        "github_repo": "orbit-search/demo",
        "connected_repository": "orbit-search-demo",
        "verify": {"log_filter": 'resource.type="cloud_run_revision" AND severity>=WARNING'},
        "code": {"source_prefixes": ["dags/"], "extensions": [".py"]},
        "smoke": [{"type": "http_public", "url": "https://demo.test/health"}],
    }
    if kind == "cloud_run":
        data["cloud_run_services"] = ["demo-a", "demo-b"]
        data["deploy_triggers"] = ["demo-run-deploy"]
    else:
        data["composer"] = {"environment": "env-v3", "sync_trigger": "demo-sync"}
    return data


ROUTES = """\
export function registerWatcherRoutes(api: Router) {
\tapi.patch("/v2/organizations/:organizationId/watchers/:watcherId", auth, async (ctx: MyContext) => {
\t\tconst body = parse(ctx.request.body);
\t\tctx.body = await updateWatcher(ctx.params.watcherId, body);
\t});

\tapi.get("/v3/watchers/:watcherId", auth, async (ctx: MyContext) => {
\t\tctx.body = await readWatcher(ctx.params.watcherId);
\t});
}
"""
MULTILINE = """\
export class SearchService {
\tprivate static async startWithFunding(
\t\tparams: StartParams,
\t\tsource: FundingSource,
\t): Promise<{ snapshot: Snapshot }> {
\t\tconst funded = await fund(params, source);
\t\treturn { snapshot: funded };
\t}
}
export const buildRows = async (
\trows: Row[],
): Promise<Row[]> => {
\treturn rows;
};
"""


class RouteTraceTest(unittest.TestCase):
    """Greptile on orbit-api #1538: route handlers, multi-line signatures, the newest suspects."""

    def test_route_handlers_and_multiline_signatures(self) -> None:
        """Route handlers and multiline signatures."""
        ranges = {f.name: (f.start, f.end) for f in rs.functions_of("src/endpoints/watchers.ts", ROUTES)}
        self.assertEqual(ranges["patch_/v2/organizations/:organizationid/watchers/:watcherid"], (2, 5))
        self.assertEqual(ranges["get_/v3/watchers/:watcherid"], (7, 9))
        ranges = {f.name: (f.start, f.end) for f in rs.functions_of("src/services/search.ts", MULTILINE)}
        self.assertEqual((ranges["startWithFunding"], ranges["buildRows"]), ((2, 8), (10, 14)))

    def test_a_failing_route_traces_to_its_handler(self) -> None:
        """A failing route traces to its handler."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "watchers.ts").write_text(ROUTES)
            tree = rs.SourceTree(root, SERVICE.code)
            route = "get_/v3/watchers/:watcherid"
            problem = rs.judge_failures({}, {route: 25.0}, {route: 40000.0}, {route: 200.0}, fields=["resource_name"])[
                0
            ]
            self.assertEqual([f.name for f in rs.origins(problem, tree)], [route])
            change = rs.parse_change(
                "c1",
                [
                    {
                        "filename": "src/watchers.ts",
                        "patch": "@@ -8,1 +8,1 @@\n-\t\tctx.body = await readWatcher(ctx.params.watcherId);"
                        "\n+\t\tctx.body = await readWatcher(ctx.params.id);",
                    }
                ],
                {"src/watchers.ts": ROUTES},
            )
            docs = rs.parse_change("c2", [{"filename": "docs/a.md", "patch": "+x"}], {})
            scope = rs.Scope(
                [_record()],
                [{"sha": "c1", "title": "Watcher reads (#5)", "pr": 5}, {"sha": "c2", "title": "Docs (#6)", "pr": 6}],
                [BASELINE],
                WINDOW,
                BASELINE[1],
            )
            with mock.patch.object(rs, "commit_changes", return_value={"c1": change, "c2": docs}):
                rs.trace_suspects([problem], scope, token="t", repo="r", tree=tree, code=SERVICE.code)
            self.assertTrue(problem.clear)
            self.assertEqual(problem.suspects[0]["pr"], 5)

    def test_the_newest_commits_are_searched(self) -> None:
        """The newest commits are searched."""
        commits = [{"sha": f"s{index:03d}", "title": f"Change {index} (#{index})", "pr": index} for index in range(45)]
        scope = rs.Scope([_record()], commits, [BASELINE], WINDOW, BASELINE[1])
        problem = rs.Problem(kind="log", key="log:x", text="new log line", literals=["Failed to load profile"])
        seen: list = []
        with mock.patch.object(rs, "commit_changes", lambda c, *a: seen.extend(c) or {}):
            rs.trace_suspects([problem], scope, token=None, repo="r", tree=None, code=SERVICE.code)
        self.assertEqual((seen[0]["sha"], seen[-1]["sha"], len(seen)), ("s005", "s044", 40))
        self.assertIn("5 older commit(s)", problem.text)

    def test_chain_commits_rank_and_final_inconclusive_is_unchecked(self) -> None:
        """Chain commits rank and final inconclusive is unchecked."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            older = _record(
                release_id="20261021T080000Z-ddddddddd",
                commit="d" * 40,
                previous={"commit": "e" * 40},
                commits=[{"sha": "d" * 40, "title": "Slow the parser (#3)", "pr": 3, "author": "a"}],
            )
            newer = _record(previous={"release_id": older["release_id"], "commit": "d" * 40})
            _save(store, older, latest=False)
            _save(store, newer)
            result = VerdictTest._result(
                None,
                result="failed",
                regressions=[{"kind": "metric", "text": "p95 route x: 0.1 -> 2"}],
                chain=[newer["release_id"], older["release_id"]],
            )
            with mock.patch.object(rr, "github_json", lambda path, token: {"files": [{"filename": "src/a.ts"}]}):
                saved = rv.apply_result(SERVICE, store, newer, result, final=False, token="t")
            prs = {item.get("pr") for item in saved["rollback_suggestion"]["suspected_prs"]}
            self.assertEqual(prs, {3, 12})
            other = _record(release_id="20261021T120000Z-fffffffff")
            _save(store, other)
            quiet = rv.apply_result(SERVICE, store, other, VerdictTest._result(None, judged=0), final=True, token=None)
            self.assertEqual((quiet["verdict"]["status"], quiet["verdict"]["unchecked"]), ("inconclusive", True))


NAMED_ROUTES = """\
export function registerRoutes(api: Router) {
\tapi.get("/v3/result/:id", auth, answerClayResult);
\tapi.get("/v3/contacts", auth, async (ctx: MyContext) => {
\t\tctx.body = await DeveloperClayPersonService.getContacts(ctx);
\t});
}
"""
SHAPES = """\
export class Builder {
\tprivate build(
\t\tinput: Input,
\t): {
\t\tid: string;
\t\tvalue: number;
\t} {
\t\treturn compute(input);
\t}
}
export const fetchRows = async (
\tid: string,
): Promise<{ rows: Row[] }> => {
\treturn load(id);
};
export async function fetchSenditProfile(id: string, options = {}) {
\tconst data = (
\t\tawait request(id)
\t).body;
\treturn data;
}
"""


class RoundSixTest(unittest.TestCase):
    """Greptile round 6: route ends, return types, expressions, the hold, unchecked, deploy_failed, rollback_env."""

    def test_a_named_handler_route_ends_on_its_line(self) -> None:
        """A route with a named handler ends on its line and calls the handler by name."""
        functions = {f.name: f for f in rs.functions_of("src/routes.ts", NAMED_ROUTES)}
        result = functions["get_/v3/result/:id"]
        self.assertEqual((result.start, result.end), (2, 2))
        self.assertIn("answerClayResult", result.calls)
        self.assertNotIn("getContacts", result.calls)
        self.assertEqual((functions["get_/v3/contacts"].start, functions["get_/v3/contacts"].end), (3, 5))

    def test_multiline_return_type_arrows_and_regex_in_routes(self) -> None:
        """A return type that spans lines before "=>", and a regex literal inside a route handler."""
        source = (
            "export const loadRows = (\n"  # 1
            "\tid: string,\n"
            "): Promise<\n"
            "\tRow[]\n"
            "> => {\n"
            "\treturn rows(id);\n"
            "};\n"  # 7
            "export function register(api: Router) {\n"  # 8
            '\tapi.get("/v3/match", auth, async (ctx: MyContext) => {\n'  # 9
            "\t\tconst pattern = /\\(/;\n"
            "\t\tctx.body = match(pattern, ctx.query.q);\n"
            "\t});\n"  # 12
            "}\n"
        )
        functions = {f.name: f for f in rs.functions_of("src/rows.ts", source)}
        self.assertEqual((functions["loadRows"].start, functions["loadRows"].end), (1, 7))
        more = (
            'api.get("/v3/a", auth, async (ctx: MyContext) => {\n'  # 1
            "\tconst ok = ids.filter((id) => /\\(/.test(id));\n"
            "\tif (!ok.length) {\n"
            "\t\treturn /\\(/.test(ctx.query.q);\n"
            "\t}\n"
            "});\n"  # 6
            "const commented = (\n"  # 7
            "\tid: string,\n"
            ") /* the row */ => {\n"
            "\treturn id;\n"
            "};\n"  # 11
        )
        ranges = {f.name: (f.start, f.end) for f in rs.functions_of("src/more.ts", more)}
        self.assertEqual(ranges["get_/v3/a"], (1, 6))
        self.assertEqual(ranges["commented"], (7, 11))
        tricky = (
            'api.get("/v3/b", auth, async (ctx: MyContext) => {\n'  # 1
            "\tconst ok = /[/*]/.test(ctx.query.q);\n"
            '\tconst path = "a/*b";\n'
            "\t/* example {\n"
            "\t   still a comment } */\n"
            "\treturn ok;\n"
            "});\n"  # 7
            "function after() {\n"  # 8
            "\t/* opens {\n"
            "\t} closes */\n"
            "\treturn 1;\n"
            "}\n"  # 12
        )
        ranges = {f.name: (f.start, f.end) for f in rs.functions_of("src/tricky.ts", tricky)}
        self.assertEqual((ranges["get_/v3/b"], ranges["after"]), ((1, 7), (8, 12)))
        cases = {
            "apostrophe in a comment": "/*\n * It's ready. */ const label = 'ready';\n",
            "quote in a comment": '/* " */ const label = "ok";\n',
            "regex-like comment end": "/* Pattern: /foo*/\n",
            "glob in a template": "const pattern = `\nassets/*\n`;\n",
            "route in a comment": '/*\napi.get("/gone", handler);\n*/\n',
            "route in a template": 'const example = `\napi.get("/gone", handler);\n`;\n',
            "regex after a condition": "if (ready) /[/*]/.test(input) && onMatch();\n",
            "template expressions": "const a = `x${ {k: '}'}.k }y${g(`z${h({})}`)}`;\n",
        }
        for name, head in cases.items():
            source = head + "function after() {\n\treturn 1;\n}\n"
            first = head.count("\n") + 1
            found = [(f.name, f.start, f.end) for f in rs.functions_of("src/case.ts", source)]
            self.assertEqual(found, [("after", first, first + 2)], name)
        go = "var usage = `\nsrc/*\n`\n\nfunc after() int {\n\treturn 1\n}\n"
        self.assertEqual([(f.name, f.start, f.end) for f in rs.functions_of("x.go", go)], [("after", 5, 7)])
        windows = "var path = `C:\\`\n\nfunc after() int {\n\treturn 1\n}\n"
        self.assertEqual([(f.name, f.start, f.end) for f in rs.functions_of("x.go", windows)], [("after", 3, 5)])
        called = "function show() {\n\treturn `${loadUser()} and ${`${formatName(x)}`}`;\n}\n"
        self.assertTrue({"loadUser", "formatName"} <= rs.functions_of("src/t.ts", called)[0].calls)
        nested = "function show() {\n\tconst result = `${items.map(item => { return `x\ny`;\n\t})}`;\n\treturn finish(result);\n}\n"
        [show] = rs.functions_of("src/n.ts", nested)
        self.assertEqual((show.start, show.end), (1, 6))
        self.assertIn("finish", show.calls)
        template_route = "api.get(`/v3/t`, auth, async (ctx) => {\n\tctx.body = 1;\n});\n"
        self.assertEqual(
            [(f.name, f.start, f.end) for f in rs.functions_of("src/r.ts", template_route)], [("get_/v3/t", 1, 3)]
        )
        division = (
            'api.get("/v3/c", auth, async (ctx: MyContext) => {\n'
            "\tconst size = units.in / (scale / ratio);\n"
            "\tctx.body = size;\n"
            "});\n"
        )
        self.assertEqual([(f.start, f.end) for f in rs.functions_of("src/d.ts", division)], [(1, 4)])
        postfix = division.replace("units.in", "index++")
        empty = division.replace("units.in", "``")
        self.assertEqual([(f.start, f.end) for f in rs.functions_of("src/d.ts", empty)], [(1, 4)])
        self.assertEqual([(f.start, f.end) for f in rs.functions_of("src/d.ts", postfix)], [(1, 4)])
        regex = division.replace("units.in / (scale / ratio)", "++count && /[(]/.test(text)")
        self.assertEqual([(f.start, f.end) for f in rs.functions_of("src/d.ts", regex)], [(1, 4)])
        # One pass: a 1 MB file takes well under a second (a copy per word made it quadratic).
        big = "const value = item.size / 2; // note\n" * 28000
        started = time.monotonic()
        rs.blank_comments(big.splitlines())
        self.assertLess(time.monotonic() - started, 2.0)
        route = functions["get_/v3/match"]
        self.assertEqual((route.start, route.end), (9, 12))
        self.assertIn("match", route.calls)

    def test_object_return_types_and_expressions(self) -> None:
        """Object return types are skipped before the body; `const data = (` is no function."""
        ranges = {f.name: (f.start, f.end) for f in rs.functions_of("src/shapes.ts", SHAPES)}
        self.assertEqual(ranges["build"], (2, 9))
        typed = "const load = (\n\tid: string,\n): Promise<{ ledgerId: string; balanceAfter: number }> => {\n\treturn x;\n};\n"
        self.assertEqual([(f.name, f.start, f.end) for f in rs.functions_of("src/typed.ts", typed)], [("load", 1, 5)])
        self.assertEqual(ranges["fetchRows"], (11, 15))
        self.assertEqual(ranges["fetchSenditProfile"], (16, 21))
        self.assertNotIn("data", ranges)

    def test_the_hold_names_only_the_releases_the_rollback_removes(self) -> None:
        """A rollback to "previous" holds only the newest release's PRs."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            older = _record(
                release_id="20261021T080000Z-ddddddddd",
                commit="d" * 40,
                previous={"commit": "e" * 40},
                commits=[{"sha": "d" * 40, "title": "Old change (#3)", "pr": 3, "author": "a"}],
            )
            newer = _record(previous={"release_id": older["release_id"], "commit": "d" * 40})
            _save(store, older, latest=False)
            _save(store, newer)
            suspect = {
                "sha": COMMIT,
                "pr": 12,
                "title": "Change the parser (#12)",
                "score": 6,
                "reason": "x",
                "release_id": newer["release_id"],
            }
            result = VerdictTest._result(
                None,
                result="failed",
                regressions=[{"kind": "new_error", "name": "log:x", "text": "x", "suspects": [suspect]}],
                new_errors={
                    "problems": [{"clear": True, "suspects": [suspect]}],
                    "scope": {"releases": [newer["release_id"], older["release_id"]]},
                },
                chain=[newer["release_id"], older["release_id"]],
            )
            with mock.patch.object(rr, "github_json", lambda path, token: {"files": [{"filename": "src/a.ts"}]}):
                saved = rv.apply_result(SERVICE, store, newer, result, final=False, token="t")
            prs = {item.get("pr") for item in saved["rollback_suggestion"]["suspected_prs"]}
            self.assertEqual(prs, {12})  # the older release stays deployed: its PR is not held

    def test_unchecked_when_a_source_could_not_run_or_a_checkpoint_failed(self) -> None:
        """A passed verdict with an unread source, and a checkpoint error, are unchecked."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            _save(store, record)
            result = VerdictTest._result(
                None, new_errors={"parts": {"logs": {"status": "inconclusive"}}, "problems": []}
            )
            saved = rv.apply_result(SERVICE, store, record, result, final=True, token=None)
            self.assertEqual((saved["verdict"]["status"], saved["verdict"]["unchecked"]), ("passed", True))
            other = _record(release_id="20261021T120000Z-fffffffff")
            _save(store, other)
            rv._record_checkpoint_error(store, other, 60, "boom", final=True)
            self.assertTrue(rr.read_record(store, other["release_id"])["verdict"]["unchecked"])

    def test_a_failed_deploy_fails_the_deploy_checks(self) -> None:
        """A record with status deploy_failed fails its deploy checks."""
        record = _record(status="deploy_failed")
        clients = rr.Clients(env=PROD, runner=FakeCluster(digest=NEW))
        with mock.patch.object(rv, "smoke_checks", return_value=[]):
            result = rv.evaluate(SERVICE, record, phase="deploy", clients=clients, dd=None, now=WINDOW[1])
        self.assertEqual(result["result"], "failed")
        self.assertIn("deploy", [item["name"] for item in result["regressions"]])

    def test_rollback_env_is_patched_with_the_image(self) -> None:
        """A Deployment with rollback_env gets its image and env in one patch."""
        data = service_data()
        data["environments"]["prod"]["deployments"][0]["rollback_env"] = {"OAUTH_SESSION_ISSUANCE_ENABLED": "true"}
        service = rc.parse_service(data)

        class PatchingCluster(FakeCluster):
            def __call__(self, command: list[str], timeout: float = 120) -> str:
                """Apply a strategic patch's image to the fake Deployment and its pods."""
                if "patch" in command:
                    self.commands.append(command)
                    body = json.loads(command[command.index("-p") + 1])
                    container = body["spec"]["template"]["spec"]["containers"][0]
                    name = command[command.index("patch") + 1].split("/", 1)[1]
                    self.deployments[name]["spec"]["template"]["spec"]["containers"][0]["image"] = container["image"]
                    for pod in self.pods:
                        if pod["metadata"]["name"].startswith(f"{name}-"):
                            pod["status"]["containerStatuses"][0]["imageID"] = (
                                f"{REPO}@{rr.digest_of(container['image'])}"
                            )
                    return ""
                return super().__call__(command)

        cluster = PatchingCluster(env=service.prod, digest=NEW)
        clients = rr.Clients(env=service.prod, runner=cluster)
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _save(store, _record())
            latest, target = rb.resolve_target(store, "previous")
            plan = rb.build_plan(service, latest, target, rr.snapshot_workloads(clients), None)
            with mock.patch.object(rr, "wait_rollouts", return_value=[]):
                self.assertEqual(rb.apply_plan(plan, clients), [])
        patches = [c for c in cluster.commands if "patch" in c]
        self.assertEqual(len(patches), 1)
        body = json.loads(patches[0][patches[0].index("-p") + 1])["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(body["env"], [{"name": "OAUTH_SESSION_ISSUANCE_ENABLED", "value": "true", "valueFrom": None}])
        self.assertTrue(body["image"].endswith(OLD))
        self.assertTrue(
            any(c[c.index("-n") + 2 : c.index("-n") + 4] == ["set", "image"] for c in cluster.commands if "-n" in c)
        )


class ProxyTest(unittest.TestCase):
    """embeddings-api 20261022T053511Z: two pods of an HPA scale-up made the smoke check fail and rolled back a good release."""

    def test_starting_and_unreachable_pods_do_not_fail_the_release(self) -> None:
        """A pod still starting is not called; one the proxy cannot reach is named, not failed."""
        cluster = FakeCluster(digest=NEW)
        cluster.pods.append(_pod("prod-demo-new", "prod-demo-rs", "app", NEW, not_ready=True))
        clients = rr.Clients(env=PROD, runner=cluster)
        unreachable = json.dumps(
            {"kind": "Status", "status": "Failure", "message": "error trying to reach service: dial tcp 10.0.0.1:3000"},
            separators=(",", ":"),
        ).encode()
        calls: list[str] = []

        def pod_get(namespace, pod, port, path):
            """prod-demo-1 stays unreachable through the proxy; the other pods answer."""
            calls.append(pod)
            return (503, unreachable) if pod == "prod-demo-1" else (200, b"ok")

        check = {"type": "http_pod", "name": "health", "deployment": "prod-demo", "port": 3000, "path": "/healthz"}
        result = rv.check_http_pod(clients, check, pod_get, sleep=lambda _: None)
        self.assertEqual(result.status, "passed")
        self.assertNotIn("prod-demo-new", calls)  # not Ready yet: not called
        self.assertEqual(calls.count("prod-demo-1"), rv.PROXY_ATTEMPTS)
        self.assertIn("1 not Ready yet", result.details[0])
        # An app error still fails, and so does no pod answering at all.
        failing = rv.check_http_pod(clients, check, lambda *a: (500, b"boom"), sleep=lambda _: None)
        self.assertEqual(failing.status, "failed")
        silent = rv.check_http_pod(clients, check, lambda *a: (503, unreachable), sleep=lambda _: None)
        self.assertEqual(silent.status, "failed")
        spaced = json.dumps({"kind": "Status", "message": "error trying to reach service: dial tcp"}).encode()
        self.assertTrue(rv._proxy_unreachable(503, spaced))
        self.assertFalse(rv._proxy_unreachable(503, b"Service Unavailable"))

    def test_a_hung_pod_fails(self) -> None:
        """A Ready pod whose request keeps timing out fails the check, with the error kept."""
        clients = rr.Clients(env=PROD, runner=FakeCluster(digest=NEW))
        check = {"type": "http_pod", "name": "health", "deployment": "prod-demo", "port": 3000, "path": "/healthz"}

        def pod_get(namespace, pod, port, path):
            """prod-demo-1 hangs; the other pods answer."""
            if pod == "prod-demo-1":
                raise TimeoutError("timed out")
            return 200, b"ok"

        result = rv.check_http_pod(clients, check, pod_get, sleep=lambda _: None)
        self.assertEqual(result.status, "failed")
        self.assertIn("timed out", " ".join(result.details))


class PublicRetryTest(unittest.TestCase):
    """orbit-mobile-api 20261022T053857Z: Cloudflare answered 521 for a minute after the rollout and rolled back a good release."""

    def test_an_edge_error_is_tried_again_and_an_app_error_is_not(self) -> None:
        """521 is tried again until it answers; 500 is judged at once; 521 for 2 minutes fails."""
        answers = iter([(521, b"origin down"), (521, b"origin down"), (200, b"ok")])
        now = [0.0]
        check = {"type": "http_public", "url": "https://demo.test/health", "expect_status": [200]}
        result = rv.check_http_public(
            check,
            get=lambda request: next(answers),
            sleep=lambda s: now.__setitem__(0, now[0] + s),
            clock=lambda: now[0],
        )
        self.assertEqual(result.status, "passed")
        self.assertIn("after 3 tries", result.details[0])
        calls: list = []
        failing = rv.check_http_public(
            check, get=lambda request: calls.append(1) or (500, b"boom"), sleep=lambda s: None
        )
        self.assertEqual((failing.status, len(calls)), ("failed", 1))
        now[0] = 0.0
        down = rv.check_http_public(
            check, get=lambda request: (521, b"x"), sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0]
        )
        self.assertEqual(down.status, "failed")


class LightTest(unittest.TestCase):
    def setUp(self):
        history = mock.patch.object(rv, "builds_since_rollback", return_value=[])
        history.start()
        self.addCleanup(history.stop)

    def test_light_terminal_verdict_and_receipt_are_atomic_on_resume(self):
        service = rl.parse_light(light_data("cloud_run"))
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as tmp:
                store = _store(tmp)
                record = _record()
                _save(store, record)
                args = argparse.Namespace(release_id=record["release_id"], checkpoints="60", retry_completed=False)
                observed = {
                    "problems": [{"clear": True, "text": "bad"}] if failed else [],
                    "parts": {"logs": {"status": "checked"}},
                    "scope": {},
                }
                real_update = rr.update_record

                def outage(*args, **kwargs):
                    event = args[3]
                    details = args[4] if len(args) > 4 else {}
                    if event == "verify_final" or failed and details.get("result") == "failed":
                        raise rr.RecordError("terminal write down")
                    return real_update(*args, **kwargs)

                with (
                    mock.patch.object(rl, "wait_for_deploys", return_value=None),
                    mock.patch.object(rl, "replaced_by", return_value=None),
                    mock.patch.object(rl, "evaluate_window", return_value=observed) as evaluate,
                    mock.patch.object(rl, "on_failed") as rollback,
                    mock.patch.object(rl, "rr_token", return_value=None),
                    mock.patch.object(rs, "rollback_target", return_value=None),
                    mock.patch.object(rv, "suspected_prs", return_value=[]),
                    mock.patch.object(
                        rv, "new_error_regression", return_value={"kind": "new_error", "name": "bad", "text": "bad"}
                    ),
                    mock.patch.object(rr, "post_slack", return_value=True),
                ):
                    with mock.patch.object(rr, "update_record", side_effect=outage), self.assertRaises(rr.RecordError):
                        rl.cmd_watch(args, service, store, mock.Mock())
                    saved = rr.read_record(store, record["release_id"])
                    self.assertNotEqual(saved["followup"]["checkpoints"]["window_60m"]["state"], "completed")
                    rl.cmd_watch(args, service, store, mock.Mock())
                    self.assertEqual(evaluate.call_count, 2)
                    saved = rr.read_record(store, record["release_id"])
                    self.assertEqual(saved["verdict"]["status"], "failed" if failed else "passed")
                    self.assertEqual(saved["followup"]["checkpoints"]["window_60m"]["state"], "completed")
                    self.assertEqual(rollback.call_count, int(failed))

    def test_light_manual_final_attempt_gets_a_terminal_verdict(self):
        service = rl.parse_light(light_data("cloud_run"))
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            prior = {"at": "manual", "result": "passed", "evidence": "preserved"}
            record["checks"]["window_60m"] = prior
            rv.ro.note_checkpoint(record, prior, "window_60m")
            _save(store, record)
            args = argparse.Namespace(release_id=record["release_id"], checkpoints="60", retry_completed=False)
            clean = {"problems": [], "parts": {"logs": {"status": "checked"}}, "scope": {}}
            with (
                mock.patch.object(rl, "wait_for_deploys", return_value=None),
                mock.patch.object(rl, "replaced_by", return_value=None),
                mock.patch.object(rl, "evaluate_window", return_value=clean) as evaluate,
                mock.patch.object(rr, "post_slack", return_value=True),
            ):
                self.assertEqual(rl.cmd_watch(args, service, store, mock.Mock()), rv.EXIT_OK)
            self.assertEqual(evaluate.call_count, 1)
            saved = rr.read_record(store, record["release_id"])
            self.assertEqual(saved["verdict"]["status"], "passed")
            receipt = saved["followup"]["checkpoints"]["window_60m"]
            self.assertEqual(receipt["attempt_history"][0]["check"], prior)

    def test_light_terminal_mutation_ends_at_15m_unchecked(self):
        service = rl.parse_light(light_data("cloud_run"))
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            _save(store, record)
            args = argparse.Namespace(release_id=record["release_id"], checkpoints="15 60", retry_completed=False)
            clean = {"problems": [], "parts": {"logs": {"status": "checked"}}, "scope": {}}
            with (
                mock.patch.object(rl, "wait_for_deploys", return_value=None),
                mock.patch.object(rl, "replaced_by", return_value=None),
                mock.patch.object(rl, "evaluate_window", return_value=clean) as evaluate,
                mock.patch.object(rv, "builds_since_rollback", return_value=["CANCELLED mutating build"]),
                mock.patch.object(rr, "post_slack", return_value=True),
            ):
                self.assertEqual(rl.cmd_watch(args, service, store, mock.Mock()), rv.EXIT_FAILED)
            self.assertEqual(evaluate.call_count, 1)
            self.assertTrue(rr.read_record(store, record["release_id"])["verdict"]["unchecked"])

    def test_owner_and_resumed_checkpoint_receipts(self):
        service = rl.parse_light(light_data("cloud_run"))
        for owned in (True, False):
            with self.subTest(owned=owned), tempfile.TemporaryDirectory() as tmp:
                store = _store(tmp)
                record = _record() if owned else _record(followup={})
                rv.ro.merge_plan(record, [15, 60])
                rv.ro.note_checkpoint(record, {"at": "original", "result": "passed"}, "window_15m")
                _save(store, record)
                args = argparse.Namespace(release_id=record["release_id"], checkpoints="15 60", retry_completed=False)
                clean = {"problems": [], "parts": {"logs": {"status": "checked"}}, "scope": {}}
                with (
                    mock.patch.object(rl, "wait_for_deploys", return_value=None),
                    mock.patch.object(rl, "replaced_by", return_value=None),
                    mock.patch.object(rl, "evaluate_window", return_value=clean) as evaluate,
                    mock.patch.object(rr, "post_slack", return_value=True),
                ):
                    outcome = rl.cmd_watch(args, service, store, mock.Mock())
                # An unclaimed release is owned by its PR author: green, like a claimed one.
                self.assertEqual(outcome, rv.EXIT_OK)
                self.assertEqual(evaluate.call_count, 1)
                saved = rr.read_record(store, record["release_id"])
                self.assertEqual(saved["followup"]["checkpoints"]["window_15m"]["completed_at"], "original")
                self.assertEqual(saved["followup"]["checkpoints"]["window_60m"]["state"], "completed")

    def test_final_light_checker_receipt_outage_still_exits_red(self):
        service = rl.parse_light(light_data("cloud_run"))
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            _save(store, record)
            args = argparse.Namespace(release_id=record["release_id"], checkpoints="60", retry_completed=False)
            with (
                mock.patch.object(rl, "wait_for_deploys", return_value=None),
                mock.patch.object(rl, "evaluate_window", side_effect=rr.RecordError("provider down")),
                mock.patch.object(rv, "_record_checkpoint_error"),
                mock.patch.object(rr, "post_slack", return_value=True),
            ):
                self.assertEqual(rl.cmd_watch(args, service, store, mock.Mock()), rv.EXIT_FAILED)

    def test_parse(self) -> None:
        """Parse."""
        run = rl.parse_light(light_data("cloud_run"))
        self.assertEqual(run.services, ("demo-a", "demo-b"))
        self.assertFalse(run.has_gate)
        composer = rl.parse_light(light_data("composer"))
        self.assertEqual(composer.sync_trigger, "demo-sync")
        self.assertEqual(composer.deploy_triggers, ("demo-sync",))
        self.assertEqual(run.deploy_triggers, ("demo-run-deploy",))
        bad = light_data("composer")
        bad["tier"] = "full"
        with self.assertRaises(rc.ConfigError):
            rl.parse_light(bad)
        with self.assertRaises(rc.ConfigError):
            rl.parse_light({**light_data("cloud_run"), "smoke": [{"type": "pods_ready", "deployment": "x"}]})

    def test_cloud_run_checks_and_rollback(self) -> None:
        """Cloud run checks and rollback."""
        service = rl.parse_light(light_data("cloud_run"))
        before = {
            "cloud_run/demo-a": {"latest_created": "a-1", "latest_ready": "a-1", "traffic": {"a-1": 100}},
            "cloud_run/demo-b": {"latest_created": "b-1", "latest_ready": "b-1", "traffic": {"b-1": 100}},
        }
        after = {
            "cloud_run/demo-a": {"latest_created": "a-2", "latest_ready": "a-2", "traffic": {"a-2": 100}},
            "cloud_run/demo-b": {"latest_created": "b-2", "latest_ready": "b-1", "traffic": {"b-1": 100}},
        }
        check = rl.check_cloud_run(before, after)
        self.assertEqual(check.status, "failed")
        self.assertIn("b-2 is not ready", " ".join(check.details))
        same = {key: {**value, "image": "img@1"} for key, value in before.items()}
        self.assertEqual(rl.check_cloud_run(same, same).status, "passed")
        moved = {key: {**value, "image": "img@2"} for key, value in before.items()}
        self.assertEqual(rl.check_cloud_run(same, moved).status, "failed")
        command = rl.rollback_command(service, {"previous_snapshot": before})
        self.assertIn("update-traffic demo-a", command)
        self.assertIn("--to-revisions=b-1=100", command)

    def test_composer_import_errors_and_rollback(self) -> None:
        """Composer import errors and rollback."""
        service = rl.parse_light(light_data("composer"))
        record = {
            "previous": {"commit": PREVIOUS},
            "previous_snapshot": {"composer/import_errors": {"errors": {"/home/airflow/gcs/dags/old.py": "E1"}}},
        }
        after = {
            "composer/import_errors": {
                "errors": {"/home/airflow/gcs/dags/old.py": "E1", "/home/airflow/gcs/dags/new.py": "NameError: x"}
            }
        }
        check = rl.check_import_errors(service, record, after)
        self.assertEqual(check.status, "failed")
        self.assertIn("dags/new.py: NameError: x", check.details)
        self.assertEqual(
            rl.rollback_command(service, record),
            f"gcloud builds triggers run demo-sync --region=us-central1 --project=orbit-search --sha={PREVIOUS}",
        )

    def test_import_check_skips_without_api(self) -> None:
        """Import check skips without api."""
        service = rl.parse_light(light_data("composer"))
        record = {"previous_snapshot": {"composer/import_errors": {"errors": None, "error": "HTTP 403"}}}
        check = rl.check_import_errors(
            service, record, {"composer/import_errors": {"errors": None, "error": "HTTP 403"}}
        )
        self.assertEqual((check.status, check.details), ("skipped", ["HTTP 403"]))

    def test_dag_file_is_the_origin(self) -> None:
        """Dag file is the origin."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dags").mkdir()
            (root / "dags" / "sync.py").write_text(
                'from airflow import DAG\n\nwith DAG(dag_id="orbit_sync") as dag:\n    pass\n'
            )
            code = rc.Code(source_prefixes=("dags/",), extensions=(".py",))
            tree = rs.SourceTree(root, code)
            problem = rs.Problem(kind="dag_failure", key="dag:orbit_sync", text="t", identifiers=["orbit_sync"])
            found = rs.origins(problem, tree)
            self.assertEqual([(f.path, f.name) for f in found], [("dags/sync.py", rs.MODULE)])
            change = rs.parse_change("c", [{"filename": "dags/sync.py", "patch": "@@ -1,1 +1,1 @@\n-x\n+y"}], {})
            score, reasons, _, _ = rs.score_commit(problem, {"title": "t"}, change, found, tree)
            self.assertGreaterEqual(score, rs.ORIGIN)

    def test_light_window_render(self) -> None:
        """Light window render."""
        service = rl.parse_light(light_data("composer"))
        text = kit.rendered(service)["release/services/demo-composer.release-verify.yaml"]
        self.assertIn('--service "${_SERVICE}" watch \\', text)
        self.assertIn('tags: ["demo-composer-release-verify"]', text)

    def test_airflow_lists_read_every_page(self) -> None:
        """Airflow lists read every page."""
        service = rl.parse_light(light_data("composer"))
        airflow = rl.Airflow.__new__(rl.Airflow)
        runs = [{"dag_id": f"d{index % 3}"} for index in range(250)]

        def get(path):
            """Answer with the next canned response."""
            offset = int(path.rsplit("offset=", 1)[1])
            return {"dag_runs": runs[offset : offset + rl.AIRFLOW_PAGE_SIZE], "total_entries": len(runs)}

        airflow.get = get
        now = datetime(2026, 10, 21, 12, tzinfo=rr.UTC)
        self.assertEqual(sum(airflow.failed_runs(now - timedelta(hours=1), now).values()), 250)
        # An answer that stops short of total_entries is not complete: the check is inconclusive.
        airflow.get = lambda path: {"dag_runs": runs[:10] if "offset=0" in path else [], "total_entries": 250}
        with self.assertRaises(rr.RecordError):
            airflow.failed_runs(now - timedelta(hours=1), now)
        self.assertEqual(service.kind, "composer")

    def test_failed_import_check_stays_red_when_the_record_write_fails(self) -> None:
        """Failed import check stays red when the record write fails."""
        service = rl.parse_light(light_data("composer"))
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _save(store, _record())
            marker = Path(tmp) / "failed"
            args = mock.Mock(
                release_id=_record()["release_id"], wait=False, failed_marker=str(marker), force_fail=False
            )
            failed = rv.Check("import_errors", "failed", ["1 file", "dags/new.py: NameError"])
            with (
                mock.patch.object(rl, "deploy_checks", return_value=([failed], {})),
                mock.patch.object(rl, "GoogleApi"),
                mock.patch.object(rr, "update_record", side_effect=rr.RecordError("GCS down")),
                self.assertRaises(rr.RecordError),
            ):
                rl.cmd_check(args, service, store, mock.Mock())
            self.assertTrue(marker.exists())

    def test_alert_once_and_older_target(self) -> None:
        """Alert once and older target."""
        service = rl.parse_light(light_data("composer"))
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            older = _record(release_id="20261021T080000Z-ddddddddd", commit="d" * 40, previous={"commit": "e" * 40})
            newer = _record(previous={"release_id": older["release_id"], "commit": "d" * 40})
            _save(store, older, latest=False)
            _save(store, newer)
            new_errors = {
                "problems": [{"clear": True, "suspects": [{"release_id": older["release_id"]}]}],
                "scope": {"releases": [newer["release_id"], older["release_id"]]},
            }
            regression = {"kind": "new_error", "name": "dag:x", "text": "DAG x fails"}
            with mock.patch.object(rv, "suspected_prs", return_value=[]), mock.patch.object(rl, "rr_token"):
                saved = rl._fail(service, store, newer, "window_15m", [regression], new_errors)
            suggestion = saved["rollback_suggestion"]
            self.assertEqual(suggestion["target"]["rollback_to"], f"previous-of:{older['release_id']}")
            self.assertTrue(suggestion["how"][0].endswith(f"--sha={'e' * 40}"))
            with (
                mock.patch.dict(os.environ, {"RELEASE_SLACK_DISABLED": "0"}),
                mock.patch.object(rr, "post_slack", return_value=True) as slack,
            ):
                rl.alert(service, store, saved)
                rl.alert(service, store, rr.read_record(store, newer["release_id"]))
            self.assertEqual(slack.call_count, 1)
            self.assertIn(f"--sha={'e' * 40}", json.dumps(slack.call_args[0][1]))

    def test_watch_waits_for_a_running_sync(self) -> None:
        """Watch waits for a running sync."""
        service = rl.parse_light(light_data("composer"))
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(build={"id": "own"})
            _save(store, record)
            answers = iter([["sync2"], []])
            with mock.patch.object(rl, "running_deploys", side_effect=lambda *_: next(answers)):
                self.assertIsNone(rl.wait_for_deploys(service, store, record, sleep=lambda _: None))
            with mock.patch.object(rl, "running_deploys", return_value=["sync3"]):
                self.assertEqual(rl.replaced_by(service, store, record), "deploy build sync3")

    def test_light_warning_names_its_own_rollback(self) -> None:
        """Light warning names its own rollback."""
        service = rl.parse_light(light_data("composer"))
        line = rv.warning_line(service, _record(), [{"text": "x"}], rr.LocalStore(Path("/tmp")), "gcloud builds x")
        self.assertIn("roll back with gcloud builds x if", line)
        self.assertNotIn("release_rollback.py", line)

    def test_cloud_run_rollback_restores_the_serving_split(self) -> None:
        """Cloud run rollback restores the serving split."""
        service = rl.parse_light(light_data("cloud_run"))
        before = {
            # A human moved traffic back to a-1; a-2 is still the newest ready revision.
            "cloud_run/demo-a": {"latest_ready": "a-2", "traffic": {"a-1": 100, "a-2": 0}},
            "cloud_run/demo-b": {"latest_ready": "b-3", "traffic": {"b-2": 90, "b-3": 10}},
        }
        command = rl.rollback_command(service, {"previous_snapshot": before})
        self.assertIn(
            "update-traffic demo-a --region=us-central1 --project=orbit-search --to-revisions=a-1=100", command
        )
        self.assertIn("--to-revisions=b-2=90,b-3=10", command)

    def test_failed_deploy_fails_the_check(self) -> None:
        """Failed deploy fails the check."""
        service = rl.parse_light(light_data("cloud_run"))
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _save(store, _record(status="deploy_failed"))
            args = mock.Mock(release_id=_record()["release_id"], wait=False, failed_marker="", force_fail=False)
            passed = rv.Check("revisions", "passed", ["2 services checked"])
            with (
                mock.patch.object(rl, "deploy_checks", return_value=([passed], {})),
                mock.patch.object(rl, "GoogleApi"),
                mock.patch.object(rl, "_fail", side_effect=lambda *a: a[2]) as failed,
                mock.patch.object(rl, "alert"),
            ):
                self.assertEqual(rl.cmd_check(args, service, store, mock.Mock()), rv.EXIT_FAILED)
            self.assertEqual(failed.call_args[0][4][0]["name"], "deploy")

    def test_first_light_release_is_a_target(self) -> None:
        """The first recorded Cloud Run release has no previous commit, only its before-snapshot."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            first = _record(release_id="20261021T080000Z-ddddddddd", previous={}, previous_snapshot={"cloud_run/a": {}})
            newer = _record(previous={"release_id": first["release_id"]})
            _save(store, first, latest=False)
            _save(store, newer)
            problems = [{"clear": True, "suspects": [{"release_id": first["release_id"]}]}]
            scope = {"releases": [newer["release_id"], first["release_id"]]}
            target = rs.rollback_target(newer, store, problems, scope)
            self.assertEqual(target["rollback_to"], f"previous-of:{first['release_id']}")

    def test_a_build_during_the_checks_reruns_the_checkpoint(self) -> None:
        """A build during the checks reruns the checkpoint."""
        service = rl.parse_light(light_data("cloud_run"))
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(
                times={"deploy_started": "2026-10-21T10:00:00Z", "deploy_finished": "2026-10-21T10:05:00Z"}
            )
            _save(store, record)
            args = mock.Mock(release_id=record["release_id"], checkpoints="15")
            clean = {"problems": [], "parts": {"logs": {"status": "checked"}}, "scope": {}}
            with (
                mock.patch.object(rl, "wait_for_deploys", return_value=None),
                mock.patch.object(rl, "replaced_by", side_effect=["deploy build b2", None]),
                mock.patch.object(rl, "evaluate_window", return_value=clean) as evaluate,
                mock.patch.object(rr, "post_slack", return_value=True),
            ):
                rl.cmd_watch(args, service, store, mock.Mock())
            self.assertEqual(evaluate.call_count, 2)
            self.assertEqual(rr.read_record(store, record["release_id"])["verdict"]["status"], "passed")

    def test_last_checkpoint_error_is_inconclusive(self) -> None:
        """Last checkpoint error is inconclusive."""
        service = rl.parse_light(light_data("cloud_run"))
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(
                times={"deploy_started": "2026-10-21T10:00:00Z", "deploy_finished": "2026-10-21T10:05:00Z"}
            )
            _save(store, record)
            args = mock.Mock(release_id=record["release_id"], checkpoints="15")
            with (
                mock.patch.object(rl, "wait_for_deploys", return_value=None),
                mock.patch.object(rl, "evaluate_window", side_effect=rr.RecordError("logs down")),
                mock.patch.object(rr, "post_slack", return_value=True) as slack,
            ):
                self.assertEqual(rl.cmd_watch(args, service, store, mock.Mock()), rv.EXIT_FAILED)
            saved = rr.read_record(store, record["release_id"])
            self.assertEqual((saved["verdict"]["status"], saved["verdict"]["unchecked"]), ("inconclusive", True))
            self.assertIn("inconclusive", slack.call_args[0][0])
            # The next release's watch judges the unchecked release with it.
            newer = _record(
                release_id="20261021T120000Z-ccccccccc",
                times={"deploy_started": "2026-10-21T12:00:00Z", "deploy_finished": "2026-10-21T12:05:00Z"},
                previous={"release_id": record["release_id"]},
            )
            _save(store, newer)
            scope = rs.release_scope(store, newer, datetime(2026, 10, 21, 12, 30, tzinfo=rr.UTC))
            self.assertEqual([r["release_id"] for r in scope.releases], [newer["release_id"], record["release_id"]])

    def test_light_alert_footer_has_no_hold(self) -> None:
        """Light alert footer has no hold."""
        service = rl.parse_light(light_data("cloud_run"))
        record = _record(verdict={"status": "failed", "phase": "deploy", "reasons": ["x"]}, rollback_suggestion=None)
        _, blocks = rv.rollback_alert(service, record, None, "gs://x", command="gcloud run ...")
        self.assertIn("no hold for this service", json.dumps(blocks))


# --------------------------------------------------------------------------- light kinds, tier full

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def full_run_data(**overrides) -> dict:
    """A tier full cloud_run config: two services, their base URLs, one smoke check each."""
    data = {
        "service": "demo-run",
        "kind": "cloud_run",
        "tier": "full",
        "github_repo": "orbit-search/demo",
        "connected_repository": "orbit-search-demo",
        "cloud_run_services": ["demo-a", "demo-b"],
        "cloud_run_urls": {"demo-a": "https://demo-a-xyz-uc.a.run.app", "demo-b": "https://demo-b-xyz-uc.a.run.app"},
        "triggers": {
            "dev": ["demo-run-deploy-dev"],
            "prod": ["demo-run-prod", "demo-run-deploy"],
            "rollback": "demo-run-rollback",
        },
        "smoke": [
            {"type": "http_public", "name": "a health", "url": "https://demo-a-xyz-uc.a.run.app/health"},
            {"type": "http_public", "name": "b health", "url": "https://demo-b-xyz-uc.a.run.app/health"},
        ],
        "verify": {"log_filter": 'resource.type="cloud_run_revision"'},
        "code": {"source_prefixes": ["src/"], "extensions": [".ts"]},
    }
    data.update(overrides)
    return data


def full_composer_data(**overrides) -> dict:
    """A tier full composer config."""
    data = {
        "service": "demo-flow",
        "kind": "composer",
        "tier": "full",
        "github_repo": "orbit-search/demo",
        "connected_repository": "orbit-search-demo",
        "composer": {"environment": "env-v3", "bucket": "gs://demo-bucket"},
        "triggers": {"dev": ["demo-flow-deploy-dev"], "prod": ["demo-flow-prod"], "rollback": "demo-flow-rollback"},
        "verify": {"log_filter": 'resource.type="cloud_composer_environment"'},
        "code": {"source_prefixes": ["dags/"], "extensions": [".py"]},
    }
    data.update(overrides)
    return data


RUN = rl.parse_light(full_run_data())
FLOW = rl.parse_light(full_composer_data())


class FakeGcloud:
    """Answers the gcloud and gsutil calls of the light kinds; keeps Cloud Run traffic, tags and revisions."""

    def __init__(self) -> None:
        """Two services, each serving revision 1; revision 2 is the candidate."""
        self.commands: list[list[str]] = []
        self.revisions = {"demo-a-1": DIGEST_A, "demo-b-1": DIGEST_A, "demo-a-2": DIGEST_B, "demo-b-2": DIGEST_B}
        self.not_ready: set[str] = set()
        self.traffic = {"demo-a": {"demo-a-1": 100}, "demo-b": {"demo-b-1": 100}}
        self.tags = {"demo-a": {"candidate": "demo-a-2"}, "demo-b": {"candidate": "demo-b-2"}}
        self.fail: str = ""

    def __call__(self, command: list[str], timeout: float = 120) -> str:
        """Answer one recorded command."""
        self.commands.append(command)
        if self.fail and self.fail in " ".join(command):
            raise rr.RecordError(f"{self.fail} failed")
        if command[:4] == ["gcloud", "run", "services", "describe"]:
            name = command[4]
            traffic = [{"revisionName": rev, "percent": pct} for rev, pct in self.traffic[name].items()]
            traffic += [{"revisionName": rev, "percent": 0, "tag": tag} for tag, rev in self.tags[name].items()]
            return json.dumps(
                {
                    "spec": {"template": {"spec": {"containers": [{"image": "img"}]}}},
                    "status": {
                        "latestCreatedRevisionName": f"{name}-2",
                        "latestReadyRevisionName": f"{name}-2",
                        "traffic": traffic,
                        "url": f"https://{name}-xyz-uc.a.run.app",
                    },
                }
            )
        if command[:4] == ["gcloud", "run", "revisions", "describe"]:
            revision = command[4]
            if revision not in self.revisions:
                raise rr.RecordError(f"revision {revision} not found")
            ready = "False" if revision in self.not_ready else "True"
            return json.dumps(
                {
                    "spec": {"containers": [{"image": f"gcr.io/p/x@{self.revisions[revision]}"}]},
                    "status": {
                        "conditions": [{"type": "Ready", "status": ready}],
                        "imageDigest": f"gcr.io/p/x@{self.revisions[revision]}",
                    },
                }
            )
        if command[:4] == ["gcloud", "run", "services", "update-traffic"]:
            split = command[-1].split("=", 1)[1]
            self.traffic[command[4]] = {item.split("=")[0]: int(item.split("=")[1]) for item in split.split(",")}
            return ""
        if command[:4] == ["gcloud", "composer", "environments", "describe"]:
            return "gs://demo-bucket/dags\n"
        if command[0] == "gsutil":
            return ""
        raise AssertionError(f"unexpected command {command}")


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
        "parse_result": "",
        "build_url": "https://build",
        "wait": False,
        "dry_run_dir": None,
        "out_dir": "",
        "poll_seconds": 0.0,
        "older_timeout_seconds": 1,
        "dev_timeout_seconds": 1,
    }
    defaults.update(values)
    return argparse.Namespace(**defaults)


class LightCandidateCleanupTest(unittest.TestCase):
    def test_removes_only_matching_tag_with_etag_and_preserves_traffic(self) -> None:
        """Cleanup is a traffic-only conditional update, preserving percentages and unrelated tags."""
        traffic = [
            {"revision": "demo-a-1", "percent": 100, "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION"},
            {"revision": "demo-a-2", "percent": 0, "tag": "candidate"},
            {"revision": "demo-a-1", "percent": 0, "tag": "stable"},
        ]
        with mock.patch.object(rl, "GoogleApi"), mock.patch.object(rl, "_http_json") as request:
            request.side_effect = [{"etag": "v1", "traffic": traffic}, {"done": True}, {"traffic": traffic[:1]}]
            self.assertEqual(rl.remove_candidate_tags(RUN, {"demo-a": "demo-a-2"}), [])
        patch = request.call_args_list[1].args[0]
        self.assertEqual(patch.get_method(), "PATCH")
        self.assertTrue(patch.full_url.endswith("?updateMask=traffic"))
        body = json.loads(patch.data)
        self.assertEqual(body["etag"], "v1")
        self.assertEqual(body["traffic"], [traffic[0], {"revision": "demo-a-2", "percent": 0}, traffic[2]])

    def test_leaves_a_newer_candidate_alone(self) -> None:
        """An older build never removes a newer revision's tag."""
        with mock.patch.object(rl, "GoogleApi"), mock.patch.object(rl, "_http_json") as request:
            request.return_value = {"etag": "v2", "traffic": [{"revision": "demo-a-3", "tag": "candidate"}]}
            self.assertEqual(rl.remove_candidate_tags(RUN, {"demo-a": "demo-a-2"}), [])
            self.assertEqual(request.call_count, 1)

    def test_conflict_and_missing_etag_refuse_unguarded_cleanup(self) -> None:
        """A concurrent change or unavailable fingerprint cannot turn into unconditional tag removal."""
        traffic = [{"revision": "demo-a-2", "tag": "candidate"}]
        for current, responses in (
            ({"traffic": traffic}, []),
            ({"etag": "v1", "traffic": traffic}, [rl.GuardError("PATCH returned HTTP 409")]),
        ):
            with (
                self.subTest(current=current),
                mock.patch.object(rl, "GoogleApi"),
                mock.patch.object(rl, "_http_json") as request,
            ):
                request.side_effect = [current, *responses]
                self.assertEqual(len(rl.remove_candidate_tags(RUN, {"demo-a": "demo-a-2"})), 1)
                if responses:
                    self.assertEqual(json.loads(request.call_args_list[1].args[0].data)["etag"], "v1")
                self.assertEqual(request.call_count, 1 + len(responses))

    def test_cleanup_must_finish_and_tag_must_disappear(self) -> None:
        """A failed operation or a retained public tag makes cleanup fail visibly."""
        current = {"etag": "v1", "traffic": [{"revision": "demo-a-2", "tag": "candidate"}]}
        for responses in ([current, {"done": True, "error": "denied"}], [current, {"done": True}, current]):
            with (
                self.subTest(responses=responses),
                mock.patch.object(rl, "GoogleApi"),
                mock.patch.object(rl, "_http_json", side_effect=responses),
            ):
                self.assertEqual(len(rl.remove_candidate_tags(RUN, {"demo-a": "demo-a-2"})), 1)


class LightFullConfigTest(unittest.TestCase):
    def test_cloud_run_full_parse(self) -> None:
        """Tier full reads the triggers and URLs; dev verify calls the smoke checks on the candidate tag."""
        self.assertTrue(RUN.has_gate and RUN.auto_rollback and RUN.has_dev)
        self.assertEqual(RUN.prod.deploy_triggers, ("demo-run-prod", "demo-run-deploy"))
        self.assertEqual(RUN.dev.deploy_trigger, "demo-run-deploy-dev")
        self.assertEqual(RUN.rollback_trigger, "demo-run-rollback")
        self.assertEqual(RUN.rollback_build_tag, "demo-run-rollback")
        self.assertEqual(
            [check["url"] for check in RUN.dev.smoke],
            [
                "https://candidate---demo-a-xyz-uc.a.run.app/health",
                "https://candidate---demo-b-xyz-uc.a.run.app/health",
            ],
        )
        self.assertEqual(RUN.prod.smoke[0]["url"], "https://demo-a-xyz-uc.a.run.app/health")
        self.assertEqual(set(RUN.environments), {"prod", "dev"})

    def test_cloud_run_full_validation(self) -> None:
        """Bad tier full cloud_run configs."""
        bad = [
            full_run_data(deploy_triggers=["x"]),
            full_run_data(cloud_run_urls={"demo-a": "https://demo-a.example.com", "demo-b": "https://b.run.app"}),
            full_run_data(smoke=[{"type": "http_public", "url": "https://demo-a-xyz-uc.a.run.app/health"}]),
            full_run_data(smoke=[{"type": "http_public", "url": "https://other.example.com/health"}]),
            full_run_data(triggers={"prod": ["p"]}),
            full_run_data(tier="check_rollback"),
        ]
        for data in bad:
            with self.assertRaises(rc.ConfigError, msg=json.dumps(data)[:200]):
                rl.parse_light(data)

    def test_composer_full_parse(self) -> None:
        """Composer tier full: the bucket fallback, the candidate folder under data/."""
        self.assertEqual(FLOW.bucket, "gs://demo-bucket")
        self.assertEqual(FLOW.candidate_prefix, "data/release-candidate/")
        self.assertEqual(
            rl.candidate_uri(FLOW, "gs://demo-bucket/dags", COMMIT),
            f"gs://demo-bucket/data/release-candidate/{COMMIT}/dags",
        )
        self.assertEqual(FLOW.dev.smoke, ())
        with self.assertRaises(rc.ConfigError):
            rl.parse_light(full_composer_data(composer={"environment": "e", "candidate_prefix": "dags/candidates/"}))
        with self.assertRaises(rc.ConfigError):
            rl.parse_light(full_composer_data(composer={"environment": "e", "bucket": "demo-bucket"}))

    def test_render_full(self) -> None:
        """Tier full renders a window build that may roll back and a rollback build; check_only renders neither."""
        files = kit.rendered(RUN)
        window = files["release/services/demo-run.release-verify.yaml"]
        self.assertIn('"RELEASE_AUTO_ROLLBACK=${_AUTO_ROLLBACK}"', window)
        self.assertIn("demo-run-rollback trigger", window)
        rollback = files["release/services/demo-run.rollback.yaml"]
        self.assertIn("python3 release/release_rollback.py apply \\", rollback)
        self.assertIn("--service demo-run \\", rollback)
        self.assertIn('tags: ["demo-run-rollback"]', rollback)
        self.assertNotIn("kubectl", rollback)
        self.assertIn("candidate folder goes to dags/", kit.rendered(FLOW)["release/services/demo-flow.rollback.yaml"])
        light = kit.rendered(rl.parse_light(light_data("cloud_run")))
        self.assertEqual(list(light), ["release/services/demo-cloud-run.release-verify.yaml"])
        self.assertNotIn("AUTO_ROLLBACK", light["release/services/demo-cloud-run.release-verify.yaml"])

    def test_build_service_account(self) -> None:
        """The window and rollback builds run as cloud-build@ unless the config names its own build identity."""
        for name, text in kit.rendered(RUN).items():
            self.assertIn(
                'serviceAccount: "projects/orbit-search/serviceAccounts/cloud-build@orbit-search.iam.gserviceaccount.com"',
                text,
                name,
            )
        own = rl.parse_light(full_run_data(build_service_account="demo-run-build@orbit-search.iam.gserviceaccount.com"))
        self.assertEqual(own.build_service_account, "demo-run-build@orbit-search.iam.gserviceaccount.com")
        files = kit.rendered(own)
        self.assertEqual(len(files), 2)
        for name, text in files.items():
            self.assertIn(
                'serviceAccount: "projects/orbit-search/serviceAccounts/demo-run-build@orbit-search.iam.gserviceaccount.com"',
                text,
                name,
            )
            self.assertNotIn("cloud-build@", text, name)
        for bad in (
            "demo-run-build@other-project.iam.gserviceaccount.com",
            "621810938413-compute@developer.gserviceaccount.com",
            "projects/orbit-search/serviceAccounts/demo-run-build@orbit-search.iam.gserviceaccount.com",
            'x@orbit-search.iam.gserviceaccount.com"\nsteps: []',
            "demo-run-build@orbit-search.iam.gserviceaccount.com\n",
        ):
            with self.subTest(bad=bad), self.assertRaises(rc.ConfigError):
                rl.parse_light(full_run_data(build_service_account=bad))

    def test_any_service_loader(self) -> None:
        """release_config.load_any_service picks the GKE or the light loader by kind."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "demo-run.json").write_text(json.dumps(full_run_data()))
            (folder / "demo.json").write_text(json.dumps(service_data()))
            with mock.patch.object(rc, "SERVICES_DIR", folder):
                self.assertEqual(rc.config_kind("demo-run"), "cloud_run")
                self.assertEqual(rc.config_kind("demo"), "gke")
                self.assertIsInstance(rc.load_any_service("demo-run"), rl.LightService)
                self.assertIsInstance(rc.load_any_service("demo"), rc.Service)
                with self.assertRaises(rc.ConfigError):
                    rc.load_service("demo-run")
                with self.assertRaises(rc.ConfigError):
                    rc.config_kind()


class LightFullDevTest(unittest.TestCase):
    def setUp(self) -> None:
        """A local dev store, a fake gcloud and no network."""
        self.tmp = tempfile.TemporaryDirectory()
        self.store = rr.LocalStore(Path(self.tmp.name) / "dev")
        self.gcloud = FakeGcloud()
        self.patches = [
            mock.patch.object(rl, "GoogleApi"),
            mock.patch.object(rr, "github_token", side_effect=guard.GuardError("no token")),
            mock.patch.object(rr, "build_info", return_value={"id": "b1"}),
            mock.patch.object(rr, "post_slack", return_value=True),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        """Remove what the test created."""
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def _record_dev(self, pushed: str = DIGEST_B) -> str:
        """Begin and finish a dev release of the candidate revisions; return its release id."""
        images = Path(self.tmp.name) / "images.json"
        images.write_text(json.dumps({"demo-a": f"gcr.io/p/a@{pushed}", "demo-b": f"gcr.io/p/b@{pushed}"}))
        id_file = Path(self.tmp.name) / "release_id"
        args = _args(env="dev", images_file=str(images), release_id_file=str(id_file))
        self.assertEqual(rl.cmd_begin(args, RUN, self.store, self.gcloud), 0)
        release_id = id_file.read_text().strip()
        self.assertEqual(rl.cmd_finish(_args(env="dev", release_id=release_id), RUN, self.store, self.gcloud), 0)
        return release_id

    def test_cloud_run_dev_verify_passes_and_indexes_the_revisions(self) -> None:
        """The candidate revisions run the pushed digests and answer on the tag URLs: passed, indexed."""
        release_id = self._record_dev()
        record = rr.read_record(self.store, release_id)
        self.assertEqual(record["artifacts"], {"demo-a": f"demo-a-2@{DIGEST_B}", "demo-b": f"demo-b-2@{DIGEST_B}"})
        calls = []

        def public(check, get=None):
            """Record the URL and pass."""
            calls.append(check["url"])
            return rv.Check(check["name"], "passed", ["ok"])

        with (
            mock.patch.object(rv, "check_http_public", side_effect=public),
            mock.patch.object(rv, "github_compare_status", return_value="behind"),
            mock.patch.object(rr, "github_token", return_value="t"),
        ):
            code = rl.cmd_dev_verify(_args(env="dev", release_id=release_id), RUN, self.store, self.gcloud)
        self.assertEqual(code, rv.EXIT_OK)
        self.assertTrue(all(url.startswith("https://candidate---") for url in calls))
        index, _ = self.store.read(rg.dev_commit_index(COMMIT))
        self.assertEqual(index["verdict"]["status"], "passed")
        self.assertEqual(index["artifacts"], record["artifacts"])
        pointer, _ = self.store.read(rg.DEV_VERIFIED_POINTER)
        self.assertEqual(pointer["commit"], COMMIT)

    def test_cloud_run_dev_verify_fails_on_another_digest_or_a_moved_tag(self) -> None:
        """A candidate that runs another image (the deploy failed silently) or a tag that moved fails dev verify."""
        release_id = self._record_dev(pushed=DIGEST_C)
        self.gcloud.tags["demo-b"]["candidate"] = "demo-b-1"
        marker = Path(self.tmp.name) / "failed"
        with (
            mock.patch.object(rv, "check_http_public", return_value=rv.Check("x", "passed", ["ok"])),
            mock.patch.object(rv, "newer_dev_build", return_value=None),
        ):
            args = _args(env="dev", release_id=release_id, failed_marker=str(marker))
            self.assertEqual(rl.cmd_dev_verify(args, RUN, self.store, self.gcloud), rv.EXIT_FAILED)
        self.assertTrue(marker.exists())
        record = rr.read_record(self.store, release_id)
        reasons = " ".join(record["verdict"]["reasons"])
        self.assertIn(f"this build pushed {DIGEST_C}", reasons)
        self.assertIn("tag moved to demo-b-1", reasons)
        self.assertFalse(self.store.read(rg.DEV_VERIFIED_POINTER)[0])

    def test_dev_verify_superseded_is_not_red(self) -> None:
        """A failure while a newer dev build replaced this one is superseded: no alert, no red marker."""
        release_id = self._record_dev()
        marker = Path(self.tmp.name) / "failed"
        with (
            mock.patch.object(rv, "check_http_public", return_value=rv.Check("x", "failed", ["HTTP 503"])),
            mock.patch.object(rv, "newer_dev_build", return_value="newer build b2"),
        ):
            args = _args(env="dev", release_id=release_id, failed_marker=str(marker))
            self.assertEqual(rl.cmd_dev_verify(args, RUN, self.store, self.gcloud), rv.EXIT_OK)
        self.assertFalse(marker.exists())
        self.assertEqual(rr.read_record(self.store, release_id)["verdict"]["status"], "superseded")

    def test_composer_dev_verify(self) -> None:
        """Composer dev verify: the candidate folder is unchanged and the parse has no new import error."""
        manifest = ("sha256:" + "d" * 64, 3)
        with mock.patch.object(rl, "folder_manifest", return_value=manifest):
            id_file = Path(self.tmp.name) / "release_id"
            rl.cmd_begin(_args(env="dev", release_id_file=str(id_file)), FLOW, self.store, self.gcloud)
            release_id = id_file.read_text().strip()
            rl.cmd_finish(_args(env="dev", release_id=release_id), FLOW, self.store, self.gcloud)
            record = rr.read_record(self.store, release_id)
            uri = f"gs://demo-bucket/data/release-candidate/{COMMIT}/dags"
            self.assertEqual(record["artifacts"], {"dags": f"{uri} {manifest[0]}"})
            parse = Path(self.tmp.name) / "parse.json"
            parse.write_text(
                json.dumps(
                    {
                        "candidate": {
                            "airflow": "3.1.7",
                            "files": 3,
                            "dags": 2,
                            "errors": {"a.py": "VARIABLE_NOT_FOUND: x"},
                        },
                        "baseline": {"airflow": "3.1.7", "files": 3, "dags": 2, "errors": {}},
                    }
                )
            )
            with mock.patch.object(rv, "github_compare_status", return_value="behind"):
                args = _args(env="dev", release_id=release_id, parse_result=str(parse))
                self.assertEqual(rl.cmd_dev_verify(args, FLOW, self.store, self.gcloud), rv.EXIT_OK)
            checks = rr.read_record(self.store, release_id)["checks"]["deploy"]["checks"]
            self.assertIn("not judged", " ".join(checks[1]["details"]))
        # The folder changed after the record: fail.
        with (
            mock.patch.object(rl, "folder_manifest", return_value=("sha256:" + "e" * 64, 3)),
            mock.patch.object(rv, "newer_dev_build", return_value=None),
        ):
            args = _args(env="dev", release_id=release_id, parse_result=str(parse))
            self.assertEqual(rl.cmd_dev_verify(args, FLOW, self.store, self.gcloud), rv.EXIT_FAILED)

    def test_dev_turn_uses_the_dev_triggers(self) -> None:
        """turn --env dev is single flight among the dev triggers, with no branch check."""
        with mock.patch.object(rl.guard, "Guard") as fake:
            fake.return_value.wait_for_turn.side_effect = guard.Superseded("newer")
            self.assertEqual(rl.cmd_turn(_args(env="dev"), RUN, self.store, self.gcloud), guard.EXIT_SUPERSEDED)
        kwargs = fake.call_args.kwargs
        self.assertEqual(
            (kwargs["peer_triggers"], kwargs["promoting_trigger"]), (("demo-run-deploy-dev",), "demo-run-deploy-dev")
        )
        fake.return_value.check_on_branch.assert_not_called()
        with self.assertRaises(rc.ConfigError):
            rl.cmd_turn(_args(env="dev"), rl.parse_light(light_data("cloud_run")), self.store, self.gcloud)


class LightFullParseTest(unittest.TestCase):
    def test_compare_and_check(self) -> None:
        """New import errors fail; errors prod's DAGs have, and environment-only errors, do not."""
        result = {
            "candidate": {
                "airflow": "3.1.7",
                "files": 3,
                "dags": 2,
                "errors": {
                    "a.py": "ModuleNotFoundError: No module named 'x'",
                    "b.py": "NameError: y",
                    "c.py": "airflow.sdk.exceptions.AirflowRuntimeError: VARIABLE_NOT_FOUND: {'message': 'Variable V not found'}",
                },
            },
            "baseline": {
                "airflow": "3.1.7",
                "files": 3,
                "dags": 2,
                "errors": {"a.py": "ModuleNotFoundError: No module named 'x'"},
            },
        }
        new, environment = rl.compare_parse(result)
        self.assertEqual((list(new), list(environment)), (["b.py"], ["c.py"]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps(result))
            check = rl.check_parse(FLOW, str(path))
            self.assertEqual(check.status, "failed")
            self.assertIn("dags/b.py: NameError: y", check.details)
            path.write_text(json.dumps({"error": "candidate: pip failed"}))
            self.assertEqual(rl.check_parse(FLOW, str(path)).status, "failed")
            self.assertEqual(rl.check_parse(FLOW, str(Path(tmp) / "missing.json")).status, "failed")
            self.assertEqual(rl.check_parse(FLOW, "").status, "failed")

    def test_parse_requires_complete_results_from_both_folders(self) -> None:
        """A missing child result or malformed metadata cannot authorize promotion."""
        parsed = {"airflow": "3.1.7", "files": 0, "dags": 0, "errors": {}}
        malformed = [{}, [], None]
        for role in ("candidate", "baseline"):
            malformed.append({other: parsed for other in ("candidate", "baseline") if other != role})
            for field in parsed:
                partial = {name: value for name, value in parsed.items() if name != field}
                malformed.append({"candidate": parsed, "baseline": parsed, role: partial})
            for field, value in (
                ("airflow", ""),
                ("files", True),
                ("dags", -1),
                ("files", "1"),
                ("errors", []),
                ("errors", {"a.py": None}),
            ):
                malformed.append({"candidate": parsed, "baseline": parsed, role: {**parsed, field: value}})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            for result in malformed:
                with self.subTest(result=result):
                    path.write_text(json.dumps(result))
                    self.assertEqual(rl.check_parse(FLOW, str(path)).status, "failed")
            path.write_text(json.dumps({"candidate": parsed, "baseline": parsed}))
            self.assertEqual(rl.check_parse(FLOW, str(path)).status, "passed")

    def test_parse_dags_runs_each_folder_alone(self) -> None:
        """parse-dags parses the candidate and the baseline in two processes, each with its own DAG folder."""
        seen = []

        def run(command, env, **_):
            """Write a canned parse result where the child would."""
            seen.append(env["AIRFLOW__CORE__DAGS_FOLDER"])
            Path(command[command.index("--out") + 1]).write_text(json.dumps({"errors": {}, "files": 1}))
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "result.json"
            with mock.patch.object(rl.subprocess, "run", side_effect=run):
                self.assertEqual(rl.main(["parse-dags", "--candidate", tmp, "--baseline", "/", "--out", str(out)]), 0)
            result = json.loads(out.read_text())
            self.assertEqual(sorted(result), ["baseline", "candidate"])
            self.assertEqual(seen, [str(Path(tmp).resolve()), "/"])
            failing = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "ImportError: airflow"))
            with mock.patch.object(rl.subprocess, "run", failing):
                rl.main(["parse-dags", "--candidate", tmp, "--baseline", tmp, "--out", str(out)])
            self.assertIn("ImportError", json.loads(out.read_text())["error"])

    def test_folder_manifest_pages(self) -> None:
        """The manifest covers every page; the same objects give the same hash, a changed one another."""
        pages = [
            {"items": [{"name": "data/c/dags/a.py", "crc32c": "x", "size": "1"}], "nextPageToken": "p2"},
            {"items": [{"name": "data/c/dags/b/c.py", "crc32c": "y", "size": "2"}]},
        ]
        with mock.patch.object(rl, "_http_json", side_effect=list(pages)):
            first = rl.folder_manifest("gs://bkt/data/c/dags", lambda: "t")
        with mock.patch.object(rl, "_http_json", side_effect=list(reversed(pages[:1])) + [pages[1]]):
            again = rl.folder_manifest("gs://bkt/data/c/dags/", lambda: "t")
        self.assertEqual(first, again)
        self.assertEqual(first[1], 2)
        changed = [pages[0], {"items": [{"name": "data/c/dags/b/c.py", "crc32c": "z", "size": "2"}]}]
        with mock.patch.object(rl, "_http_json", side_effect=changed):
            self.assertNotEqual(rl.folder_manifest("gs://bkt/data/c/dags", lambda: "t")[0], first[0])
        with mock.patch.object(rl, "_http_json", return_value={}):
            self.assertEqual(rl.folder_manifest("gs://bkt/none", lambda: "t"), ("", 0))

    def test_downloaded_commit_manifest_matches_gcs_metadata(self) -> None:
        """The standard CRC32C test vector and local bytes define the expected remote artifact."""
        self.assertEqual(rl.crc32c(b"123456789"), "4waSgw==")
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "a.py").write_bytes(b"123456789")
            expected = rl.local_dag_manifest(folder)
        payload = {"items": [{"name": "dags/a.py", "crc32c": "4waSgw==", "size": "9"}]}
        with mock.patch.object(rl, "_http_json", return_value=payload):
            self.assertEqual(rl.folder_manifest("gs://bucket/dags", lambda: "t"), (expected, 1))


class LightFullProdTest(unittest.TestCase):
    def setUp(self) -> None:
        """Local stores, a fake gcloud and no network."""
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.gcloud = FakeGcloud()
        self.prod = rr.LocalStore(self.root / "prod")
        self.patches = [
            mock.patch.object(rl, "GoogleApi"),
            mock.patch.object(rr, "github_token", return_value="t"),
            mock.patch.object(rr, "build_info", return_value={"id": "b1"}),
            mock.patch.object(rr, "post_slack", return_value=True),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        """Remove what the test created."""
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def test_gate_compares_artifacts(self) -> None:
        """The prod gate promotes only the artifacts that dev verify checked."""
        stores = rg.Stores(service=RUN, prod=self.prod, dev=rr.LocalStore(self.root / "dev"))
        verified = {"demo-a": f"demo-a-2@{DIGEST_B}", "demo-b": f"demo-b-2@{DIGEST_B}"}
        stores.dev.write(
            rg.dev_commit_index(COMMIT), {"commit": COMMIT, "verdict": {"status": "passed"}, "artifacts": verified}, 0
        )
        self.assertTrue(rg.decide(stores, COMMIT, "t", artifacts=dict(verified)).go)
        moved = rg.decide(stores, COMMIT, "t", artifacts={**verified, "demo-b": f"demo-b-3@{DIGEST_C}"})
        self.assertEqual(moved.status, "unverified")
        self.assertIn("demo-b-3", moved.reason)
        self.assertEqual(rg.decide(stores, COMMIT, "t", artifacts={}).status, "unverified")
        artifacts_file = self.root / "artifacts.json"
        artifacts_file.write_text(json.dumps(verified))
        args = argparse.Namespace(
            commit=COMMIT, build_id="", image_file=None, artifacts_file=str(artifacts_file), skip_reason_file=None
        )
        with mock.patch.object(rg, "wait_for_rollbacks"):
            self.assertEqual(rg.cmd_check_prod(args, stores), rg.EXIT_GO)
            artifacts_file.write_text(json.dumps({**verified, "demo-a": f"demo-a-9@{DIGEST_C}"}))
            self.assertEqual(rg.cmd_check_prod(args, stores), rg.EXIT_ERROR)

    def test_check_prod_takes_one_of_image_or_artifacts(self) -> None:
        """check-prod needs --image-file (GKE) or --artifacts-file (light kinds), not both."""
        for extra in ([], ["--image-file", "a", "--artifacts-file", "b"]):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                rg.main(["check-prod", "--commit", COMMIT, *extra])

    def test_gate_refuses_a_check_only_light_service(self) -> None:
        """release_gate.py has nothing to gate for a check_only light service."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "demo-cloud-run.json").write_text(json.dumps(light_data("cloud_run")))
            with mock.patch.object(rc, "SERVICES_DIR", Path(tmp)), mock.patch("sys.stderr"):
                self.assertEqual(rg.main(["status"]), rg.EXIT_ERROR)

    def test_preflight_and_still_current(self) -> None:
        """Preflight writes the live artifacts of the verified revisions; still-current refuses a change."""
        verified = {"demo-a": f"demo-a-2@{DIGEST_B}", "demo-b": f"demo-b-2@{DIGEST_B}"}
        args = _args(out_dir=self.tmp.name)
        with (
            mock.patch.object(rl.guard, "Guard") as fake,
            mock.patch.object(rl, "_verified_artifacts", return_value=verified),
        ):
            fake.return_value.check_superseded.return_value = []
            self.assertEqual(rl.cmd_preflight(args, RUN, self.prod, self.gcloud), 0)
            written = json.loads((self.root / rl.ARTIFACTS_FILE).read_text())
            self.assertEqual(written, verified)
            fake.return_value.wait_for_dev.assert_called_once()
            self.assertTrue(fake.return_value.wait_for_dev.call_args.kwargs["allow_overlap"])
            self.assertEqual(fake.call_args.kwargs["rollback_tag"], "demo-run-rollback")
            self.assertEqual(rl.cmd_still_current(args, RUN, self.prod, self.gcloud), 0)
            self.gcloud.not_ready.add("demo-b-2")
            with self.assertRaises(rr.RecordError):
                rl.cmd_still_current(args, RUN, self.prod, self.gcloud)
            fake.return_value.check_superseded.side_effect = guard.Superseded("newer")
            self.assertEqual(rl.cmd_still_current(args, RUN, self.prod, self.gcloud), guard.EXIT_SUPERSEDED)
            fake.return_value.wait_for_turn.side_effect = guard.Superseded("newer")
            self.assertEqual(rl.cmd_preflight(args, RUN, self.prod, self.gcloud), guard.EXIT_SUPERSEDED)

    def test_promote_record_and_check(self) -> None:
        """The promote moves 100% to the verified revisions; the record keeps what served before."""
        _save(
            self.prod,
            _record(release_id="20261020T100000Z-bbbbbbbbb", commit=PREVIOUS, artifacts={"demo-a": "demo-a-1@x"}),
        )
        artifacts = {"demo-a": f"demo-a-2@{DIGEST_B}", "demo-b": f"demo-b-2@{DIGEST_B}"}
        artifacts_file = self.root / "artifacts.json"
        artifacts_file.write_text(json.dumps(artifacts))
        id_file = self.root / "release_id"
        begin = _args(artifacts_file=str(artifacts_file), release_id_file=str(id_file))
        self.assertEqual(rl.cmd_begin(begin, RUN, self.prod, self.gcloud), 0)
        release_id = id_file.read_text().strip()
        record = rr.read_record(self.prod, release_id)
        self.assertEqual(record["artifacts"], artifacts)
        self.assertEqual(record["previous"]["artifacts"], {"demo-a": "demo-a-1@x"})
        self.assertEqual(rl.serving_split(record["previous"]["workloads"]["cloud_run/demo-a"]), {"demo-a-1": 100})
        self.assertEqual(rl.cmd_apply(_args(artifacts_file=str(artifacts_file)), RUN, self.prod, self.gcloud), 0)
        moves = [command for command in self.gcloud.commands if command[3] == "update-traffic"]
        self.assertEqual(
            [command[-1] for command in moves], ["--to-revisions=demo-a-2=100", "--to-revisions=demo-b-2=100"]
        )
        rr.acquire_lock(self.prod, "b1", "deploy", lambda _: True, wait_seconds=1)
        self.assertEqual(rl.cmd_finish(_args(release_id=release_id), RUN, self.prod, self.gcloud), 0)
        self.assertEqual(self.prod.read(rr.LOCK_OBJECT)[0]["holder"], "")
        saved = rr.read_record(self.prod, release_id)
        self.assertEqual(rl.check_promoted_revisions(RUN, saved, saved["workloads"]).status, "passed")
        self.gcloud.traffic["demo-b"] = {"demo-b-1": 50, "demo-b-2": 50}
        after = rl.snapshot_cloud_run(RUN, self.gcloud)
        failed = rl.check_promoted_revisions(RUN, saved, after)
        self.assertEqual(failed.status, "failed")
        self.assertIn("demo-b-1=50,demo-b-2=50", failed.details[1])
        self.gcloud.fail = "update-traffic demo-a"
        self.assertEqual(
            rl.cmd_apply(_args(artifacts_file=str(artifacts_file)), RUN, self.prod, self.gcloud), rv.EXIT_ERROR
        )

    def test_composer_promote_syncs_the_candidate(self) -> None:
        """The composer promote syncs the verified candidate folder to dags/; the record needs artifacts."""
        uri = f"gs://demo-bucket/data/release-candidate/{COMMIT}/dags"
        with mock.patch.object(rl, "folder_manifest", return_value=(DIGEST_A, 3)):
            self.assertEqual(rl.promote_artifacts(FLOW, {"dags": f"{uri} {DIGEST_A}"}, self.gcloud), [])
        self.assertEqual(self.gcloud.commands[-1], ["gsutil", "-m", "rsync", "-r", "-d", uri, "gs://demo-bucket/dags"])
        self.assertEqual(len(rl.promote_artifacts(FLOW, {}, self.gcloud)), 1)
        with self.assertRaises(rr.RecordError):
            rl.cmd_begin(_args(release_id_file=str(self.root / "id")), FLOW, self.prod, self.gcloud)

    def test_composer_promote_refuses_mutated_source_and_destination(self) -> None:
        """A mutation after still-current or during rsync must fail the release."""
        artifact = {"dags": f"gs://demo-bucket/data/release-candidate/{COMMIT}/dags {DIGEST_A}"}
        with mock.patch.object(rl, "folder_manifest", return_value=(DIGEST_B, 3)):
            self.assertIn("changed after verification", rl.promote_artifacts(FLOW, artifact, self.gcloud)[0])
        self.assertFalse(any(command[0] == "gsutil" for command in self.gcloud.commands))
        with mock.patch.object(rl, "folder_manifest", side_effect=[(DIGEST_A, 3), (DIGEST_B, 3)]):
            self.assertIn("live DAG manifest differs", rl.promote_artifacts(FLOW, artifact, self.gcloud)[0])

    def test_stage_and_fetch(self) -> None:
        """stage copies the repository's dags/ to the candidate folder; fetch-dags copies both folders down."""
        source = self.root / "dags"
        source.mkdir()
        self.assertEqual(rl.cmd_stage(_args(source=str(source)), FLOW, self.prod, self.gcloud), 0)
        uri = f"gs://demo-bucket/data/release-candidate/{COMMIT}/dags"
        self.assertEqual(self.gcloud.commands[-1][-2:], [str(source), uri])
        out = self.root / "parse"
        self.assertEqual(rl.cmd_fetch_dags(_args(out_dir=str(out)), FLOW, self.prod, self.gcloud), 0)
        self.assertEqual(
            [command[-1] for command in self.gcloud.commands[-2:]], [str(out / "candidate"), str(out / "baseline")]
        )
        self.assertEqual(oct(out.stat().st_mode & 0o777), "0o777")
        self.assertEqual((out / "fetch-complete").read_text().strip(), COMMIT)
        with self.assertRaises(rc.ConfigError):
            rl.cmd_stage(_args(source=str(source)), RUN, self.prod, self.gcloud)
        # Without the environment's DAG folder, the config's bucket.
        self.gcloud.fail = "composer environments describe"
        self.assertEqual(rl.dags_uri(FLOW, self.gcloud), "gs://demo-bucket/dags")

    def test_failed_fetch_invalidates_a_previous_parse_and_completion_marker(self) -> None:
        """Either failed rsync leaves no marker or reusable parse, even after a previous successful fetch."""
        out = self.root / "parse"
        out.mkdir()
        for failed_source in ("data/release-candidate/", "gs://demo-bucket/dags"):
            (out / "fetch-complete").write_text(COMMIT)
            (out / "result.json").write_text("{}")
            self.gcloud.fail = "rsync -r -d " + (
                f"gs://demo-bucket/data/release-candidate/{COMMIT}/dags"
                if "candidate" in failed_source
                else failed_source
            )
            with self.subTest(source=failed_source), self.assertRaises(rr.RecordError):
                rl.cmd_fetch_dags(_args(out_dir=str(out)), FLOW, self.prod, self.gcloud)
            self.assertFalse((out / "fetch-complete").exists())
            self.assertFalse((out / "result.json").exists())

    def test_failed_full_release_starts_the_rollback(self) -> None:
        """Tier full: a failed deploy check posts that a rollback follows; the window build starts it."""
        record = _record(artifacts={"demo-a": "demo-a-2@x", "demo-b": "demo-b-2@x"})
        _save(self.prod, record)
        failed = rv.Check("revisions", "failed", ["2 services", "cloud_run/demo-a: traffic is demo-a-1=100"])
        with (
            mock.patch.object(rl, "deploy_checks", return_value=([failed], {})),
            mock.patch.object(rv, "suspected_prs", return_value=[]),
            mock.patch.object(rl, "alert") as alert,
            mock.patch.object(rv, "start_rollback") as start,
        ):
            self.assertEqual(
                rl.cmd_check(_args(release_id=record["release_id"]), RUN, self.prod, self.gcloud), rv.EXIT_FAILED
            )
            alert.assert_not_called()
            self.assertIn("An automatic rollback follows", rr.post_slack.call_args[0][0])
            saved = rr.read_record(self.prod, record["release_id"])
            self.assertIn("release_rollback.py run", saved["rollback_suggestion"]["how"][0])
            self.assertEqual(saved["rollback_suggestion"]["target"]["release_id"], record["previous"]["release_id"])
            rl.cmd_watch(_args(release_id=record["release_id"], checkpoints="15"), RUN, self.prod, self.gcloud)
            start.assert_called_once()
        check_only = rl.parse_light(light_data("cloud_run"))
        with mock.patch.object(rl, "alert") as alert, mock.patch.object(rv, "start_rollback") as start:
            rl.on_failed(check_only, self.prod, record)
            alert.assert_called_once()
            start.assert_not_called()

    def test_auto_rollback_triggers_the_light_rollback(self) -> None:
        """release_verify.start_rollback works for a light service: its trigger, branch and _EXPECT_LATEST."""
        record = _record(verdict={"status": "failed", "phase": "deploy"}, rollback_suggestion={"summary": "x"})
        _save(self.prod, record)
        commands = []
        with (
            mock.patch.dict(os.environ, {"RELEASE_AUTO_ROLLBACK": "1"}),
            mock.patch.object(rr, "build_alive", return_value=lambda _: False),
        ):
            build = rv.start_rollback(
                RUN,
                self.prod,
                record,
                runner=lambda c, timeout=120: commands.append(c) or "rb1\n",
                builds=lambda *_: (False, []),
                follow=False,
            )
        self.assertEqual(build, "rb1")
        self.assertIn("demo-run-rollback", commands[0])
        self.assertIn(f"_EXPECT_LATEST={record['release_id']}", commands[0][-2])

    def test_window_builds_and_schedule(self) -> None:
        """A full-tier window waits only for prod builds past their first change and for rollbacks."""
        waiting = guard.Build.from_api(
            {
                "id": "w",
                "status": "WORKING",
                "createTime": "2026-10-21T10:00:00Z",
                "steps": [{"id": "release-record-begin", "status": "QUEUED"}],
            }
        )
        changing = guard.Build.from_api(
            {
                "id": "c",
                "status": "WORKING",
                "createTime": "2026-10-21T10:00:00Z",
                "steps": [{"id": "release-record-begin", "status": "SUCCESS"}],
            }
        )
        rollback = guard.Build.from_api({"id": "r", "status": "WORKING", "createTime": "2026-10-21T10:00:00Z"})
        api = rl.GoogleApi.return_value
        api.list_builds.return_value = [waiting, changing]
        api.ongoing_rollbacks.return_value = [rollback]
        self.assertEqual([item.split(" ")[0] for item in rl.running_deploys(RUN, "own")], ["c", "r"])
        light = rl.parse_light(light_data("cloud_run"))
        self.assertEqual(len(rl.running_deploys(light, "own")), 2)
        _save(self.prod, _record())
        commands = []
        with mock.patch.object(rr, "run_command", side_effect=lambda c, timeout=0: commands.append(c) or "wb1\n"):
            args = _args(release_id=_record()["release_id"], source_dir=str(self.root / "src"), no_slack=False)
            rl.cmd_schedule_window(args, RUN, self.prod, self.gcloud)
            args.no_slack = True
            rl.cmd_schedule_window(args, RUN, self.prod, self.gcloud)
            rl.cmd_schedule_window(args, light, self.prod, self.gcloud)
        subs = [next(part for part in command if part.startswith("--substitutions=")) for command in commands]
        self.assertTrue(subs[0].endswith(",_AUTO_ROLLBACK=1"))
        self.assertTrue(subs[1].endswith(",_AUTO_ROLLBACK="))
        self.assertNotIn("_AUTO_ROLLBACK", subs[2])


class LightFullRollbackTest(unittest.TestCase):
    def setUp(self) -> None:
        """A prod store with a promoted release; no network."""
        self.tmp = tempfile.TemporaryDirectory()
        self.store = rr.LocalStore(Path(self.tmp.name))
        self.gcloud = FakeGcloud()
        self.gcloud.traffic = {"demo-a": {"demo-a-2": 100}, "demo-b": {"demo-b-2": 100}}
        before = {
            "cloud_run/demo-a": {"traffic": {"demo-a-1": 100, "demo-a-2": 0}},
            "cloud_run/demo-b": {"traffic": {"demo-b-1": 100}},
        }
        self.record = _record(
            artifacts={"demo-a": f"demo-a-2@{DIGEST_B}", "demo-b": f"demo-b-2@{DIGEST_B}"},
            previous={"release_id": "20261020T100000Z-bbbbbbbbb", "commit": PREVIOUS, "workloads": before},
        )
        _save(self.store, self.record)
        self.cleanup_tags = mock.Mock(return_value=[])
        self.patches = [
            mock.patch.object(rl, "remove_candidate_tags", self.cleanup_tags),
            mock.patch.object(rb, "assert_alone"),
            mock.patch.object(rr, "build_info", return_value={"id": "rb1"}),
            mock.patch.object(rr, "post_slack", return_value=True),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        """Remove what the test created."""
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def _apply_args(self, **values) -> argparse.Namespace:
        """The arguments of release_rollback.py apply in the rollback build."""
        defaults = dict(
            dry_run=False,
            reason="release-verify failed",
            target="previous",
            build_id="rb1",
            expect_latest="",
            requested_by="auto",
        )
        defaults.update(values)
        return argparse.Namespace(**defaults)

    def test_plan(self) -> None:
        """The plan sets each service's traffic back to what served before; a missing revision blocks it."""
        latest, target = rb.resolve_target(self.store, "previous")
        plan = rl.rollback_plan(RUN, latest, target, self.gcloud)
        self.assertEqual(
            [(step.current, step.target, step.action) for step in plan.steps],
            [
                ("demo-a-2=100", "demo-a-1=100", "set"),
                ("demo-b-2=100", "demo-b-1=100", "set"),
            ],
        )
        self.assertEqual(plan.problems, [])
        del self.gcloud.revisions["demo-b-1"]
        self.assertIn("cannot be read", rl.rollback_plan(RUN, latest, target, self.gcloud).problems[0])
        with mock.patch("builtins.print"):
            args = argparse.Namespace(to="previous", json=None)
            self.assertEqual(rl.cmd_rollback_plan(args, RUN, self.store, self.gcloud), 1)

    def test_check_only_record_is_a_target(self) -> None:
        """The first rollback after the move to tier full finds what served before in previous_snapshot."""
        with tempfile.TemporaryDirectory() as tmp:
            store = rr.LocalStore(Path(tmp))
            snapshot = {
                "cloud_run/demo-a": {"traffic": {"demo-a-1": 100}},
                "cloud_run/demo-b": {"traffic": {"demo-b-1": 100}},
            }
            _save(store, _record(previous={"release_id": None, "commit": PREVIOUS}, previous_snapshot=snapshot))
            latest, target = rl.resolve_target(store, "previous")
            self.assertEqual(target["workloads"], snapshot)
            plan = rl.rollback_plan(RUN, latest, target, self.gcloud)
            self.assertEqual(plan.problems, [])
            self.assertEqual([step.target for step in plan.steps], ["demo-a-1=100", "demo-b-1=100"])

    def test_apply_records_and_holds(self) -> None:
        """The apply moves the traffic, writes a rollback record with the hold and moves latest.json."""
        with mock.patch("builtins.print"):
            code = rl.cmd_rollback_apply(
                self._apply_args(expect_latest=self.record["release_id"]),
                RUN,
                self.store,
                self.gcloud,
                alive=lambda _: False,
            )
        self.assertEqual(code, 0)
        self.cleanup_tags.assert_called_once_with(RUN, {"demo-a": "demo-a-2", "demo-b": "demo-b-2"})
        self.assertEqual(self.gcloud.traffic, {"demo-a": {"demo-a-1": 100}, "demo-b": {"demo-b-1": 100}})
        latest = rr.read_record(self.store, "latest")
        self.assertEqual((latest["kind"], latest["status"], latest["commit"]), ("rollback", "deployed", PREVIOUS))
        self.assertEqual(latest["promote_hold"]["rolled_back_release"], self.record["release_id"])
        self.assertRegex(latest["promote_hold"]["resume"], r"release_gate.py( --service demo-run)? resume")
        self.assertEqual(rr.read_record(self.store, self.record["release_id"])["status"], "rolled_back")
        self.assertEqual(self.store.read(rr.LOCK_OBJECT)[0]["holder"], "")
        # The gate holds the next promote.
        stores = rg.Stores(service=RUN, prod=self.store, dev=None)
        with (
            mock.patch.object(rg, "contains", return_value=True),
            mock.patch.object(rg, "commit_messages", return_value=[]),
        ):
            self.assertEqual(rg._hold(stores, "d" * 40, "t", self.store.read("latest.json")[0]).status, "held")

    def test_failed_tag_cleanup_does_not_mark_rollback_successful(self) -> None:
        """Restored traffic alone cannot declare success while the failed candidate remains public."""
        self.cleanup_tags.return_value = ["demo-a: failed candidate tag remains"]
        with mock.patch("builtins.print"):
            self.assertEqual(
                rl.cmd_rollback_apply(self._apply_args(), RUN, self.store, self.gcloud, alive=lambda _: False), 1
            )
        latest = rr.read_record(self.store, "latest")
        self.assertEqual(latest["kind"], "rollback")
        self.assertEqual(latest["status"], "deployed")
        self.assertEqual(latest["verdict"]["status"], "failed")
        self.assertEqual(latest["promote_hold"]["status"], "held")
        self.assertEqual(rr.read_record(self.store, self.record["release_id"])["rolled_back_by"], latest["release_id"])
        stores = rg.Stores(service=RUN, prod=self.store, dev=None)
        with (
            mock.patch.object(rg, "contains", return_value=True),
            mock.patch.object(rg, "commit_messages", return_value=[]),
        ):
            self.assertEqual(rg._hold(stores, "d" * 40, "t", self.store.read("latest.json")[0]).status, "held")

    def test_apply_refuses_when_prod_moved_on_and_reports_a_failed_move(self) -> None:
        """_EXPECT_LATEST refuses a stale rollback; a traffic move that fails leaves latest.json as it was."""
        with self.assertRaises(rr.RecordError), mock.patch("builtins.print"):
            rl.cmd_rollback_apply(
                self._apply_args(expect_latest="other"), RUN, self.store, self.gcloud, alive=lambda _: False
            )
        self.gcloud.fail = "update-traffic demo-b"
        with mock.patch("builtins.print"), mock.patch("sys.stderr"):
            self.assertEqual(
                rl.cmd_rollback_apply(self._apply_args(), RUN, self.store, self.gcloud, alive=lambda _: False), 1
            )
        self.assertEqual(rr.read_record(self.store, "latest")["release_id"], self.record["release_id"])
        with mock.patch("builtins.print"):
            self.assertEqual(rl.cmd_rollback_apply(self._apply_args(dry_run=True), RUN, self.store, self.gcloud), 0)

    def test_composer_rollback_stages_a_missing_folder(self) -> None:
        """A composer rollback syncs the target commit's folder; one from before the candidate folders comes from GitHub."""
        latest, target = rb.resolve_target(self.store, "previous")
        plan = rl.rollback_plan(FLOW, latest, target, self.gcloud)
        self.assertEqual([(step.current, step.target) for step in plan.steps], [(COMMIT, PREVIOUS)])
        uri = f"gs://demo-bucket/data/release-candidate/{PREVIOUS}/dags"
        with (
            mock.patch.object(rl, "GoogleApi"),
            mock.patch.object(rl, "folder_manifest", return_value=(DIGEST_A, 3)),
            mock.patch.object(rl, "stage_from_github", return_value=DIGEST_A) as stage,
        ):
            self.assertEqual(rl.apply_rollback(FLOW, plan, self.gcloud), [])
        stage.assert_called_once_with(FLOW, PREVIOUS, uri, self.gcloud)
        self.assertEqual(self.gcloud.commands[-1][-2:], [uri, "gs://demo-bucket/dags"])
        no_target = rl.rollback_plan(FLOW, latest, {"commit": None}, self.gcloud)
        self.assertIn("unknown", no_target.problems[0])

    def test_composer_rollback_preserves_and_checks_recorded_artifacts(self) -> None:
        """Corrupted target files cannot pass the rollback or disappear from its record."""
        uri = f"gs://demo-bucket/data/release-candidate/{PREVIOUS}/dags"
        artifact = {"dags": f"{uri} {DIGEST_A}"}
        latest, target = rb.resolve_target(self.store, "previous")
        target["artifacts"] = artifact
        plan = rl.rollback_plan(FLOW, latest, target, self.gcloud)
        self.assertEqual(plan.target_artifacts, artifact)
        with (
            mock.patch.object(rl, "GoogleApi"),
            mock.patch.object(rl, "folder_manifest", return_value=(DIGEST_B, 2)),
            mock.patch.object(rl, "stage_from_github", return_value=DIGEST_B) as stage,
        ):
            failures = rl.apply_rollback(FLOW, plan, self.gcloud)
        self.assertIn("does not match", failures[0])
        stage.assert_called_once_with(FLOW, PREVIOUS, uri, self.gcloud)
        self.assertFalse(any(command[0] == "gsutil" for command in self.gcloud.commands))
        check = rl.check_rollback(FLOW, plan, {"composer/dags": {"manifest": DIGEST_B}})
        self.assertEqual(check.status, "failed")
        self.assertEqual(rl.check_rollback(FLOW, plan, {"composer/dags": {"manifest": DIGEST_A}}).status, "passed")

    def test_resolved_previous_retains_its_artifact_identity(self) -> None:
        """The resolver and rollback records carry the exact target artifact across another rollback."""
        artifact = {"dags": f"gs://demo-bucket/data/release-candidate/{PREVIOUS}/dags {DIGEST_A}"}
        rr.update_record(
            self.store,
            self.record["release_id"],
            lambda current: current["previous"].update(artifacts=artifact),
            "test",
            {},
        )
        latest, target = rb.resolve_target(self.store, "previous")
        self.assertEqual(target["artifacts"], artifact)
        plan = rl.rollback_plan(FLOW, latest, target, self.gcloud)
        record = rb.create_rollback_record(FLOW, self.store, self._apply_args(), latest, target, ({}, {}), plan)
        self.assertEqual(record["artifacts"], artifact)

    def test_stage_from_github_takes_only_dag_files(self) -> None:
        """The tarball gives only regular files under dags/: no links, no paths out of the folder."""
        import io
        import tarfile

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for name, data in (
                ("o-r-sha/dags/a.py", b"A"),
                ("o-r-sha/dags/sub/b.py", b"B"),
                ("o-r-sha/src/x.ts", b"X"),
                ("o-r-sha/dags/../../evil.py", b"E"),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            link = tarfile.TarInfo("o-r-sha/dags/link.py")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = buffer.getvalue()
        seen = {}

        def runner(command, timeout=120):
            """Look at the folder that rsync would upload."""
            folder = Path(command[-2])
            seen["files"] = sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file())
            return ""

        with (
            mock.patch.object(rr, "github_token", return_value="t"),
            mock.patch.object(rl.urllib.request, "urlopen", return_value=response),
            mock.patch("builtins.print"),
        ):
            rl.stage_from_github(FLOW, PREVIOUS, "gs://demo-bucket/data/release-candidate/x/dags", runner)
        self.assertEqual(seen["files"], ["a.py", "sub/b.py"])

    def test_release_rollback_main_dispatches_light_services(self) -> None:
        """release_rollback.py plan, run and apply of a light config go to release_light; check_only is refused."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "demo-run.json").write_text(json.dumps(full_run_data()))
            (folder / "demo-cloud-run.json").write_text(json.dumps(light_data("cloud_run")))
            with (
                mock.patch.object(rc, "SERVICES_DIR", folder),
                mock.patch.object(rl, "open_store", return_value=self.store),
                mock.patch.object(rl, "cmd_rollback_plan", return_value=0) as plan,
                mock.patch.object(rl, "cmd_rollback_apply", return_value=0) as apply,
                mock.patch.object(rb, "cmd_run", return_value=0) as run,
            ):
                self.assertEqual(rb.main(["plan", "--service", "demo-run"]), 0)
                self.assertEqual(rb.main(["apply", "--service", "demo-run", "--build-id", "x"]), 0)
                self.assertEqual(rb.main(["run", "--service", "demo-run", "--reason", "why"]), 0)
                with mock.patch("sys.stderr"):
                    self.assertEqual(rb.main(["plan", "--service", "demo-cloud-run"]), 1)
            self.assertEqual(plan.call_args[0][1].name, "demo-run")
            self.assertEqual(apply.call_count, 1)
            self.assertEqual(run.call_args[0][1].rollback_trigger, "demo-run-rollback")

    def test_cmd_run_starts_the_light_rollback_trigger(self) -> None:
        """release_rollback.py run starts <service>-rollback at the release branch."""
        commands = []
        with (
            mock.patch.object(rr, "run_command", side_effect=lambda c, timeout=0: commands.append(c) or "rb9\n"),
            mock.patch("builtins.print"),
        ):
            args = argparse.Namespace(reason="bad", requested_by="n", to="previous", dry_run=False, wait=False)
            self.assertEqual(rb.cmd_run(args, RUN, self.store, None), 0)
        self.assertEqual(commands[0][:5], ["gcloud", "builds", "triggers", "run", "demo-run-rollback"])
        self.assertIn("--branch=main", commands[0])


class GuardOverlapTest(unittest.TestCase):
    def test_wait_for_dev_allows_overlap_for_light_kinds(self) -> None:
        """Two overlapping dev builds of a commit block a GKE promote (the tag) but not a light one."""
        older = _build(
            "d1", "2026-10-21T10:00:00Z", start="2026-10-21T10:00:01Z", finish="2026-10-21T10:09:00Z", commit=COMMIT
        )
        newer = _build(
            "d2", "2026-10-21T10:01:00Z", start="2026-10-21T10:01:01Z", finish="2026-10-21T10:08:00Z", commit=COMMIT
        )
        api = mock.Mock()
        api.commit_builds.return_value = [older, newer]
        api.trigger_builds_around.return_value = []
        turn = guard.Guard(api=api, build_id="p1", commit=COMMIT)
        own = _build("p1", "2026-10-21T10:10:00Z", commit=COMMIT)
        with self.assertRaises(guard.GuardError):
            turn.wait_for_dev(own, ("demo-dev",), 1, "main")
        self.assertEqual(turn.wait_for_dev(own, ("demo-dev",), 1, "main", allow_overlap=True).id, "d2")


# --------------------------------------------------------------------------- control by version, environmental signals

VERSIONED_ERRORS = "sum:errors{x} by {resource_name,version}.as_count()"
VERSIONED_HITS = "sum:hits{x} by {resource_name,version}.as_count()"


def _versioned_service() -> rc.Service:
    """The demo service with a versioned HTTP error rate."""
    data = service_data()
    data["verify"]["metrics"][0]["versioned"] = True
    return rc.parse_service(data)


class VersionTest(unittest.TestCase):
    def test_versioned_query(self) -> None:
        """version becomes the last group tag."""
        self.assertEqual(rv.versioned_query(ERRORS), VERSIONED_ERRORS)
        self.assertEqual(rv.versioned_query("sum:hits{x}.as_count()"), "sum:hits{x} by {version}.as_count()")
        self.assertEqual(rv.versioned_query("sum:hits{x}"), "sum:hits{x} by {version}")
        self.assertTrue(rv.is_release_version(COMMIT[:9], COMMIT))
        self.assertFalse(rv.is_release_version("abc", "abcdef0"))
        data = service_data()
        data["verify"]["metrics"][1]["versioned"] = True
        with self.assertRaises(rc.ConfigError):
            rc.parse_service(data)

    def _findings(self, versions: dict[str, tuple[float, float]]) -> list:
        """The judged findings when GET /b serves these (errors, hits) per version in the window."""
        base = {ERRORS: {"GET /b": 1}, HITS: {"GET /b": 1000}}
        cand = {
            ERRORS: {"GET /b": sum(e for e, _ in versions.values())},
            HITS: {"GET /b": sum(n for _, n in versions.values())},
            VERSIONED_ERRORS: {f"GET /b|{v}": e for v, (e, _) in versions.items()},
            VERSIONED_HITS: {f"GET /b|{v}": n for v, (_, n) in versions.items()},
        }
        with mock.patch.object(rv, "runtime_change", return_value=(True, "src")):
            findings = rv.judge_metrics(
                _versioned_service(), FakeDatadog(base, cand), _record(), BASELINE, WINDOW, None
            )
        return [f for f in findings if f.metric == "http_error_rate"]

    def test_other_versions_fail_too(self) -> None:
        """The previous version runs as bad in the same window: environmental, no rollback."""
        [finding] = self._findings({COMMIT[:9]: (150, 500), PREVIOUS[:9]: (150, 500)})
        self.assertEqual(finding.status, "environmental")
        self.assertIn("not the release", finding.limit)

    def test_only_the_release_fails(self) -> None:
        """The other versions stay at the baseline: the release's regression stands."""
        [finding] = self._findings({COMMIT[:9]: (290, 500), PREVIOUS[:9]: (1, 500)})
        self.assertEqual(finding.status, "exceeded")
        self.assertIn("other versions", finding.limit)

    def test_each_control_must_support_the_environmental_verdict(self) -> None:
        """A healthy or undersampled control contradicts even a heavily sampled failing control."""
        for other in ((1, 1000), (3, 10)):
            with self.subTest(other=other):
                [finding] = self._findings({COMMIT[:9]: (300, 1000), PREVIOUS[:9]: other, "c" * 9: (6000, 10000)})
                self.assertEqual(finding.status, "exceeded")

    def test_multiple_supported_controls_allow_environmental_verdict(self) -> None:
        """Every independently sampled control and the release show the same failure."""
        [finding] = self._findings({COMMIT[:9]: (300, 1000), PREVIOUS[:9]: (300, 1000), "c" * 9: (400, 1000)})
        self.assertEqual(finding.status, "environmental")

    def test_unjudged_release_is_no_control(self) -> None:
        """An earlier unjudged release judged with this one counts as the release, never as a control."""
        base = {ERRORS: {"GET /b": 1}, HITS: {"GET /b": 1000}}
        cand = {
            ERRORS: {"GET /b": 300},
            HITS: {"GET /b": 1000},
            VERSIONED_ERRORS: {f"GET /b|{COMMIT[:9]}": 150, f"GET /b|{PREVIOUS[:9]}": 150},
            VERSIONED_HITS: {f"GET /b|{COMMIT[:9]}": 500, f"GET /b|{PREVIOUS[:9]}": 500},
        }
        with mock.patch.object(rv, "runtime_change", return_value=(True, "src")):
            findings = rv.judge_metrics(
                _versioned_service(),
                FakeDatadog(base, cand),
                _record(),
                BASELINE,
                WINDOW,
                None,
                judged_commits=(PREVIOUS,),
            )
        [finding] = [f for f in findings if f.metric == "http_error_rate"]
        self.assertEqual(finding.status, "exceeded")
        self.assertIn("no other version served", finding.limit)

    def test_healthy_earlier_release_hides_nothing(self) -> None:
        """A healthy earlier unjudged version does not dilute the newest one: each is judged on its own."""
        base = {ERRORS: {"GET /b": 1}, HITS: {"GET /b": 1000}}
        older = "c" * 40
        versions = {COMMIT[:9]: (150, 500), PREVIOUS[:9]: (5, 500), older[:9]: (50, 500)}
        cand = {
            ERRORS: {"GET /b": 205},
            HITS: {"GET /b": 1500},
            VERSIONED_ERRORS: {f"GET /b|{v}": e for v, (e, _) in versions.items()},
            VERSIONED_HITS: {f"GET /b|{v}": n for v, (_, n) in versions.items()},
        }
        with mock.patch.object(rv, "runtime_change", return_value=(True, "src")):
            findings = rv.judge_metrics(
                _versioned_service(),
                FakeDatadog(base, cand),
                _record(),
                BASELINE,
                WINDOW,
                None,
                judged_commits=(PREVIOUS,),
            )
        [finding] = [f for f in findings if f.metric == "http_error_rate"]
        self.assertEqual(finding.status, "exceeded")

    def test_no_other_version(self) -> None:
        """No other version served: the plain limit decides."""
        [finding] = self._findings({COMMIT[:9]: (300, 1000)})
        self.assertEqual(finding.status, "exceeded")
        self.assertIn("no other version served", finding.limit)

    def test_an_unjudged_earlier_version_cannot_clear_the_regression(self) -> None:
        """Insufficient samples are not positive evidence that an earlier release was healthy."""
        older = "c" * 40
        versions = {COMMIT[:9]: (50, 1000), PREVIOUS[:9]: (10, 10), older[:9]: (50, 1000)}
        cand = {
            ERRORS: {"GET /b": 110},
            HITS: {"GET /b": 2010},
            VERSIONED_ERRORS: {f"GET /b|{v}": e for v, (e, _) in versions.items()},
            VERSIONED_HITS: {f"GET /b|{v}": n for v, (_, n) in versions.items()},
        }
        with mock.patch.object(rv, "runtime_change", return_value=(True, "src")):
            findings = rv.judge_metrics(
                _versioned_service(),
                FakeDatadog({ERRORS: {"GET /b": 1}, HITS: {"GET /b": 1000}}, cand),
                _record(),
                BASELINE,
                WINDOW,
                None,
                judged_commits=(PREVIOUS,),
            )
        self.assertEqual([f.status for f in findings if f.metric == "http_error_rate"], ["exceeded"])

    def test_manifest_comparison_must_complete(self) -> None:
        """Missing credentials, failed reads and truncated comparisons are unknown, not clean."""
        latest, target = _record(), {"commit": PREVIOUS, "images": {"app": f"{REPO}@{OLD}"}}
        for commit, known in (("unknown", False), ("abc", False), (COMMIT, True)):
            with self.subTest(commit=commit):
                self.assertEqual(
                    rb.build_plan(
                        SERVICE, {**latest, "commit": commit}, {**target, "commit": commit}, {}, None
                    ).settings_compared,
                    known,
                )
        self.assertFalse(rb.build_plan(SERVICE, latest, target, {}, None).settings_compared)
        with mock.patch.object(rr, "github_json", side_effect=guard.GuardError("unavailable")):
            self.assertFalse(rb.build_plan(SERVICE, latest, target, {}, "token").settings_compared)
        for payload, expected in (
            ({"files": []}, True),
            ({}, False),
            ({"files": [{"filename": "src/x.py"}] * 300}, False),
        ):
            with mock.patch.object(rr, "github_json", return_value=payload):
                self.assertEqual(rb.build_plan(SERVICE, latest, target, {}, "token").settings_compared, expected)

    def test_unknown_retained_settings_keep_the_hold(self) -> None:
        """An older rollback record without positive comparison evidence cannot clear a release."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rr, "post_slack", return_value=True):
            store = _store(tmp)
            record = self._rolled_back(store)
            rr.update_record(
                store, "20261021T110000Z-bbbbbbbbb", lambda current: current.update(rollback={}), "test", {}
            )
            outcome = rv.watch_rollback(
                SERVICE, store, FakeDatadog({}, {}), record, sleep=lambda _: None, prod_busy=lambda: []
            )
            self.assertIn("did not verify", outcome)
            self.assertEqual(rv.environmental_signals(store), {})
            self.assertEqual(rr.read_record(store, "20261021T110000Z-bbbbbbbbb")["promote_hold"]["status"], "holding")

    def test_marked_and_recovered(self) -> None:
        """A marked signal is a quiet warning; back within its old limit, it is recovered."""
        metric = SERVICE.metrics[0]
        marks = {
            "http_error_rate demo GET /b": {
                "baseline": 0.001,
                "baseline_samples": 1000,
                "since": "2026-10-21T09:00:00Z",
                "rollback_id": "rb",
            }
        }
        failing = rv.judge(metric, "demo GET /b", (0.001, 1000), (0.3, 1000))
        rv.apply_environmental([failing], marks)
        self.assertEqual(failing.status, "environmental_marked")
        self.assertEqual(rv.recovered_signals(SERVICE, [failing], marks), [])
        back = rv.judge(metric, "demo GET /b", (0.001, 1000), (0.001, 1000))
        self.assertEqual(rv.recovered_signals(SERVICE, [back], marks), ["http_error_rate demo GET /b"])

    def _rolled_back(self, store: rr.LocalStore) -> dict:
        """A failed release on GET /b, rolled back by a rollback record that is now latest."""
        regression = {
            "kind": "metric",
            "metric": "http_error_rate",
            "scope": "demo GET /b",
            "baseline": 0.001,
            "baseline_samples": 1000,
        }
        record = _record(
            verdict={"status": "failed", "phase": "window_15m"},
            checks={"window_15m": {"regressions": [regression]}},
            rolled_back_by="20261021T110000Z-bbbbbbbbb",
        )
        _save(store, record)
        rollback = _record(
            release_id="20261021T110000Z-bbbbbbbbb",
            kind="rollback",
            commit=PREVIOUS,
            times={"deploy_started": "2026-10-21T10:58:00Z", "deploy_finished": "2026-10-21T11:00:00Z"},
            promote_hold={"status": "holding", "prs": [12]},
            rollback={"settings_compared": True, "not_rolled_back": []},
        )
        _save(store, rollback)
        return record

    def test_rollback_that_does_not_help(self) -> None:
        """After the rollback the target fails the same limit: the signal is marked and the hold ends."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rr, "post_slack", return_value=True) as slack:
            store = _store(tmp)
            record = self._rolled_back(store)
            dd = FakeDatadog({}, {ERRORS: {"GET /b": 300}, HITS: {"GET /b": 1000}})
            outcome = rv.watch_rollback(
                SERVICE,
                store,
                dd,  # type: ignore[arg-type]
                record,
                sleep=lambda _: None,
                now=lambda: datetime(2026, 10, 21, 11, 20, tzinfo=rr.UTC),
                prod_busy=lambda: [],
            )
            self.assertEqual(outcome, "environmental: http_error_rate demo GET /b")
            marks = rv.environmental_signals(store)
            self.assertEqual(list(marks), ["http_error_rate demo GET /b"])
            self.assertEqual(rr.read_record(store, record["release_id"])["verdict"]["cause"], "environmental")
            hold = rr.read_record(store, "20261021T110000Z-bbbbbbbbb")["promote_hold"]
            self.assertEqual(hold["status"], "released")
            self.assertIn("did not help", slack.call_args_list[-1].args[0])
            self.assertTrue(
                json.loads((Path(tmp) / rv.ENVIRONMENTAL_OBJECT).read_text())["signals"]["http_error_rate demo GET /b"][
                    "alerted"
                ]
            )

    def test_rollback_that_left_settings(self) -> None:
        """A rollback that left deployment changes in place proves nothing: no mark."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rr, "post_slack", return_value=True):
            store = _store(tmp)
            record = self._rolled_back(store)
            rr.update_record(
                store,
                "20261021T110000Z-bbbbbbbbb",
                lambda current: current.update(rollback={"not_rolled_back": ["k8/prod/deployment.yaml"]}),
                "test",
                {},
            )
            dd = FakeDatadog({}, {ERRORS: {"GET /b": 300}, HITS: {"GET /b": 1000}})
            outcome = rv.watch_rollback(SERVICE, store, dd, record, sleep=lambda _: None, prod_busy=lambda: [])  # type: ignore[arg-type]
            self.assertIn("left changes in place", outcome)
            self.assertEqual(rv.environmental_signals(store), {})

    def test_mixed_failure_keeps_the_cause(self) -> None:
        """One signal still fails, another regression recovered: marks, but no release-wide cause, hold stays."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rr, "post_slack", return_value=True) as slack:
            store = _store(tmp)
            record = self._rolled_back(store)

            def two(current: dict) -> None:
                """Add a failed smoke check to the failed checkpoint."""
                current["checks"]["window_15m"]["regressions"].append({"kind": "check", "name": "health"})

            rr.update_record(store, record["release_id"], two, "test", {})
            dd = FakeDatadog({}, {ERRORS: {"GET /b": 300}, HITS: {"GET /b": 1000}})
            rv.watch_rollback(
                SERVICE,
                store,
                dd,  # type: ignore[arg-type]
                record,
                sleep=lambda _: None,
                now=lambda: datetime(2026, 10, 21, 11, 20, tzinfo=rr.UTC),
                prod_busy=lambda: [],
            )
            verdict = rr.read_record(store, record["release_id"])["verdict"]
            self.assertNotIn("cause", verdict)
            self.assertEqual(verdict["environmental_signals"], ["http_error_rate demo GET /b"])
            self.assertEqual(rr.read_record(store, "20261021T110000Z-bbbbbbbbb")["promote_hold"]["status"], "holding")
            self.assertIn("keep the promote hold", slack.call_args_list[-1].args[0])

    def test_rollback_check_skips_spread_and_busy_prod(self) -> None:
        """A spread metric is not judged again, and nothing is judged while a build changes prod."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rr, "post_slack", return_value=True):
            store = _store(tmp)
            record = self._rolled_back(store)
            dd = FakeDatadog({}, {ERRORS: {"GET /b": 300}, HITS: {"GET /b": 1000}})
            when = lambda: datetime(2026, 10, 21, 11, 20, tzinfo=rr.UTC)  # noqa: E731
            busy = rv.watch_rollback(
                SERVICE,
                store,
                dd,
                record,
                sleep=lambda _: None,
                now=when,
                prod_busy=lambda: ["deploy build d1"],  # type: ignore[arg-type]
            )
            self.assertEqual(busy, "not judged: deploy build d1 changes prod")

            def spread_only(current: dict) -> None:
                """The checkpoint failed on the p95 only."""
                current["checks"]["window_15m"]["regressions"] = [
                    {
                        "kind": "metric",
                        "metric": "http_p95_seconds",
                        "scope": "demo",
                        "baseline": 1.0,
                        "baseline_samples": 1000,
                    }
                ]

            rr.update_record(store, record["release_id"], spread_only, "test", {})
            outcome = rv.watch_rollback(
                SERVICE, store, dd, record, sleep=lambda _: None, now=when, prod_busy=lambda: []
            )  # type: ignore[arg-type]
            self.assertEqual(outcome, "no metric without the spread rule regressed")
            self.assertEqual(rv.environmental_signals(store), {})

    def test_missing_rollback_samples_are_inconclusive(self) -> None:
        """Missing version-tagged traffic proves no recovery and never releases the hold."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rr, "post_slack", return_value=True):
            store = _store(tmp)
            record = self._rolled_back(store)
            outcome = rv.watch_rollback(
                SERVICE,
                store,
                FakeDatadog({}, {}),
                record,
                sleep=lambda _: None,
                prod_busy=lambda: [],
                now=lambda: datetime(2026, 10, 21, 11, 20, tzinfo=rr.UTC),
            )
            self.assertIn("not enough samples", outcome)
            self.assertNotIn("helped", outcome)
            self.assertEqual(rv.environmental_signals(store), {})
            self.assertEqual(rr.read_record(store, record["rolled_back_by"])["promote_hold"]["status"], "holding")

    def test_ended_deploy_in_observation_keeps_the_hold(self) -> None:
        """A cancelled deploy can contaminate the window without finishing a release record."""
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(rr, "post_slack", return_value=True),
            mock.patch.object(rr, "lock_holder", return_value=None),
            mock.patch.object(rv, "GoogleApi") as api,
        ):
            store = _store(tmp)
            record = self._rolled_back(store)
            cancelled = guard.Build.from_api(
                {
                    "id": "cancelled-fix",
                    "status": "CANCELLED",
                    "createTime": "2026-10-21T10:00:00Z",
                    "finishTime": "2026-10-21T11:10:00Z",
                    "steps": [{"id": "release-record-begin", "status": "SUCCESS"}],
                }
            )
            api.return_value.list_builds.side_effect = [[cancelled], []]
            outcome = rv.watch_rollback(
                SERVICE,
                store,
                FakeDatadog({}, {ERRORS: {"GET /b": 300}, HITS: {"GET /b": 1000}}),
                record,
                sleep=lambda _: None,
                now=lambda: datetime(2026, 10, 21, 11, 20, tzinfo=rr.UTC),
            )
            self.assertIn("cancelled-fix", outcome)
            self.assertEqual(rv.environmental_signals(store), {})
            self.assertEqual(rr.read_record(store, record["rolled_back_by"])["promote_hold"]["status"], "holding")
            query = api.return_value.list_builds.call_args_list[0].args[0]
            self.assertIn("finish_time>=", query)
            self.assertIn("2026-10-21T11:00:00.000000Z", query)

    def test_cancelled_deploy_appearing_after_metric_reads_blocks_marks(self) -> None:
        """A build that starts and ends between the two history reads still contaminates the evidence."""
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(rr, "post_slack", return_value=True),
            mock.patch.object(rr, "lock_holder", return_value=None),
            mock.patch.object(rv, "GoogleApi") as api,
        ):
            store = _store(tmp)
            record = self._rolled_back(store)
            cancelled = guard.Build.from_api(
                {
                    "id": "late-fix",
                    "status": "CANCELLED",
                    "createTime": "2026-10-21T11:10:00Z",
                    "finishTime": "2026-10-21T11:15:00Z",
                    "steps": [{"id": "release-record-begin", "status": "SUCCESS"}],
                }
            )
            api.return_value.list_builds.side_effect = [[], [], [cancelled], []]
            outcome = rv.watch_rollback(
                SERVICE,
                store,
                FakeDatadog({}, {ERRORS: {"GET /b": 300}, HITS: {"GET /b": 1000}}),
                record,
                sleep=lambda _: None,
                now=lambda: datetime(2026, 10, 21, 11, 20, tzinfo=rr.UTC),
            )
            self.assertIn("late-fix", outcome)
            self.assertEqual(rv.environmental_signals(store), {})
            self.assertEqual(rr.read_record(store, record["rolled_back_by"])["promote_hold"]["status"], "holding")

    def test_other_ended_rollback_contaminates_but_dry_run_does_not(self) -> None:
        """All other actual rollback builds count, regardless of terminal status."""
        real = guard.Build.from_api(
            {
                "id": "other",
                "status": "FAILURE",
                "createTime": "2026-10-21T11:01:00Z",
                "substitutions": {"_DRY_RUN": "false"},
            }
        )
        dry = guard.Build.from_api(
            {
                "id": "dry",
                "status": "SUCCESS",
                "createTime": "2026-10-21T11:01:00Z",
                "substitutions": {"_DRY_RUN": "true"},
            }
        )
        with mock.patch.object(rv, "GoogleApi") as api:
            api.return_value.list_builds.side_effect = [[], [real, dry]]
            changing = rv.builds_since_rollback(
                SERVICE, {"times": {"deploy_finished": "2026-10-21T11:00:00Z"}, "build": {"id": "own"}}
            )
            self.assertEqual(len(changing), 1)
            self.assertIn("other", changing[0])

    def test_build_history_ignores_pre_gate_cancel_and_own_rollback(self) -> None:
        """A cancelled build that never reached mutation and this rollback do not contaminate the window."""
        pending = guard.Build.from_api(
            {
                "id": "before-gate",
                "status": "CANCELLED",
                "createTime": "2026-10-21T11:01:00Z",
                "steps": [{"id": "release-record-begin", "status": "QUEUED"}],
            }
        )
        own = guard.Build.from_api(
            {
                "id": "rb",
                "status": "SUCCESS",
                "createTime": "2026-10-21T10:58:00Z",
                "substitutions": {"_DRY_RUN": "false"},
            }
        )
        with mock.patch.object(rv, "GoogleApi") as api:
            api.return_value.list_builds.side_effect = [[pending], [own]]
            self.assertEqual(
                rv.builds_since_rollback(
                    SERVICE,
                    {
                        "times": {"deploy_finished": "2026-10-21T11:00:00Z"},
                        "build": {"id": "rb"},
                    },
                ),
                [],
            )

    def test_active_unknown_marker_waits_before_mutation_but_terminal_unknown_is_conservative(
        self,
    ):
        for marker in ("", "STATUS_UNKNOWN"):
            active = guard.Build.from_api(
                {
                    "id": "building",
                    "status": "WORKING",
                    "createTime": "2026-10-21T11:01:00Z",
                    "steps": [{"id": "release-record-begin", "status": marker}],
                }
            )
            terminal = guard.Build.from_api(
                {
                    "id": "ended",
                    "status": "CANCELLED",
                    "createTime": "2026-10-21T11:01:00Z",
                    "steps": [{"id": "release-record-begin", "status": marker}],
                }
            )
            started = guard.Build.from_api(
                {
                    "id": "started",
                    "status": "WORKING",
                    "createTime": "2026-10-21T11:01:00Z",
                    "steps": [
                        {
                            "id": "release-record-begin",
                            "status": marker,
                            "timing": {"startTime": "2026-10-21T11:02:00Z"},
                        }
                    ],
                }
            )
            self.assertTrue(started.changing_cluster)
            with mock.patch.object(rv, "GoogleApi") as api:
                api.return_value.list_builds.side_effect = [
                    [active, terminal, started],
                    [],
                ]
                observed = rv.builds_since_rollback(SERVICE, {"times": {"deploy_finished": "2026-10-21T11:00:00Z"}})
            self.assertEqual(len(observed), 2)
            self.assertIn("ended", observed[0])

    def test_confirmed_skipped_history_does_not_contaminate_the_window(self):
        skipped = guard.Build(
            id="noop",
            status="SUCCESS",
            create_time="2026-10-21T11:01:00Z",
            commit=COMMIT,
            trigger_name="prod",
            step_status=(("release-record-begin", "SUCCESS"),),
            step_outputs=('{"release_deploy":"skipped_before_mutation"}',),
        )
        with mock.patch.object(rv, "GoogleApi") as api:
            api.return_value.list_builds.side_effect = [[skipped], []]
            self.assertEqual(
                rv.builds_since_rollback(SERVICE, {"times": {"deploy_finished": "2026-10-21T11:00:00Z"}}),
                [],
            )

    def test_one_off_window_persists_signal_recovery(self) -> None:
        """The manual checkpoint re-enables rollback before a recovered signal can fail again."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rr, "post_slack", return_value=True):
            store = _store(tmp)
            record = self._rolled_back(store)
            key = "http_error_rate demo GET /b"
            store.write(rv.ENVIRONMENTAL_OBJECT, {"signals": {key: {"status": "active"}}}, 0)
            saved = {**record, "verdict": {"status": "passed"}}
            result = {"result": "passed", "recovered_signals": [key]}
            args = argparse.Namespace(
                phase="window",
                no_metrics=True,
                wait_rollouts_seconds=0,
                release_id=record["release_id"],
                failed_marker="",
                force_fail="",
                final=False,
            )
            with (
                mock.patch.object(rv, "_token", return_value=None),
                mock.patch.object(rv, "_now", return_value=datetime(2026, 10, 21, 11, 20, tzinfo=rr.UTC)),
                mock.patch.object(rv, "evaluate", return_value=result),
                mock.patch.object(rv, "apply_result", return_value=saved),
                mock.patch.object(rv, "print_result"),
                mock.patch.object(rv, "slack_line", return_value="ok"),
                mock.patch.object(rv, "prod_state", return_value=(None, None)),
                mock.patch.object(rv, "builds_since_rollback", return_value=[]),
            ):
                self.assertEqual(rv.cmd_check(args, SERVICE, SERVICE.prod, store, mock.Mock()), rv.EXIT_OK)
            marks, _ = store.read(rv.ENVIRONMENTAL_OBJECT)
            self.assertEqual(marks["signals"][key]["status"], "recovered")
            failing = rv.judge(SERVICE.metrics[0], "demo GET /b", (0.001, 1000), (0.3, 1000))
            rv.apply_environmental([failing], rv.environmental_signals(store))
            self.assertEqual(failing.status, "exceeded")

    def test_rollback_that_helps(self) -> None:
        """After the rollback the target is back at the baseline: nothing is marked."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rr, "post_slack", return_value=True):
            store = _store(tmp)
            record = self._rolled_back(store)
            dd = FakeDatadog({}, {ERRORS: {"GET /b": 1}, HITS: {"GET /b": 1000}})
            outcome = rv.watch_rollback(
                SERVICE,
                store,
                dd,  # type: ignore[arg-type]
                record,
                sleep=lambda _: None,
                now=lambda: datetime(2026, 10, 21, 11, 20, tzinfo=rr.UTC),
                prod_busy=lambda: [],
            )
            self.assertTrue(outcome.startswith("the rollback helped"))
            self.assertEqual(rv.environmental_signals(store), {})

    def test_a_deploy_started_during_the_rollback_metric_reads(self) -> None:
        """A fix deploy contaminates the evidence even while latest still names the rollback."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rr, "post_slack", return_value=True) as slack:
            store = _store(tmp)
            record = self._rolled_back(store)
            dd = FakeDatadog({}, {ERRORS: {"GET /b": 300}, HITS: {"GET /b": 1000}})
            replies = iter([[], ["deploy build fix"]])
            outcome = rv.watch_rollback(
                SERVICE,
                store,
                dd,
                record,
                sleep=lambda _: None,
                now=lambda: datetime(2026, 10, 21, 11, 20, tzinfo=rr.UTC),
                prod_busy=lambda: next(replies),
            )
            self.assertIn("deploy build fix changes prod", outcome)
            self.assertEqual(rv.environmental_signals(store), {})
            self.assertNotIn("cause", rr.read_record(store, record["release_id"])["verdict"])
            self.assertEqual(rr.read_record(store, "20261021T110000Z-bbbbbbbbb")["promote_hold"]["status"], "holding")
            slack.assert_not_called()


class CheckpointReviewDefectsTest(unittest.TestCase):
    """Early checks, later terminal verdicts, busy prod, the runner contract and the prod build contract."""

    def _result(self, **overrides) -> dict:
        """A window check result, with defaults for what the test leaves out."""
        result = {
            "phase": "window",
            "at": "2026-10-21T10:08:00Z",
            "candidate_minutes": 3,
            "checks": [],
            "regressions": [],
            "warnings": [],
            "judged": 500,
            "signals_checked": ["logs"],
            "result": "passed",
        }
        result.update(overrides)
        return result

    def _check(self, store, record, *, minutes: float, result: dict, state=(None, None), final=True, requested=None):
        """Run release_verify.py check --phase window at deploy_finished + minutes; return (exit, record)."""
        args = argparse.Namespace(
            release_id=record["release_id"],
            phase="window",
            no_metrics=True,
            now=None,
            wait_rollouts_seconds=0,
            force_fail="",
            final=final,
            failed_marker="",
            checkpoint_minutes=requested,
        )
        when = datetime(2026, 10, 21, 10, 5, tzinfo=rr.UTC) + timedelta(minutes=minutes)
        with (
            mock.patch.object(rv, "_token", return_value=None),
            mock.patch.object(rv, "_now", return_value=when),
            mock.patch.object(rv, "prod_state", return_value=state),
            mock.patch.object(rv, "builds_since_rollback", return_value=[]),
            mock.patch.object(rv, "evaluate", return_value=result),
            mock.patch.object(rv, "print_result"),
            mock.patch.object(rv, "on_failure"),
            mock.patch.object(rr, "post_slack", return_value=True),
        ):
            outcome = rv.cmd_check(args, SERVICE, SERVICE.prod, store, mock.Mock())
        return outcome, rr.read_record(store, record["release_id"])

    def test_early_manual_check_completes_no_scheduled_checkpoint(self) -> None:
        """Early manual check completes no scheduled checkpoint."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            rv.ro.merge_plan(record, [15, 60])
            _save(store, record)
            outcome, saved = self._check(store, record, minutes=3, result=self._result())
            self.assertEqual(outcome, rv.EXIT_OK)
            receipts = saved["followup"]["checkpoints"]
            self.assertEqual({receipts[key]["state"] for key in ("window_15m", "window_60m")}, {"scheduled"})
            self.assertNotIn("window_15m", saved["checks"])
            self.assertEqual([item["key"] for item in saved["followup"]["early_checks"]], ["early_3m"])
            self.assertEqual(saved["verdict"]["status"], "pending")
            # A checkpoint asked for before its due time is early too.
            outcome, saved = self._check(store, saved, minutes=5, result=self._result(), requested=15)
            self.assertEqual(saved["followup"]["checkpoints"]["window_15m"]["state"], "scheduled")
            self.assertEqual(len(saved["followup"]["early_checks"]), 2)
            # The watch still runs the real +15 and +60 checkpoints.
            args = argparse.Namespace(
                release_id=record["release_id"],
                checkpoints="15,60",
                extend_minutes=15,
                max_window_minutes=180,
                retry_completed=False,
            )
            with (
                mock.patch.object(rv, "_datadog", return_value=(None, None)),
                mock.patch.object(rv, "_token", return_value=None),
                mock.patch.object(rv, "wait_until_settled", return_value=None),
                mock.patch.object(rv, "prod_state", return_value=(None, None)),
                mock.patch.object(rv, "builds_since_rollback", return_value=[]),
                mock.patch.object(rv, "evaluate", return_value=self._result(candidate_minutes=60)) as evaluate,
                mock.patch.object(rv, "print_result"),
                mock.patch.object(rr, "post_slack", return_value=True),
            ):
                self.assertEqual(rv.cmd_watch(args, SERVICE, SERVICE.prod, store, mock.Mock()), rv.EXIT_OK)
            self.assertEqual(evaluate.call_count, 2)
            final = rr.read_record(store, record["release_id"])
            self.assertEqual(final["followup"]["checkpoints"]["window_15m"]["state"], "completed")
            self.assertEqual(final["verdict"]["phase"], "window_60m")

    def test_early_check_failure_still_fails_the_release(self) -> None:
        """Early check failure still fails the release."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            _save(store, record)
            failed = self._result(
                candidate_minutes=4, result="failed", regressions=[{"kind": "check", "name": "images", "text": "crash"}]
            )
            outcome, saved = self._check(store, record, minutes=4, result=failed)
            self.assertEqual(outcome, rv.EXIT_FAILED)
            self.assertEqual((saved["verdict"]["status"], saved["verdict"]["phase"]), ("failed", "early_4m"))
            self.assertNotIn("checkpoints", saved["followup"])

    def test_older_checkpoint_never_replaces_a_later_terminal_verdict(self) -> None:
        """Older checkpoint never replaces a later terminal verdict."""
        cases = [
            (dict(result="inconclusive", availability={"status": "unconfirmed"}), False),
            (dict(result="passed"), True),
            (dict(result="passed", judged=0), True),
        ]
        for overrides, final in cases:
            with self.subTest(overrides=overrides, final=final), tempfile.TemporaryDirectory() as tmp:
                store = _store(tmp)
                record = _record(verdict={"status": "passed", "phase": "window_60m", "judged": 500})
                _save(store, record)
                result = self._result(candidate_minutes=70, checkpoint_minutes=15, **overrides)
                saved = rv.apply_result(SERVICE, store, record, result, final=final, token=None)
                self.assertEqual(saved["verdict"], {"status": "passed", "phase": "window_60m", "judged": 500})
                self.assertEqual(saved["followup"]["checkpoints"]["window_15m"]["state"], "completed")
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(verdict={"status": "passed", "phase": "window_60m"})
            _save(store, record)
            failed = self._result(
                checkpoint_minutes=15, result="failed", regressions=[{"kind": "check", "name": "x", "text": "x"}]
            )
            saved = rv.apply_result(SERVICE, store, record, failed, final=True, token=None)
            self.assertEqual(saved["verdict"]["status"], "failed")

    def test_older_checkpoint_error_keeps_a_later_terminal_verdict(self) -> None:
        """Older checkpoint error keeps a later terminal verdict."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(verdict={"status": "passed", "phase": "window_60m"})
            _save(store, record)
            rv._record_checkpoint_error(store, record, 15, "cluster unavailable", final=True)
            saved = rr.read_record(store, record["release_id"])
            self.assertEqual(saved["verdict"], {"status": "passed", "phase": "window_60m"})
            self.assertEqual(saved["followup"]["checkpoints"]["window_15m"]["state"], "error")

    def test_light_older_final_checkpoint_keeps_the_later_verdict(self) -> None:
        """Light older final checkpoint keeps the later verdict."""
        service = rl.parse_light(light_data("cloud_run"))
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(verdict={"status": "passed", "phase": "window_60m"})
            _save(store, record)
            args = argparse.Namespace(release_id=record["release_id"], checkpoints="15", retry_completed=True)
            unread = {"problems": [], "parts": {"logs": {"status": "inconclusive"}}, "scope": {}}
            with (
                mock.patch.object(rl, "wait_for_deploys", return_value=None),
                mock.patch.object(rl, "replaced_by", return_value=None),
                mock.patch.object(rl, "evaluate_window", return_value=unread),
                mock.patch.object(rv, "builds_since_rollback", return_value=[]),
                mock.patch.object(rr, "post_slack", return_value=True),
            ):
                rl.cmd_watch(args, service, store, mock.Mock())
            saved = rr.read_record(store, record["release_id"])
            self.assertEqual((saved["verdict"]["status"], saved["verdict"]["phase"]), ("passed", "window_60m"))

    def test_busy_prod_leaves_checkpoints_open_and_only_a_replacement_supersedes(self) -> None:
        """Busy prod leaves checkpoints open and only a replacement supersedes."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            rv.ro.merge_plan(record, [15, 60])
            _save(store, record)
            busy = (None, "build d2 is changing prod")
            outcome, saved = self._check(store, record, minutes=16, result=self._result(), state=busy, final=False)
            self.assertEqual(outcome, rv.EXIT_ERROR)
            receipts = saved["followup"]["checkpoints"]
            self.assertEqual(receipts["window_15m"]["state"], "error")
            self.assertIn("stays open", receipts["window_15m"]["last_error"])
            self.assertEqual(receipts["window_60m"]["state"], "scheduled")
            self.assertNotIn("window_15m", saved["checks"])
            outcome, saved = self._check(store, saved, minutes=17, result=self._result(), state=("release r2", None))
            self.assertEqual(outcome, rv.EXIT_ERROR)
            receipts = saved["followup"]["checkpoints"]
            self.assertEqual({receipt["state"] for receipt in receipts.values()}, {"superseded"})

    def test_window_check_with_an_injected_runner_passes_its_deadline(self) -> None:
        """Window check with an injected runner passes its deadline."""
        timeouts: list[float] = []

        class TimedCluster(FakeCluster):
            """FakeCluster that records the timeout of every command."""

            def __call__(self, command: list[str], timeout: float = 120) -> str:
                """Record the timeout, then answer."""
                timeouts.append(timeout)
                return super().__call__(command, timeout)

        clients = rr.Clients(env=PROD, runner=TimedCluster(digest=NEW))
        result = rv.evaluate(
            SERVICE,
            _record(),
            phase="window",
            clients=clients,
            dd=None,
            now=datetime(2026, 10, 21, 10, 20, tzinfo=rr.UTC),
            get=mock.Mock(side_effect=AssertionError("no HTTP in a window check")),
        )
        images = next(check for check in result["checks"] if check["name"] == "images")
        self.assertEqual(images["status"], "passed", images)
        self.assertEqual(result["availability"]["status"], "confirmed")
        self.assertTrue(timeouts)
        self.assertTrue(all(0 < value <= 120 for value in timeouts), timeouts)

    def test_finish_retries_then_alerts_with_the_recovery_commands(self) -> None:
        """Finish retries then alerts with the recovery commands."""
        calls = []

        def flaky() -> int:
            """Fail twice, then finish."""
            calls.append(1)
            if len(calls) < 3:
                raise rr.RecordError("GCS 503")
            return 0

        sleeps: list[float] = []
        with mock.patch.object(rr, "post_slack", return_value=True) as slack:
            code = rr.finish_with_retries(
                flaky, attempts=3, title="demo", env="prod", release_id="r1", recovery=["a"], sleep=sleeps.append
            )
            self.assertEqual((code, len(calls), len(sleeps)), (0, 3, 2))
            slack.assert_not_called()
            recovery = rr.finish_recovery("gke", "demo", "prod", "r1", "deployed")
            code = rr.finish_with_retries(
                mock.Mock(side_effect=OSError("disk")),
                attempts=3,
                title="demo",
                env="prod",
                release_id="r1",
                recovery=recovery,
                sleep=lambda _: None,
            )
        self.assertEqual(code, 1)
        text = slack.call_args[0][0]
        for command in recovery:
            self.assertIn(command, text)
        self.assertIn("release_record.py finish --service demo --env prod --release-id r1 --status deployed", text)
        self.assertIn("release_verify.py check --service demo --env prod --phase deploy --release-id r1", text)
        self.assertIn("release_verify.py schedule-window --service demo --env prod --release-id r1", text)
        light = rr.finish_recovery("light", "demo-run", "prod", "r1", "deploy_failed")
        self.assertIn("release_light.py --service demo-run finish --env prod --release-id r1", light[0])
        self.assertIn("schedule-window --release-id r1", light[2])

    def test_retried_finish_keeps_the_first_finish_time(self) -> None:
        """Retried finish keeps the first finish time."""
        record = {"status": "deployed", "times": {"deploy_finished": "2026-10-21T10:05:00Z"}}
        rr.keep_finish_time(record, "deployed")
        self.assertEqual(record["times"]["deploy_finished"], "2026-10-21T10:05:00Z")
        record["status"] = "deploying"
        rr.keep_finish_time(record, "deployed")
        self.assertNotEqual(record["times"]["deploy_finished"], "2026-10-21T10:05:00Z")

    def test_finish_cli_retries_and_returns_red(self) -> None:
        """Finish cli retries and returns red."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "release_id").write_text("r1\n", encoding="utf-8")
            with (
                mock.patch.object(rr, "open_from_args", return_value=(SERVICE, PROD, mock.Mock(), mock.Mock())),
                mock.patch.object(rr, "cmd_finish", side_effect=rr.RecordError("GCS 503")) as finish,
                mock.patch.object(rr, "post_slack", return_value=True) as slack,
                mock.patch.object(rr.time, "sleep"),
            ):
                code = rr.main(
                    ["finish", "--env", "prod", "--attempts", "3", "--release-id-file", f"{tmp}/release_id"]
                    + ["--status", "deployed"]
                )
            self.assertEqual((code, finish.call_count), (1, 3))
            self.assertIn("--release-id r1", slack.call_args[0][0])

    def test_prod_build_contract(self) -> None:
        """Prod build contract."""
        good = """steps:
  - id: notify-promote-blocked
    args:
      - |
        if [ ! -f /workspace/promote_blocked ]; then exit 0; fi
        printf '%s\\n' '{"release_deploy":"skipped_before_mutation"}' > "$${BUILDER_OUTPUT}/output"
        exit 1
  - id: release-record-begin
    args:
      - |
        release_no_change() {
          printf '%s\\n' '{"release_deploy":"skipped_before_mutation"}' > "$${BUILDER_OUTPUT}/output"
        }
        if [ -f /workspace/deploy_failed ] || [ -f /workspace/deploy_superseded ] || [ -f /workspace/promote_blocked ]; then
          release_no_change
          exit 0
        fi
        python3 release/release_record.py begin --env prod --commit "$COMMIT_SHA" \\
          || { echo failed; release_no_change; }
  - id: release-record-finish
    secretEnv: ["WEBHOOK_ORBIT"]
    args:
      - |
        python3 release/release_record.py finish --env prod --attempts 3 --status deployed
"""
        self.assertEqual(kit.prod_build_problems("good.yaml", good), [])
        old_skip = good.replace(
            " || [ -f /workspace/deploy_superseded ] || [ -f /workspace/promote_blocked ]",
            " || [ -f /workspace/deploy_superseded ]",
        )
        self.assertIn("promote_blocked", kit.prod_build_problems("b.yaml", old_skip)[0])
        no_failure_receipt = good.replace("echo failed; release_no_change;", "echo failed;")
        self.assertIn("release_no_change", kit.prod_build_problems("b.yaml", no_failure_receipt)[0])
        no_retry = good.replace(" --attempts 3", "")
        self.assertIn("--attempts", kit.prod_build_problems("b.yaml", no_retry)[0])
        no_alert = good.replace('    secretEnv: ["WEBHOOK_ORBIT"]\n', "")
        self.assertEqual(len(kit.prod_build_problems("b.yaml", no_alert)), 1)
        self.assertIn("WEBHOOK_ORBIT", kit.prod_build_problems("b.yaml", no_alert)[0])
        block_lists = [
            "    secretEnv:\n      - GITHUB_TOKEN\n      - WEBHOOK_ORBIT\n",
            '    secretEnv:\n    - "WEBHOOK_ORBIT"\n',
            '    secretEnv: [\n      "GITHUB_TOKEN",\n      "WEBHOOK_ORBIT",\n    ]\n',
        ]
        for listing in block_lists:
            with self.subTest(listing=listing):
                variant = good.replace('    secretEnv: ["WEBHOOK_ORBIT"]\n', listing)
                self.assertEqual(kit.prod_build_problems("b.yaml", variant), [])
        other = good.replace('    secretEnv: ["WEBHOOK_ORBIT"]\n', "    secretEnv:\n      - GITHUB_TOKEN\n")
        self.assertIn("WEBHOOK_ORBIT", kit.prod_build_problems("b.yaml", other)[0])
        silent_block = good.replace(
            """        printf '%s\\n' '{"release_deploy":"skipped_before_mutation"}' > "$${BUILDER_OUTPUT}/output"
        exit 1""",
            "        exit 1",
        )
        self.assertIn("notify-promote-blocked", kit.prod_build_problems("b.yaml", silent_block)[0])
        self.assertEqual(kit.prod_build_problems("dev.yaml", good.replace("--env prod", "--env dev")), [])

    def test_prod_build_skip_paths_leave_the_receipt_in_bash(self) -> None:
        """Run the record-begin script of each prod build of the repository: every stop before the deploy
        writes the receipt (orbit-infrastructure has none; test_release_vercel runs the rendered ones)."""
        root = Path(__file__).resolve().parents[1]
        checked = 0
        for path in sorted(root.glob("cloudbuild*.yaml")):
            text = path.read_text(encoding="utf-8")
            steps = {kit._step_id(block): block for block in kit.STEP_START.split(text)}
            if not kit.PROD_RECORD.search(steps.get("release-record-begin", "")):
                continue
            with self.subTest(build=path.name):
                assert_receipts(self, steps["release-record-begin"], steps.get("notify-promote-blocked"))
            checked += 1
        if not checked:
            self.skipTest("no prod build file next to the kit")

    def test_offline_launcher_nulls_credentials_and_blocks_live_clis(self) -> None:
        """Offline launcher nulls credentials and blocks live clis."""
        import run_offline_checks as offline

        script = (
            "import json, os, subprocess\n"
            "names = ('KUBECONFIG', 'GOOGLE_APPLICATION_CREDENTIALS', 'CLOUDSDK_CONFIG', 'RELEASE_SLACK_DISABLED',"
            " 'HTTPS_PROXY', 'SECRET_TOKEN')\n"
            "seen = {name: os.environ.get(name) for name in names}\n"
            "seen['kubectl'] = subprocess.run(['kubectl', 'get', 'pods'], capture_output=True).returncode\n"
            "seen['gcloud'] = subprocess.run(['gcloud', 'auth', 'list'], capture_output=True).returncode\n"
            "open('seen.json', 'w').write(json.dumps(seen))\n"
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"SECRET_TOKEN": "x"}):
            self.assertEqual(offline.main(["--cwd", tmp, "--", sys.executable, "-c", script]), 0)
            seen = json.loads(Path(tmp, "seen.json").read_text(encoding="utf-8"))
        self.assertEqual((seen["KUBECONFIG"], seen["GOOGLE_APPLICATION_CREDENTIALS"]), ("/dev/null", "/dev/null"))
        self.assertTrue(seen["CLOUDSDK_CONFIG"].endswith("gcloud-config"))
        self.assertEqual((seen["RELEASE_SLACK_DISABLED"], seen["HTTPS_PROXY"]), ("1", "http://127.0.0.1:9"))
        self.assertIsNone(seen["SECRET_TOKEN"])
        self.assertEqual((seen["kubectl"], seen["gcloud"]), (offline.BLOCKED_EXIT, offline.BLOCKED_EXIT))

    def test_finish_refuses_to_hide_a_newer_release(self) -> None:
        """A late finish (a recovery from the alert) leaves the old record and latest.json alone once a newer
        release or rollback is latest; the build's own finish records that it names its failed steps."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            old = _record(status="deploying", build={"id": "deploy1"})
            _save(store, old, latest=False)
            newer = _record(release_id="20261021T120000Z-ccccccccc", kind="rollback")
            _save(store, newer)
            args = argparse.Namespace(
                release_id_file=None, release_id=old["release_id"], status="deployed", failed_step_file=None
            )
            clients = rr.Clients(env=PROD, runner=FakeCluster(digest=NEW))
            with self.assertRaises(rr.RecordError):
                rr.cmd_finish(args, PROD, store, clients)
            self.assertEqual(rr.read_record(store, old["release_id"])["status"], "deploying")
            self.assertEqual(store.read("latest.json")[0]["release_id"], newer["release_id"])
            with self.assertRaises(rr.RecordError):
                rr.point_latest_unless_newer(store, old)
            # The build's own finish (latest is older): it finishes and notes the named steps.
            store2 = _store(f"{tmp}/own")
            _save(store2, _record(release_id="20261020T100000Z-bbbbbbbbb"))
            _save(store2, old, latest=False)
            args.failed_step_file = f"{tmp}/deploy_failed_step"
            rr.cmd_finish(args, PROD, store2, clients)
            saved = rr.read_record(store2, old["release_id"])
            self.assertEqual((saved["status"], saved["deploy_steps_named"]), ("deployed", True))
            self.assertEqual(store2.read("latest.json")[0]["release_id"], old["release_id"])

    def test_inconclusive_dev_verify_is_not_verified(self) -> None:
        """A dev result that capacity left inconclusive is not a pass: the index blocks the promote."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(environment="dev", verdict={"status": "pending"})
            _save(store, record)
            result = self._result(
                phase="deploy",
                result="inconclusive",
                checks=[{"name": "rollouts", "status": "environmental", "details": ["1 Deployments", "capacity"]}],
            )
            with (
                mock.patch.object(rr, "post_slack", return_value=True) as slack,
                mock.patch.object(rv, "_point_verified") as pointed,
            ):
                saved = rv.finish_dev_verify(SERVICE, store, record, result, build_url="https://b")
            self.assertEqual(saved["verdict"]["status"], "inconclusive")
            self.assertIn("rollouts: 1 Deployments; capacity", saved["verdict"]["reason"])
            index, _ = store.read(rg.dev_commit_index(record["commit"]))
            self.assertEqual(index["verdict"]["status"], "inconclusive")
            pointed.assert_not_called()
            self.assertIn("dev verify inconclusive", slack.call_args[0][0])

    def test_rollback_watch_reads_a_failed_early_check(self) -> None:
        """A failed early check is the verdict's phase: its regressions stay readable after the rollback."""
        record = {"checks": {}, "followup": {}}
        rv.ro.note_early_check(record, {"at": "t", "result": "failed", "regressions": [{"kind": "metric"}]}, "early_4m")
        self.assertEqual(rv.ro.phase_result(record, "early_4m")["regressions"], [{"kind": "metric"}])
        record["checks"]["window_15m"] = {"regressions": []}
        self.assertEqual(rv.ro.phase_result(record, "window_15m"), {"regressions": []})
        self.assertIsNone(rv.ro.phase_result(record, "window_60m"))
        # Two early checks in one rounded minute: the verdict's time picks the failure that set it.
        failing = {"at": "t1", "result": "failed", "regressions": [{"kind": "metric"}, {"kind": "check"}]}
        later = {"at": "t2", "result": "failed", "regressions": [{"kind": "metric"}]}
        same = {"checks": {}, "followup": {}}
        rv.ro.note_early_check(same, failing, "early_3m")
        rv.ro.note_early_check(same, later, "early_3m")
        rv.ro.note_early_check(same, {"at": "t3", "result": "passed", "regressions": []}, "early_3m")
        self.assertEqual(rv.ro.phase_result(same, "early_3m", "t1"), failing)
        self.assertEqual(rv.ro.phase_result(same, "early_3m"), failing)


def step_script(block: str) -> str:
    """The bash script of one Cloud Build step block (the "- |" argument), with Cloud Build's $$ unescaped."""
    lines = block.splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == "- |") + 1
    indent = len(lines[start]) - len(lines[start].lstrip())
    body = []
    for line in lines[start:]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    return "\n".join(body).replace("$$", "$")


def assert_receipts(test: unittest.TestCase, begin: str, blocked: str | None) -> None:
    """Every pre-deploy stop of a prod build's record-begin (and blocked notice) leaves the no-change receipt."""
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp, "bin")
        stub.mkdir()
        for name in ("python3", "curl"):
            path = stub / name
            path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            path.chmod(0o700)
        scenarios = [("begin", begin, marker) for marker in ("deploy_failed", "deploy_superseded", "promote_blocked")]
        scenarios.append(("begin", begin, "record_begin_fails"))
        if blocked:
            scenarios.append(("blocked", blocked, "promote_blocked"))
        for kind, block, marker in scenarios:
            workspace, output = Path(tmp, f"ws-{kind}-{marker}"), Path(tmp, f"out-{kind}-{marker}")
            workspace.mkdir()
            output.mkdir()
            if marker != "record_begin_fails":
                (workspace / marker).touch()
            (workspace / "release_image").write_text("repo@sha256:" + "1" * 64, encoding="utf-8")
            script = step_script(block).replace("/workspace", str(workspace))
            subprocess.run(
                ["bash", "-c", script],
                env={"PATH": f"{stub}:/usr/bin:/bin", "BUILDER_OUTPUT": str(output), "WEBHOOK_ORBIT": ""},
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            receipt = output / "output"
            test.assertTrue(receipt.exists(), f"{kind} {marker}: no receipt")
            test.assertEqual(json.loads(receipt.read_text()), {"release_deploy": "skipped_before_mutation"})


# --------------------------------------------------------------------------- people map in public repositories


class PeopleMapTest(unittest.TestCase):
    """The GitHub-to-Slack map: vendored in private repositories, from Secret Manager in public ones."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="release-people-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.missing = self.tmp / "missing.json"

    def load(self, path: Path, env: str | None) -> dict:
        values = {} if env is None else {"RELEASE_PEOPLE_JSON": env}
        with mock.patch.dict(os.environ, values, clear=False), mock.patch("sys.stderr"):
            if env is None:
                os.environ.pop("RELEASE_PEOPLE_JSON", None)
            return rv.load_people(path)

    def test_the_file_wins_and_the_secret_is_the_fallback(self) -> None:
        vendored = self.tmp / "people.json"
        vendored.write_text(json.dumps({"slack_ids": {"a": "UFILE"}, "fallback": ["UF"]}))
        secret = json.dumps({"slack_ids": {"a": "USECRET"}, "fallback": []})
        self.assertEqual(self.load(vendored, secret)["slack_ids"], {"a": "UFILE"})
        self.assertEqual(self.load(self.missing, secret)["slack_ids"], {"a": "USECRET"})
        self.assertEqual(self.load(self.missing, None), {"slack_ids": {}, "fallback": []})

    def test_a_malformed_map_mentions_nobody_and_never_raises(self) -> None:
        bad = [
            "not json",
            "[1]",
            '{"slack_ids": ["a"]}',
            '{"slack_ids": {"a": 7}}',
            '{"fallback": "U1"}',
            '{"fallback": [1]}',
        ]
        for text in bad:
            with self.subTest(text=text):
                self.assertEqual(self.load(self.missing, text), {"slack_ids": {}, "fallback": []})
                vendored = self.tmp / "bad.json"
                vendored.write_text(text)
                self.assertEqual(self.load(vendored, None), {"slack_ids": {}, "fallback": []})

    def test_an_undecodable_map_mentions_nobody(self) -> None:
        vendored = self.tmp / "binary.json"
        vendored.write_bytes(b'{"slack_ids": {"a": "\xff\xfe"}}')
        self.assertEqual(self.load(vendored, None), {"slack_ids": {}, "fallback": []})

    def test_sync_check_catches_a_stale_private_map(self) -> None:
        if not (HERE.parent / "sync.py").is_file():
            self.skipTest("sync.py lives in orbit-infrastructure/release-kit, not in a consuming repository")
        sys.path.insert(0, str(HERE.parent))
        import sync  # noqa: PLC0415

        repo = self.tmp / "private-repo"
        with mock.patch("builtins.print"):
            sync.sync(repo, "private")
        pin_path = repo / "release" / "KIT.json"
        pin = json.loads(pin_path.read_text())
        # An older people map with a matching old pin: the shared version still matches.
        (repo / "release" / kit.PEOPLE_FILE).write_text("{}")
        pin["files"][kit.PEOPLE_FILE] = kit.digest(repo / "release" / kit.PEOPLE_FILE)
        pin_path.write_text(json.dumps(pin))
        with mock.patch("builtins.print") as printed, mock.patch.object(sync.subprocess, "run") as run:
            self.assertEqual(sync.check(repo, "private"), 1)
        run.assert_not_called()
        self.assertIn("is stale", printed.call_args[0][0])

    def test_a_malformed_map_still_sends_the_alert(self) -> None:
        record = _record(verdict={"status": "failed", "phase": "window_15m", "reasons": ["x"]})
        with (
            mock.patch.object(rv, "PEOPLE_FILE", self.missing),
            mock.patch.dict(os.environ, {"RELEASE_PEOPLE_JSON": '{"slack_ids": null, "fallback": 3}'}),
            mock.patch("sys.stderr"),
        ):
            text, _ = rv.rollback_alert(SERVICE, record, None, "gs://x")
        self.assertTrue(text)
        self.assertNotIn("<@", text)

    def test_logs_never_show_slack_ids(self) -> None:
        self.assertEqual(rr.redact_slack_ids("<@U095N7BBQKD> and <@UFALL> failed"), "<@...> and <@...> failed")
        with mock.patch.dict(os.environ, {"RELEASE_SLACK_DISABLED": "1"}), mock.patch("builtins.print") as printed:
            self.assertFalse(rr.post_slack("<@U095N7BBQKD> release failed"))
        self.assertIn("<@...> release failed", printed.call_args[0][0])
        self.assertNotIn("U095N7BBQKD", printed.call_args[0][0])

    def test_public_builds_read_the_secret(self) -> None:
        import test_release_vercel as tv  # noqa: PLC0415

        services = [
            SERVICE,
            rl.parse_light(light_data("cloud_run")),
            rl.parse_light(light_data("composer")),
            rl.parse_light(tv.vercel_data()),
        ]
        private: dict[str, str] = {}
        public: dict[str, str] = {}
        for service in services:
            with mock.patch.object(kit, "people_vendored", return_value=True):
                private.update(kit.rendered(service))
            with mock.patch.object(kit, "people_vendored", return_value=False):
                public.update(kit.rendered(service))
        self.assertGreaterEqual(len(public), 3)
        for name, text in public.items():
            with self.subTest(name=name):
                self.assertNotIn("RELEASE_PEOPLE_JSON", private[name])
                webhook_steps = text.count('"WEBHOOK_ORBIT"]') + text.count('"WEBHOOK_ORBIT", ')
                self.assertGreater(webhook_steps, 0)
                for line in text.splitlines():
                    if "secretEnv:" in line and "WEBHOOK_ORBIT" in line:
                        self.assertIn('"RELEASE_PEOPLE_JSON"', line)
                self.assertEqual(
                    text.count("secrets/release-people-json/versions/latest"),
                    text.count("secrets/slack-webhook-workspace-orbit/versions/latest"),
                )
                self.assertEqual(kit.with_people_secret(text), text)  # idempotent
                self.assertEqual(kit.prod_build_problems(name, text), kit.prod_build_problems(name, private[name]))

    def test_visibility_from_the_flag_or_the_event(self) -> None:
        event = self.tmp / "event.json"
        event.write_text(json.dumps({"repository": {"private": False}}))
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(event)}):
            self.assertTrue(kit.repository_is_public())
            self.assertFalse(kit.repository_is_public("private"))
        event.write_text(json.dumps({"repository": {"private": True}}))
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(event)}):
            self.assertFalse(kit.repository_is_public())
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": ""}):
            self.assertIsNone(kit.repository_is_public())

    def test_check_refuses_a_map_in_a_public_repo(self) -> None:
        kit_dir = self.tmp / "release"
        kit_dir.mkdir()
        people = kit_dir / kit.PEOPLE_FILE
        people.write_text("{}")
        pinned = {kit.PEOPLE_FILE: kit.digest(people)}
        with mock.patch.object(kit, "KIT_DIR", kit_dir):
            self.assertEqual(kit.people_problems(pinned, public=False), [])
            self.assertEqual(kit.people_problems(pinned, public=None), [])
            refused = kit.people_problems(pinned, public=True)
            self.assertEqual(len(refused), 1)
            self.assertIn("must not be in a public repository", refused[0])
            people.write_text('{"edited": true}')
            self.assertIn("differs from KIT.json", kit.people_problems(pinned, public=False)[0])
            people.unlink()
            self.assertIn("is missing but KIT.json pins it", kit.people_problems(pinned, public=False)[0])
            self.assertEqual(kit.people_problems({}, public=True), [])
            self.assertEqual(kit.people_problems(pinned, public=True), [])

    def test_sync_leaves_the_map_out_of_public_repositories(self) -> None:
        if not (HERE.parent / "sync.py").is_file():
            self.skipTest("sync.py lives in orbit-infrastructure/release-kit, not in a consuming repository")
        sys.path.insert(0, str(HERE.parent))
        import sync  # noqa: PLC0415

        repo = self.tmp / "repo"
        with mock.patch("builtins.print"):
            sync.sync(repo, "private")
        self.assertTrue((repo / "release" / kit.PEOPLE_FILE).is_file())
        pin = json.loads((repo / "release" / "KIT.json").read_text())
        self.assertEqual(pin["visibility"], "private")
        self.assertIn(kit.PEOPLE_FILE, pin["files"])
        with mock.patch("builtins.print"):
            sync.sync(repo, "public")  # re-sync as public deletes it
        self.assertFalse((repo / "release" / kit.PEOPLE_FILE).exists())
        public_pin = json.loads((repo / "release" / "KIT.json").read_text())
        self.assertEqual(public_pin["visibility"], "public")
        self.assertNotIn(kit.PEOPLE_FILE, public_pin["files"])
        self.assertEqual(public_pin["version"], pin["version"])  # one kit version for both
        # Later syncs without --visibility keep the saved visibility.
        self.assertEqual(sync.detect_visibility(repo), "public")

    def test_sync_detects_known_public_repositories(self) -> None:
        if not (HERE.parent / "sync.py").is_file():
            self.skipTest("sync.py lives in orbit-infrastructure/release-kit, not in a consuming repository")
        sys.path.insert(0, str(HERE.parent))
        import sync  # noqa: PLC0415

        repo = self.tmp / "mcp"
        (repo / "release" / "services").mkdir(parents=True)
        (repo / "release" / "services" / "orbit-mcp.json").write_text(
            json.dumps({"github_repo": "orbit-search/orbit-mcp"})
        )
        self.assertEqual(sync.detect_visibility(repo), "public")
        (repo / "release" / "services" / "orbit-mcp.json").write_text(json.dumps({"github_repo": "orbit-search/digit"}))
        self.assertEqual(sync.detect_visibility(repo), "private")


class LateFailedReceiptAndRecoveryTest(unittest.TestCase):
    """Greptile P1s on orbit-flutter-backend #110: each test fails on the kit before the fix."""

    @staticmethod
    def _failed_final_receipt(store) -> dict:
        """A manual window check completed +60 with a failed verdict after the watch's first read."""
        record = _record()
        check = {"at": "2026-10-21T11:05:00Z", "result": "failed", "regressions": ["new error line"]}
        record["checks"]["window_60m"] = check
        rv.ro.note_checkpoint(record, check, "window_60m")
        record["verdict"] = {"status": "failed", "at": "2026-10-21T11:05:00Z", "phase": "window_60m"}
        _save(store, record)
        return record

    @staticmethod
    def _first_read_still_pending():
        """read_record that answers the watch's first read from before the manual check."""
        original = rr.read_record
        reads = []

        def read(store, release_id):
            current = original(store, release_id)
            reads.append(release_id)
            if len(reads) == 1:
                current = {**current, "verdict": {"status": "pending"}}
                current["followup"] = {"owner": current["followup"]["owner"]}
            return current

        return read

    def test_watch_rolls_back_a_failed_final_receipt_it_skips(self) -> None:
        """The watch skips a completed checkpoint, but a failed verdict on it still starts the rollback."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            self._failed_final_receipt(store)
            args = argparse.Namespace(
                release_id=_record()["release_id"],
                checkpoints="60",
                extend_minutes=15,
                max_window_minutes=180,
                retry_completed=False,
            )
            with (
                mock.patch.object(rr, "read_record", side_effect=self._first_read_still_pending()),
                mock.patch.object(rv, "_datadog", return_value=(None, None)),
                mock.patch.object(rv, "_token", return_value=None),
                mock.patch.object(rv.rs, "CloudLogging"),
                mock.patch.object(rv, "GoogleApi"),
                mock.patch.object(rv, "wait_until_settled", return_value=None),
                mock.patch.object(rv, "evaluate") as evaluate,
                mock.patch.object(rv, "on_failure") as on_failure,
                mock.patch.object(rr, "post_slack", return_value=True),
            ):
                rv.cmd_watch(args, SERVICE, SERVICE.prod, store, mock.Mock())
            evaluate.assert_not_called()
            on_failure.assert_called_once()
            self.assertEqual(on_failure.call_args[0][2]["verdict"]["status"], "failed")

    def test_light_watch_rolls_back_a_failed_final_receipt_it_skips(self) -> None:
        """The same for Cloud Run, Composer, Vercel and Railway services."""
        service = rl.parse_light(light_data("cloud_run"))
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            self._failed_final_receipt(store)
            args = argparse.Namespace(release_id=_record()["release_id"], checkpoints="60", retry_completed=False)
            with (
                mock.patch.object(rr, "read_record", side_effect=self._first_read_still_pending()),
                mock.patch.object(rv, "builds_since_rollback", return_value=[]),
                mock.patch.object(rl, "wait_for_deploys", return_value=None),
                mock.patch.object(rl, "replaced_by", return_value=None),
                mock.patch.object(rl, "evaluate_window") as evaluate,
                mock.patch.object(rl, "on_failed") as on_failed,
                mock.patch.object(rr, "post_slack", return_value=True),
            ):
                rl.cmd_watch(args, service, store, mock.Mock())
            evaluate.assert_not_called()
            on_failed.assert_called_once()

    def test_light_watch_rolls_back_a_failure_a_manual_check_saves_during_its_checks(self) -> None:
        """Greptile P1 on company-intelligence-research #41: the manual check fails the release while the watch
        sleeps or checks, and the watch's own checks pass. The watch still starts the rollback and posts no pass."""
        service = rl.parse_light(light_data("cloud_run"))
        clean = {"problems": [], "parts": {"logs": {"status": "checked"}}, "scope": {}}
        for checkpoints in ("60", "15 60"):
            with self.subTest(checkpoints=checkpoints), tempfile.TemporaryDirectory() as tmp:
                store = _store(tmp)
                record = _record()
                _save(store, record)
                first = f"window_{checkpoints.split()[0]}m"

                def manual_failure_then_clean(*_args, key: str = first, **_kwargs) -> dict:
                    """A manual window check completes this checkpoint with a failed verdict during the watch's checks."""

                    def fail(current: dict) -> None:
                        """The manual check's write: its result, its receipt and the failed verdict."""
                        check = {"at": "2026-10-21T11:05:00Z", "result": "failed", "regressions": ["new error"]}
                        current["checks"][key] = check
                        rv.ro.note_checkpoint(current, check, key)
                        current["verdict"] = {"status": "failed", "at": "2026-10-21T11:05:00Z", "phase": key}

                    rr.update_record(store, record["release_id"], fail, "verify_manual")
                    return clean

                args = argparse.Namespace(
                    release_id=record["release_id"], checkpoints=checkpoints, retry_completed=False
                )
                with (
                    mock.patch.object(rv, "builds_since_rollback", return_value=[]),
                    mock.patch.object(rl, "wait_for_deploys", return_value=None),
                    mock.patch.object(rl, "replaced_by", return_value=None),
                    mock.patch.object(rl, "evaluate_window", side_effect=manual_failure_then_clean) as evaluate,
                    mock.patch.object(rl, "on_failed") as on_failed,
                    mock.patch.object(rr, "post_slack", return_value=True) as slack,
                ):
                    self.assertEqual(rl.cmd_watch(args, service, store, mock.Mock()), rv.EXIT_OK)
                evaluate.assert_called_once()
                on_failed.assert_called_once()
                self.assertEqual(on_failed.call_args[0][2]["verdict"]["status"], "failed")
                self.assertFalse([call for call in slack.call_args_list if "passed" in str(call)])
                self.assertEqual(rr.read_record(store, record["release_id"])["verdict"]["status"], "failed")

    def test_light_watch_rolls_back_a_failure_saved_before_its_final_write(self) -> None:
        """Greptile P1 on orbit-flutter-backend #111 and orbit-chatgpt-app #121: the manual failure lands after
        the final checkpoint's result is saved and before the final verdict write. The final write keeps the
        failure; the watch acts on the record it returns and posts no pass."""
        service = rl.parse_light(light_data("cloud_run"))
        clean = {"problems": [], "parts": {"logs": {"status": "checked"}}, "scope": {}}
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record()
            _save(store, record)
            original_owner = rv.require_owner
            writes = []

            def owner_then_manual_failure(store_, saved):
                """The owner read after the checkpoint's save; the manual check writes its failure right then."""
                if not writes:
                    writes.append(1)

                    def fail(current: dict) -> None:
                        """The manual check's write: its result and the failed verdict."""
                        check = {"at": "2026-10-21T11:05:00Z", "result": "failed", "regressions": ["new error"]}
                        current["checks"]["window_60m"] = check
                        current["verdict"] = {"status": "failed", "at": "2026-10-21T11:05:00Z", "phase": "window_60m"}

                    rr.update_record(store, record["release_id"], fail, "verify_manual")
                return original_owner(store_, saved)

            args = argparse.Namespace(release_id=record["release_id"], checkpoints="60", retry_completed=False)
            with (
                mock.patch.object(rv, "builds_since_rollback", return_value=[]),
                mock.patch.object(rl, "wait_for_deploys", return_value=None),
                mock.patch.object(rl, "replaced_by", return_value=None),
                mock.patch.object(rl, "evaluate_window", return_value=clean),
                mock.patch.object(rv, "require_owner", side_effect=owner_then_manual_failure),
                mock.patch.object(rl, "on_failed") as on_failed,
                mock.patch.object(rr, "post_slack", return_value=True) as slack,
            ):
                self.assertEqual(rl.cmd_watch(args, service, store, mock.Mock()), rv.EXIT_OK)
            on_failed.assert_called_once()
            self.assertEqual(on_failed.call_args[0][2]["verdict"]["status"], "failed")
            self.assertFalse([call for call in slack.call_args_list if "passed" in str(call)])
            self.assertEqual(rr.read_record(store, record["release_id"])["verdict"]["status"], "failed")

    def test_finish_recovery_keeps_the_failed_step_guard(self) -> None:
        """The alert's finish command, run after the build ended, still names the failed step."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _save(store, _record(release_id="20261020T100000Z-bbbbbbbbb"))
            pending = _record(status="deploying", build={"id": "deploy1"})
            _save(store, pending, latest=False)
            Path(tmp, "release_id").write_text(pending["release_id"] + "\n", encoding="utf-8")
            step_file = Path(tmp, "deploy_failed_step")
            step_file.write_text('{"id": "deploy", "deployments": ["prod-demo"]}\n', encoding="utf-8")
            clients = rr.Clients(env=PROD, runner=FakeCluster(digest=NEW))
            with (
                mock.patch.object(rr, "open_from_args", return_value=(SERVICE, PROD, store, clients)),
                mock.patch.object(rr, "snapshot_workloads", side_effect=rr.RecordError("kubectl timed out")),
                mock.patch.object(rr, "post_slack", return_value=True) as slack,
                mock.patch.object(rr.time, "sleep"),
            ):
                code = rr.main(
                    ["finish", "--env", "prod", "--attempts", "3", "--release-id-file", f"{tmp}/release_id"]
                    + ["--status", "deploy_failed", "--failed-step-file", str(step_file)]
                )
            self.assertEqual(code, 1)
            finish = re.search(r"1\) `([^`]+)`", slack.call_args[0][0]).group(1)
            # The build is over: its /workspace is gone. A human runs the command from the alert.
            step_file.unlink()
            argv = shlex.split(finish)
            self.assertEqual(argv[:3], ["python3", "release/release_record.py", "finish"])
            with mock.patch.object(rr, "open_from_args", return_value=(SERVICE, PROD, store, clients)):
                self.assertEqual(rr.main(argv[2:]), 0)
            saved = rr.read_record(store, pending["release_id"])
            self.assertEqual(saved["status"], "deploy_failed")
            self.assertEqual(saved["failed_step"]["id"], "deploy")
            self.assertTrue(saved["deploy_steps_named"])

    def test_finish_recovery_without_a_named_step_still_marks_steps_named(self) -> None:
        """A build that passes the step file but named no step: the recovery keeps deploy_steps_named."""
        recovery = rr.finish_recovery("gke", "demo", "prod", "r1", "deploy_failed", failed_step=None, steps_named=True)
        argv = shlex.split(recovery[0])
        self.assertIn("--failed-step", argv)
        self.assertEqual(argv[argv.index("--failed-step") + 1], "")
        plain = rr.finish_recovery("gke", "demo", "prod", "r1", "deployed")
        self.assertNotIn("--failed-step", plain[0])


PEOPLE = {"slack_ids": {"jonnybcw": "UJONNY"}, "fallback": ["UFALLBACK"]}


class UnclaimedReleaseOwnerTest(unittest.TestCase):
    """A release that no agent claimed (a person merged it): its PR author owns it, quietly.

    _record()'s release commit is PR #12 by jonnybcw. Each test fails on the kit before the change."""

    def _watch(self, store, record, *, people=PEOPLE, result=None, error=None):
        """Run the GKE watch over +15 and +60; return (exit code, every Slack text)."""
        args = argparse.Namespace(
            release_id=record["release_id"],
            checkpoints="15,60",
            extend_minutes=15,
            max_window_minutes=180,
            retry_completed=False,
        )
        result = result or VerdictTest._result(self)
        with (
            mock.patch.object(rv, "load_people", return_value=people),
            mock.patch.object(rv, "_datadog", return_value=(None, None)),
            mock.patch.object(rv, "_token", return_value=None),
            mock.patch.object(rv.rs, "CloudLogging"),
            mock.patch.object(rv, "GoogleApi"),
            mock.patch.object(rv, "wait_until_settled", return_value=None),
            mock.patch.object(rv, "prod_state", return_value=(None, None)),
            mock.patch.object(rv, "builds_since_rollback", return_value=[]),
            mock.patch.object(rv, "evaluate", side_effect=error, return_value=result),
            mock.patch.object(rv, "print_result"),
            mock.patch.object(rv, "on_failure"),
            mock.patch.object(rr, "post_slack", return_value=True) as slack,
        ):
            outcome = rv.cmd_watch(args, SERVICE, SERVICE.prod, store, mock.Mock())
        return outcome, [" ".join(str(part) for part in call.args) for call in slack.call_args_list]

    def test_unclaimed_release_that_passes_posts_nothing_and_the_author_owns_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(followup={})
            _save(store, record)
            outcome, posts = self._watch(store, record)
            self.assertEqual(outcome, rv.EXIT_OK)
            self.assertFalse([text for text in posts if "owner" in text], posts)
            self.assertFalse([text for text in posts if "<@" in text], posts)
            owner = rr.read_record(store, record["release_id"])["followup"]["owner"]
            self.assertEqual((owner["state"], owner["login"]), ("author", "jonnybcw"))
            self.assertEqual(owner["pr"], 12)

    def test_unclaimed_release_that_fails_mentions_the_author_in_the_failure_alert(self) -> None:
        failed = VerdictTest._result(
            self, result="failed", regressions=[{"kind": "metric", "text": "error rate regressed"}]
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(followup={})
            _save(store, record)
            _, posts = self._watch(store, record, result=failed)
            alerts = [text for text in posts if "FAILED" in text]
            self.assertTrue(alerts, posts)
            self.assertIn("<@UJONNY>", alerts[0])
        # Inconclusive: the last checkpoint could not run.
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(followup={})
            _save(store, record)
            _, posts = self._watch(store, record, error=rr.RecordError("cluster unavailable"))
            alerts = [text for text in posts if "could not run" in text]
            self.assertTrue(alerts, posts)
            self.assertIn("<@UJONNY>", alerts[-1])

    def test_an_agent_claim_is_unchanged(self) -> None:
        failed = VerdictTest._result(
            self, result="failed", regressions=[{"kind": "metric", "text": "error rate regressed"}]
        )
        for result in (None, failed):
            with tempfile.TemporaryDirectory() as tmp:
                store = _store(tmp)
                record = _record()
                claimed = dict(record["followup"]["owner"])
                _save(store, record)
                outcome, posts = self._watch(store, record, result=result)
                self.assertEqual(outcome, rv.EXIT_OK)
                self.assertEqual(rr.read_record(store, record["release_id"])["followup"]["owner"], claimed)
                self.assertFalse([text for text in posts if "<@UJONNY>" in text or "owner" in text], posts)

    def test_an_author_missing_from_the_map_falls_back_and_stays_quiet_on_success(self) -> None:
        people = {"slack_ids": {"someone-else": "UELSE"}, "fallback": ["UFALLBACK"]}
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(followup={})
            _save(store, record)
            outcome, posts = self._watch(store, record, people=people)
            self.assertEqual(outcome, rv.EXIT_OK)
            self.assertFalse([text for text in posts if "owner" in text or "<@" in text], posts)
            self.assertEqual(rr.read_record(store, record["release_id"])["followup"]["owner"]["login"], "jonnybcw")
        failed = VerdictTest._result(
            self, result="failed", regressions=[{"kind": "metric", "text": "error rate regressed"}]
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(followup={})
            _save(store, record)
            _, posts = self._watch(store, record, people=people, result=failed)
            self.assertIn("<@UFALLBACK>", [text for text in posts if "FAILED" in text][0])

    def test_the_author_is_the_newest_pr_not_a_later_direct_commit(self) -> None:
        """A direct or automated commit after the PR never takes ownership from the PR's author."""
        record = _record(commit="d" * 40)
        record["commits"] = [
            {"sha": "c" * 40, "title": "Fix the parser (#12)", "pr": 12, "author": "jonnybcw"},
            {"sha": "d" * 40, "title": "chore: bump", "pr": None, "author": "renovate-bot"},
        ]
        self.assertEqual(rv.ro.release_author(record), ("jonnybcw", 12))
        record["commits"] = [{"sha": "d" * 40, "title": "hotfix", "pr": None, "author": "someone"}]
        self.assertEqual(rv.ro.release_author(record), ("someone", None))

    def test_an_alert_rereads_the_owner_after_a_late_agent_claim(self) -> None:
        """An agent claims the release while the watch sleeps: the alert mentions nobody."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            stale = _record(followup={})
            _save(store, stale)
            rr.update_record(
                store,
                stale["release_id"],
                lambda current: rv.ro.claim_owner(current, "codex://threads/late", "https://github.com/o/r/pull/12"),
                "claimed",
            )
            with mock.patch.object(rv, "load_people", return_value=PEOPLE):
                self.assertEqual(rv.owner_suffix(stale), " <@UJONNY>")
                self.assertEqual(rv.owner_suffix(stale, store), "")

    def _light_watch(self, store, record, *, error=None):
        """Run the Cloud Run watch over +60; return (exit code, every Slack text)."""
        service = rl.parse_light(light_data("cloud_run"))
        args = argparse.Namespace(release_id=record["release_id"], checkpoints="60", retry_completed=False)
        clean = {"problems": [], "parts": {"logs": {"status": "checked"}}, "scope": {}}
        with (
            mock.patch.object(rv, "load_people", return_value=PEOPLE),
            mock.patch.object(rv, "builds_since_rollback", return_value=[]),
            mock.patch.object(rl, "wait_for_deploys", return_value=None),
            mock.patch.object(rl, "replaced_by", return_value=None),
            mock.patch.object(rl, "evaluate_window", side_effect=error, return_value=clean),
            mock.patch.object(rr, "post_slack", return_value=True) as slack,
        ):
            outcome = rl.cmd_watch(args, service, store, mock.Mock())
        return outcome, [" ".join(str(part) for part in call.args) for call in slack.call_args_list]

    def test_light_unclaimed_release_passes_quietly_and_mentions_the_author_when_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(followup={})
            _save(store, record)
            outcome, posts = self._light_watch(store, record)
            self.assertEqual(outcome, rv.EXIT_OK)
            self.assertFalse([text for text in posts if "owner" in text or "<@" in text], posts)
            self.assertEqual(rr.read_record(store, record["release_id"])["followup"]["owner"]["state"], "author")
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            record = _record(followup={})
            _save(store, record)
            _, posts = self._light_watch(store, record, error=rr.RecordError("logging unavailable"))
            alerts = [text for text in posts if "inconclusive" in text]
            self.assertTrue(alerts, posts)
            self.assertIn("<@UJONNY>", alerts[-1])


if __name__ == "__main__":
    unittest.main()
