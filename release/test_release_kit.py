"""Tests of the release kit: config, guards, records, gate, rollback, release-verify and the new-error checks.

Standard library only: `python3 -m unittest discover -s release -p 'test_*.py'`.
They use a fixture service, never the repository's own configs, so they pass in
every repository that vendors the kit.
"""

from __future__ import annotations

import argparse
import json
import os
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
import release_record as rr  # noqa: E402
import release_rollback as rb  # noqa: E402
import release_signals as rs  # noqa: E402
import release_verify as rv  # noqa: E402

REPO = "us-central1-docker.pkg.dev/orbit-search/demo/demo"
NEW = "sha256:" + "1" * 64
OLD = "sha256:" + "2" * 64
COMMIT = "a" * 40
PREVIOUS = "b" * 40


def service_data(**overrides) -> dict:
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
    return {
        "metadata": {"name": name, "generation": 3},
        "spec": {"replicas": replicas, "template": {"spec": {"containers": [{"name": container, "image": image}]}}},
        "status": {"observedGeneration": 3, "updatedReplicas": replicas, "availableReplicas": replicas},
    }


def _pod(name: str, rs_name: str, container: str, digest: str, **extra) -> dict:
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
        name = next(item.name for item in self.env.deployments if item.selector == selector)
        return [item for item in items if item["metadata"]["name"].startswith(f"{name}-")]

    def __call__(self, command: list[str]) -> str:
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
    record = {
        "schema_version": 1,
        "service": "demo",
        "environment": "prod",
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
    return rr.LocalStore(Path(tmp))


def _save(store: rr.LocalStore, record: dict, latest: bool = True) -> None:
    rr.create_record(store, record, "test")
    if latest:
        rr.point_latest(store, record)


# --------------------------------------------------------------------------- config


class ConfigTest(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(SERVICE.prod.deploy_triggers, ("demo-prod",))
        self.assertEqual(SERVICE.dev.deploy_trigger, "demo-dev")
        self.assertEqual(SERVICE.rollback_build_tag, "demo-rollback")
        self.assertEqual(SERVICE.store, "gs://orbit-search-release-records/demo")
        self.assertEqual(PROD.kube_context, "gke_orbit-search_us-central1_prod")
        self.assertTrue(SERVICE.auto_rollback)

    def test_tiers_and_dev(self) -> None:
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
        code = SERVICE.code
        self.assertTrue(code.is_source("src/parser.ts"))
        self.assertFalse(code.is_source("src/parser.test.ts"))
        self.assertFalse(code.is_source("src/__tests__/a.ts"))
        self.assertFalse(code.is_source("docs/a.ts"))
        self.assertTrue(code.is_test("pkg/server_test.go"))
        self.assertTrue(code.is_deploy("k8/prod/deployment.yaml"))

    def test_bad_metric(self) -> None:
        data = service_data()
        data["verify"]["metrics"] = [{"name": "x", "kind": "rate", "ratio": 2, "absolute": 1, "min_samples": 1}]
        with self.assertRaises(rc.ConfigError):
            rc.parse_service(data)


# --------------------------------------------------------------------------- guard


def _build(build_id: str, created: str, status: str = "SUCCESS", **extra) -> guard.Build:
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


class GuardTest(unittest.TestCase):
    def test_turn(self) -> None:
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
        ok = _build("a", "2026-10-21T10:00:00Z", start="2026-10-21T10:00:01Z", finish="2026-10-21T10:09:00Z")
        self.assertEqual(guard.decide_dev([ok])[0], "ok")
        self.assertEqual(guard.decide_dev([])[0], "wait")
        self.assertEqual(guard.decide_dev([_build("a", "2026-10-21T10:00:00Z", "FAILURE")])[0], "failed")
        older = _build("z", "2026-10-21T09:59:00Z", start="2026-10-21T09:59:01Z", finish="2026-10-21T10:05:00Z")
        self.assertEqual(guard.decide_dev([ok, older])[0], "overlap")

    def test_recorded_digest(self) -> None:
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
    return rg.Stores(service=SERVICE, prod=rr.LocalStore(Path(tmp) / "prod"), dev=rr.LocalStore(Path(tmp) / "dev"))


def _verified(stores: rg.Stores, commit: str = COMMIT, digest: str = NEW) -> None:
    stores.dev.write(
        rg.dev_commit_index(commit),
        {"commit": commit, "verdict": {"status": "passed"}, "images": {"app": f"{REPO}@{digest}"}},
        0,
    )


class GateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.stores = _stores(self.tmp.name)
        self.compare = mock.patch.object(rg, "github_compare_status", return_value="ahead")
        self.compare.start()

    def tearDown(self) -> None:
        self.compare.stop()
        self.tmp.cleanup()

    def test_unverified_then_go(self) -> None:
        self.assertEqual(rg.decide(self.stores, COMMIT, "t").status, "unverified")
        _verified(self.stores)
        self.assertTrue(rg.decide(self.stores, COMMIT, "t").go)
        moved = rg.decide(self.stores, COMMIT, "t", images={"app": f"{REPO}@{OLD}"})
        self.assertEqual(moved.status, "unverified")

    def test_freeze(self) -> None:
        _verified(self.stores)
        self.stores.prod.write(rg.FREEZE_OBJECT, {"frozen": True, "by": "n", "reason": "incident"}, 0)
        decision = rg.decide(self.stores, COMMIT, "t")
        self.assertEqual(decision.status, "frozen")
        self.assertIn("unfreeze", decision.resume_hint)
        self.assertEqual(rg.decide_direct(self.stores, COMMIT, "t").status, "frozen")

    def test_hold_and_trailer(self) -> None:
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
        _verified(self.stores)
        args = mock.Mock(commit=COMMIT, build_id="b1", trigger_name="demo-dev", auto_promote="true", dry_run=True)
        with mock.patch.object(rr, "github_token", return_value="t"), mock.patch.object(rr, "post_slack") as slack:
            self.assertEqual(rg.cmd_request_promote(args, self.stores), rg.EXIT_GO)
        self.assertTrue(slack.call_args[0][0].startswith(":test_tube: TEST RUN, nothing is wrong"))
        index, _ = self.stores.dev.read(rg.dev_commit_index(COMMIT))
        self.assertNotIn("promote", index)

    def test_stale(self) -> None:
        _verified(self.stores)
        _save(self.stores.prod, _record(commit="d" * 40))
        with mock.patch.object(rg, "contains", return_value=True):
            self.assertEqual(rg.decide(self.stores, COMMIT, "t").status, "stale")


# --------------------------------------------------------------------------- rollback


class RollbackTest(unittest.TestCase):
    def test_plan_and_apply(self) -> None:
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
    """One build at a time changes prod: deploys from the gate to `finish`, rollbacks during apply."""

    def test_acquire_wait_take_over_release(self) -> None:
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
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            real_write = store.write

            def racing_write(name, data, generation):
                # Another build writes first, at the same generation this one read.
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
        with tempfile.TemporaryDirectory() as tmp:
            stores = _stores(tmp)
            api = mock.Mock()
            rollback = mock.Mock(describe=lambda: "rb1")
            # The rollback starts after the wait and before the lock: the deploy lets it go first.
            api.ongoing_rollbacks.side_effect = [[], [rollback], [rollback], [], []]
            holders: list = []
            real_release = rr.release_lock

            def release(store, holder):
                holders.append(store.read(rr.LOCK_OBJECT)[0]["holder"])
                real_release(store, holder)

            with mock.patch.object(rr, "release_lock", release):
                rg.take_prod_lock(stores, "deploy1", api=api, alive=lambda _: True, sleep=lambda _: None)
            self.assertEqual(holders, ["deploy1"])
            self.assertEqual(stores.prod.read(rr.LOCK_OBJECT)[0]["holder"], "deploy1")

    def test_rollback_apply_holds_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _save(store, _record())
            clients = rr.Clients(env=PROD, runner=FakeCluster(digest=NEW))
            args = mock.Mock(dry_run=False, reason="bad", target="previous", build_id="rb1", expect_latest="")
            args.requested_by = "test"
            seen: list = []

            def apply(*_args):
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
        snapshot = rr.snapshot_workloads(rr.Clients(env=PROD, runner=FakeCluster(digest=NEW)))
        self.assertEqual(rv.check_images(_record(), snapshot).status, "passed")
        snapshot = rr.snapshot_workloads(rr.Clients(env=PROD, runner=FakeCluster(digest=OLD)))
        self.assertEqual(rv.check_images(_record(), snapshot).status, "failed")
        bad = rr.snapshot_workloads(rr.Clients(env=PROD, runner=FakeCluster(waiting="CrashLoopBackOff")))
        self.assertIn("CrashLoopBackOff", " ".join(rv.check_images(_record(), bad).details))

    def test_service_smoke(self) -> None:
        clients = rr.Clients(env=PROD, runner=FakeCluster())
        snapshot = rr.snapshot_workloads(clients)
        calls = []

        def pod_get(namespace: str, pod: str, port: int, path: str) -> tuple[int, bytes]:
            calls.append((namespace, pod, port, path))
            return (200, b"ok") if pod.endswith("-0") else (503, b"down")

        checks = rv.smoke_checks(clients, snapshot, pod_get=pod_get)
        self.assertEqual([check.name for check in checks], ["health", "prod-demo-worker ready"])
        self.assertEqual(checks[0].status, "failed")
        self.assertEqual(len(calls), 2)
        self.assertEqual(checks[1].status, "passed")

    def test_expect(self) -> None:
        self.assertTrue(rv._expect_ok(204, b"", {})[0])
        self.assertFalse(rv._expect_ok(200, b'{"status":"down"}', {"expect_json": {"status": "ok"}})[0])
        self.assertFalse(rv._expect_ok(200, b"<html>", {"contains": "Orbit"})[0])

    def test_public(self) -> None:
        def get(request: urllib.request.Request) -> tuple[int, bytes]:
            return 200, b"<title>Orbit</title>"

        check = rv.check_http_public({"url": "https://example.test/", "contains": "Orbit"}, get)
        self.assertEqual(check.status, "passed")


class FakeDatadog:
    """Scalar answers by query and window: baseline (before 10:00) or candidate."""

    def __init__(self, base: dict[str, dict[str, float]], cand: dict[str, dict[str, float]], hourly=None) -> None:
        self.base, self.cand, self.hourly = base, cand, hourly or {}

    def scalar(self, query: str, aggregator: str, start: datetime, end: datetime) -> dict[str, float]:
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
        dd = FakeDatadog(
            {ERRORS: {"GET /a": 1, "GET /b": 1}, HITS: {"GET /a": 1000, "GET /b": 1000}},
            {ERRORS: {"GET /a": 1, "GET /b": 300}, HITS: {"GET /a": 1000, "GET /b": 1000}},
        )
        with mock.patch.object(rv, "runtime_change", return_value=(True, "src")):
            findings = rv.judge_metrics(SERVICE, dd, _record(), BASELINE, WINDOW, None)
        rates = {f.scope: f.status for f in findings if f.metric == "http_error_rate"}
        self.assertEqual(rates, {"demo GET /a": "ok", "demo GET /b": "exceeded"})

    def test_spread_and_runtime(self) -> None:
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

    def test_final(self) -> None:
        self.assertEqual(rv.final_verdict(SERVICE, self._result())[0], "passed")
        self.assertEqual(rv.final_verdict(SERVICE, self._result(judged=10))[0], "inconclusive")
        data = service_data()
        data["verify"] = {"signals": {"log_containers": ["app"]}}
        quiet = rc.parse_service(data)
        self.assertEqual(rv.final_verdict(quiet, self._result(judged=0))[0], "passed")
        self.assertEqual(rv.final_verdict(quiet, self._result(signals_checked=[]))[0], "inconclusive")

    def test_apply_failed_and_warnings(self) -> None:
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
        record = _record(
            verdict={"status": "failed", "phase": "window_15m", "reasons": ["new log line"]},
            rollback_suggestion={
                "target": {"commit": PREVIOUS},
                "suspected_prs": [
                    {"sha": COMMIT, "pr": 12, "title": "Change the parser (#12)", "author": "jonnybcw", "score": 6}
                ],
            },
        )
        text, blocks = rv.rollback_alert(SERVICE, record, "https://build", "gs://x")
        self.assertIn("<@U095N7BBQKD>", text)
        self.assertIn("Fixes-release: 20261021T100000Z-aaaaaaaaa", json.dumps(blocks))
        text, _ = rv.rollback_alert(SERVICE, record, None, "gs://x")
        self.assertIn("failed release-verify", text)

    def test_dev_alert_test_run(self) -> None:
        record = _record(verdict={"status": "failed", "reasons": ["forced"]})
        text, blocks = rv.dev_verify_alert(SERVICE, record, "https://build", "gs://x", test_run=True)
        self.assertTrue(text.startswith(rv.TEST_RUN_LINE))
        self.assertNotIn("<@", json.dumps(blocks))


class AutoRollbackTest(unittest.TestCase):
    def test_claim_and_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"RELEASE_AUTO_ROLLBACK": "1"}):
            store = _store(tmp)
            record = _record(verdict={"status": "failed", "phase": "deploy"}, rollback_suggestion={"summary": "x"})
            _save(store, record)
            commands = []

            def runner(command: list[str]) -> str:
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
                rr.release_lock(store, "deploy2")  # the gate said "frozen" and freed the lock

            with (
                mock.patch.object(rr, "post_slack", return_value=True),
                mock.patch.object(rr, "build_alive", return_value=lambda _: True),
            ):
                build = rv.start_rollback(
                    SERVICE,
                    store,
                    record,
                    runner=lambda _: "rb1\n",
                    builds=lambda *_: (False, []),
                    follow=False,
                    sleep=sleep,
                )
            self.assertEqual(build, "rb1")

    def test_claim_blocks(self) -> None:
        now = datetime(2026, 10, 21, 12, tzinfo=rr.UTC)
        self.assertFalse(rv.claim_blocks({}, "me", now))
        self.assertTrue(rv.claim_blocks({"rolled_back_by": "x"}, "me", now))
        starting = {"auto_rollback": {"status": "starting", "claim": "other", "at": "2026-10-21T11:59:00Z"}}
        self.assertTrue(rv.claim_blocks(starting, "me", now))
        starting["auto_rollback"]["at"] = "2026-10-21T11:00:00Z"
        self.assertFalse(rv.claim_blocks(starting, "me", now))

    def test_off_for_test_runs(self) -> None:
        with mock.patch.dict(os.environ, {"RELEASE_AUTO_ROLLBACK": ""}):
            self.assertIsNone(rv.start_rollback(SERVICE, None, _record()))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- new errors


class LogTest(unittest.TestCase):
    def test_pattern_and_site(self) -> None:
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
        self.assertEqual(rs.entry_text({"jsonPayload": {"msg": "x failed"}}), "x failed")
        self.assertEqual(rs.entry_text({"jsonPayload": {"error": {"message": "y"}}}), "y")

    def test_count_sites(self) -> None:
        lines = [
            ("2026-10-21T10:30:00Z", f"pod-{index % 3}", "app", "ERROR", f"Failed to load profile {index}")
            for index in range(12)
        ]
        sites = rs.count_sites(rs.count_patterns(lines))
        item = sites["ERROR:app:Failed to load profile"]
        self.assertEqual((item.count, item.spread), (12, 3))


class FailureSeriesTest(unittest.TestCase):
    def test_new_series_and_rate(self) -> None:
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
        names = [f.name for f in rs.functions_of("src/profile.ts", TS_SOURCE)]
        self.assertEqual(names, ["loadProfile", "unrelated"])
        go = rs.functions_of("src/server/search.go", GO_SOURCE)
        self.assertEqual([(f.name, f.start, f.end) for f in go], [("handleSearch", 3, 7), ("helper", 9, 11)])

    def test_trace_clear_suspect(self) -> None:
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
        for name in kit.KIT_FILES:
            self.assertTrue((HERE / name).exists(), name)


# --------------------------------------------------------------------------- review fixes


class ReviewFixTest(unittest.TestCase):
    def test_overlap_ignored_with_recorded_digest(self) -> None:
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
        def build(status: str | None) -> guard.Build:
            steps = [{"id": "build-image", "status": "SUCCESS"}]
            if status is not None:
                steps.append({"id": "release-record-begin", "status": status})
            return guard.Build.from_api({"id": "x", "createTime": "2026-10-21T10:00:00Z", "steps": steps})

        self.assertFalse(build("QUEUED").changing_cluster)
        self.assertTrue(build("WORKING").changing_cluster)
        self.assertTrue(build(None).changing_cluster)  # no marker step: it may change prod

    def test_smoke_validation(self) -> None:
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
        source = "def a():\n    x = 1\n\n    return x\n\nLIMIT = 3\n"
        self.assertEqual([(f.start, f.end) for f in rs.functions_of("src/a.py", source)], [(1, 4)])

    def test_message_match_covers_entry_fields(self) -> None:
        match = rs.message_match("boom")
        for field in ("textPayload", "jsonPayload.msg", "jsonPayload.error", "jsonPayload.err.message"):
            self.assertIn(f'{field}:"boom"', match)

    def test_rollback_target_without_recorded_predecessor(self) -> None:
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
        finding = rv.Finding("m", "s", 1.0, 3.0, 1000, 1000, "limit", "exceeded")
        rv.apply_spread([finding], {"m": {"s": [1.0, 1.1]}})
        self.assertEqual(finding.status, "no_spread")

    def test_prod_state(self) -> None:
        def build(build_id: str, marker: str) -> guard.Build:
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

    def test_single_pod_spread(self) -> None:
        data = service_data()
        data["verify"]["signals"] = {"log_containers": ["app"], "log_min_spread": 1}
        self.assertEqual(rc.parse_service(data).signals.log_min_spread, 1)
        self.assertEqual(SERVICE.signals.log_min_spread, 2)

    def test_alert_command_names_the_target(self) -> None:
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
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _save(store, _record(kind="rollback", rollback={"rolled_back_release": "bad"}))
            with self.assertRaises(rr.RecordError):
                rb.resolve_target(store, "previous")

    def test_gate_waits_for_rollbacks(self) -> None:
        running = [guard.Build.from_api({"id": "rb", "status": "WORKING", "createTime": "2026-10-21T10:00:00Z"})]
        api = mock.Mock()
        api.ongoing_rollbacks.side_effect = [running, running, []]
        sleeps = []
        rg.wait_for_rollbacks(SERVICE, sleep=sleeps.append, api=api)
        self.assertEqual(len(sleeps), 2)


# --------------------------------------------------------------------------- light kinds (Cloud Run, Composer)

import release_light as rl  # noqa: E402


def light_data(kind: str) -> dict:
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


class LightTest(unittest.TestCase):
    def test_parse(self) -> None:
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
        service = rl.parse_light(light_data("composer"))
        record = {"previous_snapshot": {"composer/import_errors": {"errors": None, "error": "HTTP 403"}}}
        check = rl.check_import_errors(
            service, record, {"composer/import_errors": {"errors": None, "error": "HTTP 403"}}
        )
        self.assertEqual((check.status, check.details), ("skipped", ["HTTP 403"]))

    def test_dag_file_is_the_origin(self) -> None:
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
        service = rl.parse_light(light_data("composer"))
        text = kit.rendered(service)["release/services/demo-composer.release-verify.yaml"]
        self.assertIn('--service "${_SERVICE}" watch \\', text)
        self.assertIn('tags: ["demo-composer-release-verify"]', text)

    def test_airflow_lists_read_every_page(self) -> None:
        service = rl.parse_light(light_data("composer"))
        airflow = rl.Airflow.__new__(rl.Airflow)
        runs = [{"dag_id": f"d{index % 3}"} for index in range(250)]

        def get(path):
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
            with mock.patch.object(rr, "post_slack", return_value=True) as slack:
                rl.alert(service, store, saved)
                rl.alert(service, store, rr.read_record(store, newer["release_id"]))
            self.assertEqual(slack.call_count, 1)
            self.assertIn(f"--sha={'e' * 40}", json.dumps(slack.call_args[0][1]))

    def test_watch_waits_for_a_running_sync(self) -> None:
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
        service = rl.parse_light(light_data("composer"))
        line = rv.warning_line(service, _record(), [{"text": "x"}], rr.LocalStore(Path("/tmp")), "gcloud builds x")
        self.assertIn("roll back with gcloud builds x if", line)
        self.assertNotIn("release_rollback.py", line)

    def test_cloud_run_rollback_restores_the_serving_split(self) -> None:
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
                rl.cmd_watch(args, service, store, mock.Mock())
            self.assertEqual(rr.read_record(store, record["release_id"])["verdict"]["status"], "inconclusive")
            self.assertIn("inconclusive", slack.call_args[0][0])

    def test_light_alert_footer_has_no_hold(self) -> None:
        service = rl.parse_light(light_data("cloud_run"))
        record = _record(verdict={"status": "failed", "phase": "deploy", "reasons": ["x"]}, rollback_suggestion=None)
        _, blocks = rv.rollback_alert(service, record, None, "gs://x", command="gcloud run ...")
        self.assertIn("no hold for this service", json.dumps(blocks))


if __name__ == "__main__":
    unittest.main()
