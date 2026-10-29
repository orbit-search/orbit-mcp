"""Tests of the capacity rules: the classifier (Rule 1), one deploy-incomplete finding (Rule 2), rollout
timeouts scaled by replicas (Rule 3), and the build's rollout-wait.

The release cases are shaped like the two false rollbacks of 2026-10-25 (orbit-api 20261025T234959Z-6c128b151:
liveness kills while starting at 4/8; deep-search 20261025T232748Z-6f3a5e7bf: entity-backfill at 63/64, the
later workloads never updated) and must not roll back. Two counter-cases must: the new version crash-loops with
a traceback, and the new ReplicaSet runs a wrong image.

Standard library only; a fake cluster answers every kubectl read and a fake Popen every rollout wait, so
nothing reaches a cluster: `python3 release/run_offline_checks.py -- python -m unittest discover -s release`.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import release_capacity as rcap  # noqa: E402
import release_config as rc  # noqa: E402
import release_record as rr  # noqa: E402
import release_verify as rv  # noqa: E402

REPO = "us-central1-docker.pkg.dev/orbit-search/cap/cap"
NEW = "sha256:" + "1" * 64
OLD = "sha256:" + "2" * 64
WRONG = "sha256:" + "3" * 64
COMMIT = "c" * 40
NOW = datetime(2026, 10, 25, 23, 50, tzinfo=rr.UTC)
REAL_WAIT_ROLLOUTS = rr.wait_rollouts
_NO_REAL_SLEEP = mock.patch.object(rcap, "_sleep", mock.Mock(side_effect=AssertionError("a test slept for real")))


def setUpModule() -> None:
    """A polled wait that a test forgot to put on the fake clock fails at once instead of sleeping."""
    _NO_REAL_SLEEP.start()


def tearDownModule() -> None:
    """Undo setUpModule."""
    _NO_REAL_SLEEP.stop()


def ts(minutes_before: float) -> str:
    """An RFC 3339 time this many minutes before NOW."""
    return (NOW - timedelta(minutes=minutes_before)).strftime("%Y-%m-%dT%H:%M:%SZ")


def service(namespace: str, deployments: list[tuple[str, str]], smoke: list[dict] | None = None) -> rc.Service:
    """A full-tier service with these (name, container) Deployments in prod."""
    items = [{"name": name, "container": container, "selector": f"app={name}"} for name, container in deployments]
    return rc.parse_service(
        {
            "service": "cap",
            "title": "cap",
            "tier": "full",
            "github_repo": "orbit-search/cap",
            "connected_repository": "orbit-search-cap",
            "image": {
                "repository": REPO,
                "registry_location": "us-central1",
                "registry_repository": "cap",
                "package": "cap",
            },
            "triggers": {"dev": ["cap-dev"], "prod": ["cap-prod"], "rollback": "cap-rollback"},
            "environments": {
                "prod": {"namespace": namespace, "deployments": items, "smoke": smoke or []},
                "dev": {
                    "namespace": namespace,
                    "deployments": [{**items[0], "name": "dev-x", "selector": "app=dev-x"}],
                },
            },
            "verify": {"signals": {"log_containers": [deployments[0][1]]}},
            "code": {"source_prefixes": ["src/"], "extensions": [".ts", ".py"]},
        }
    )


# --------------------------------------------------------------------------- kubectl JSON builders


def deployment(name, container, digest, replicas, *, updated=None, available=None, revision=5, **spec) -> dict:
    """A Deployment as kubectl returns it; spec: strategy, grace, readiness, startup."""
    probe = {}
    if spec.get("readiness"):
        probe["readinessProbe"] = spec["readiness"]
    if spec.get("startup"):
        probe["startupProbe"] = spec["startup"]
    return {
        "metadata": {"name": name, "generation": 7, "annotations": {rcap.REVISION: str(revision)}},
        "spec": {
            "replicas": replicas,
            "strategy": spec.get("strategy")
            or {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": 0}},
            "template": {
                "spec": {
                    "terminationGracePeriodSeconds": spec.get("grace", 60),
                    "containers": [{"name": container, "image": f"{REPO}:{COMMIT}@{digest}", **probe}],
                }
            },
        },
        "status": {
            "observedGeneration": 7,
            "updatedReplicas": replicas if updated is None else updated,
            "availableReplicas": replicas if available is None else available,
        },
    }


def replica_set(name, owner, container, digest, revision) -> dict:
    """A ReplicaSet of a Deployment."""
    return {
        "metadata": {
            "name": name,
            "labels": {"app": owner},
            "annotations": {rcap.REVISION: str(revision)},
            "ownerReferences": [{"kind": "Deployment", "name": owner}],
        },
        "spec": {"template": {"spec": {"containers": [{"name": container, "image": f"{REPO}@{digest}"}]}}},
    }


def pod(
    name,
    app,
    rs,
    container,
    digest,
    *,
    ready=True,
    phase="Running",
    restarts=0,
    last=None,
    waiting=None,
    ready_changed=None,
):
    """A pod; last: lastState.terminated; ready_changed: minutes before NOW of the Ready condition's last change."""
    status = {"name": container, "restartCount": restarts, "state": {}}
    if digest:
        status["imageID"] = f"{REPO}@{digest}"
    if last:
        status["lastState"] = {"terminated": last}
    if waiting:
        status["state"] = {"waiting": {"reason": waiting}}
    elif phase == "Running":
        status["state"] = {"running": {"startedAt": ts(2)}}
    conditions = [
        {
            "type": "Ready",
            "status": "True" if ready else "False",
            "lastTransitionTime": ts(ready_changed if ready_changed is not None else 20),
        }
    ]
    if phase == "Pending":
        conditions.append({"type": "PodScheduled", "status": "False", "reason": "Unschedulable"})
    return {
        "metadata": {"name": name, "labels": {"app": app}, "ownerReferences": [{"kind": "ReplicaSet", "name": rs}]},
        "spec": {"nodeName": None if phase == "Pending" else "node-a", "containers": [{"name": container}]},
        "status": {
            "phase": phase,
            "startTime": ts(20),
            "conditions": conditions,
            "containerStatuses": [] if phase == "Pending" else [status],
        },
    }


def event(namespace, pod_name, reason, message, minutes_before=5) -> dict:
    """A pod event."""
    return {
        "metadata": {"namespace": namespace, "name": f"{pod_name}.{reason}"},
        "involvedObject": {"kind": "Pod", "name": pod_name, "namespace": namespace},
        "reason": reason,
        "message": message,
        "lastTimestamp": ts(minutes_before),
    }


def hpa(namespace, name, current, most, least=2) -> dict:
    """An HPA."""
    return {
        "metadata": {"namespace": namespace, "name": name},
        "spec": {"minReplicas": least, "maxReplicas": most},
        "status": {"currentReplicas": current},
    }


def node(name, cores="8") -> dict:
    """A node with this allocatable CPU."""
    return {"metadata": {"name": name}, "status": {"allocatable": {"cpu": cores}}}


def requesting(name, node_name, cpu) -> dict:
    """A running pod that requests this CPU on a node."""
    return {
        "metadata": {"name": name},
        "spec": {"nodeName": node_name, "containers": [{"resources": {"requests": {"cpu": cpu}}}]},
        "status": {"phase": "Running"},
    }


LIVENESS_UNHEALTHY = 'Liveness probe failed: Get "http://10.0.0.1:8080/_health/liveness": context deadline exceeded'
LIVENESS_KILLING = "Container orbit-api failed liveness probe, will be restarted"
STARTUP_LOGS = "loading onnxruntime session\nwarming the embedding model\n"
TRACEBACK_LOGS = (
    'starting\nTraceback (most recent call last):\n  File "/app/main.py", line 3, in <module>\n'
    "ImportError: cannot import name 'Settings'\n"
)


class Cluster:
    """A fake cluster: answers kubectl reads (runner) and rollout waits (popen); records every command."""

    def __init__(self, namespace: str) -> None:
        """An empty cluster."""
        self.namespace = namespace
        self.deployments: dict[str, dict] = {}
        self.replica_sets: list[dict] = []
        self.pods: list[dict] = []
        self.events: list[dict] = []
        self.cluster_events: list[dict] = []
        self.hpas: list[dict] = []
        self.nodes: list[dict] = []
        self.all_pods: list[dict] = []
        self.logs: dict[str, str] = {}
        self.commands: list[list[str]] = []
        # Round n answers the n-th wait of each Deployment (missing: the rollout finished).
        self.rollout_results: list[dict[str, bool]] = []
        self.waits: dict[str, int] = {}
        self.on_wait = None
        # The polled wait after a capacity verdict (release_capacity.poll_rollouts) sleeps on a fake clock.
        self.on_sleep = None
        self.clock = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        """The fake clock advances; on_sleep can change the cluster meanwhile."""
        self.sleeps.append(seconds)
        self.clock += seconds
        if self.on_sleep:
            self.on_sleep()

    def fake_time(self):
        """Patch the polled wait's clock and sleep with this cluster's."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(rcap, "_sleep", self.sleep))
        stack.enter_context(mock.patch.object(rcap, "_clock", lambda: self.clock))
        return stack

    def _labelled(self, items: list[dict], selector: str) -> list[dict]:
        """The items with the selector's app label."""
        app = selector.split("=", 1)[1]
        return [item for item in items if item["metadata"].get("labels", {}).get("app") == app]

    def __call__(self, command: list[str], timeout: float = 120) -> str:
        """Answer one kubectl command."""
        self.commands.append(command)
        assert command[0] == "kubectl", command
        args = [part for part in command[1:] if not part.startswith("--request-timeout")]
        if args[:1] == ["-n"]:
            args = args[2:]
        if args[-2:] == ["-o", "json"]:
            args = args[:-2]
        if args[:2] == ["get", "deployment"]:
            if args[2] not in self.deployments:
                raise rr.RecordError(f"deployments.apps {args[2]!r} NotFound")
            return json.dumps(self.deployments[args[2]])
        if args[:3] == ["get", "pods", "-l"]:
            return json.dumps({"items": self._labelled(self.pods, args[3])})
        if args[:3] == ["get", "replicasets", "-l"]:
            return json.dumps({"items": self._labelled(self.replica_sets, args[3])})
        if args == ["get", "events"]:
            return json.dumps({"items": self.events})
        if args[:3] == ["get", "events", "-A"]:
            return json.dumps({"items": self.cluster_events})
        if args == ["get", "hpa", "-A"]:
            return json.dumps({"items": self.hpas})
        if args == ["get", "nodes"]:
            return json.dumps({"items": self.nodes})
        if args[:3] == ["get", "pods", "-A"]:
            return json.dumps({"items": self.all_pods})
        if args[:1] == ["logs"]:
            if args[1] not in self.logs:
                raise rr.RecordError("logs unavailable")
            return self.logs[args[1]]
        raise AssertionError(f"unexpected command {command}")

    def popen(self, command: list[str], **_: object):
        """A finished `kubectl rollout status` process: the next scripted result for this Deployment."""
        self.commands.append(command)
        name = next(part for part in command if part.startswith("deployment/")).split("/", 1)[1]
        round_ = self.waits.get(name, 0)
        self.waits[name] = round_ + 1
        ok = (self.rollout_results[round_] if round_ < len(self.rollout_results) else {}).get(name, True)
        process = mock.Mock()
        process.returncode = 0 if ok else 1
        process.communicate.return_value = (
            f'deployment "{name}" successfully rolled out\n' if ok else "error: timed out waiting for the condition\n",
            None,
        )
        return process

    def wait_rollouts(self, clients, names, timeout_seconds=900, *, timeouts=None, popen=None):
        """The kit's wait_rollouts with this fake's popen."""
        out = REAL_WAIT_ROLLOUTS(clients, names, timeout_seconds, timeouts=timeouts, popen=self.popen)
        if self.on_wait:
            self.on_wait()
        return out


def busy_cluster(cluster: Cluster) -> None:
    """The capacity of 10-25: HPAs at max, hot nodes, probe kills in other namespaces."""
    cluster.hpas = [hpa("api", "prod-ai-worker", 128, 128, 10), hpa("deep-search", "headless-browser", 100, 100, 10)]
    cluster.nodes = [node(f"n{index}") for index in range(5)]
    cluster.all_pods = [requesting(f"p{index}", f"n{index}", "7800m") for index in range(4)]
    cluster.cluster_events = [
        event("kube-system", "csi-1", "Killing", "Container csi failed liveness probe, will be restarted", 30),
        event("deep-search", "w-1", "Killing", "Container worker failed liveness probe, will be restarted", 40),
    ]


def quiet_cluster(cluster: Cluster) -> None:
    """No capacity indicator: no HPA at max, cool nodes, no probe kills elsewhere."""
    cluster.hpas = [hpa("api", "prod-ai-worker", 40, 128, 10), hpa("api", "pinned", 2, 2, 2)]
    cluster.nodes = [node(f"n{index}") for index in range(5)]
    cluster.all_pods = [requesting(f"p{index}", f"n{index}", "2") for index in range(5)]
    cluster.cluster_events = []


# --------------------------------------------------------------------------- the two incidents


ORBIT_API = service("api", [("prod-orbit-api", "orbit-api")])


def orbit_api_incident(*, logs: str = STARTUP_LOGS, last_exit: int = 137, new_digest: str = NEW) -> Cluster:
    """orbit-api 20261025T234959Z-6c128b151: rollout timed out at 4/8 updated; 2 new pods Ready on the
    release image, 2 killed again and again by the liveness probe while starting (no startupProbe)."""
    cluster = Cluster("api")
    name, container = "prod-orbit-api", "orbit-api"
    cluster.deployments[name] = deployment(
        name,
        container,
        new_digest,
        8,
        updated=4,
        available=6,
        grace=60,
        readiness={"initialDelaySeconds": 20, "periodSeconds": 5, "failureThreshold": 1},
    )
    cluster.replica_sets = [
        replica_set("orbit-api-new", name, container, new_digest, 5),
        replica_set("orbit-api-old", name, container, OLD, 4),
    ]
    cluster.pods = [pod(f"orbit-api-new-ok{index}", name, "orbit-api-new", container, new_digest) for index in range(2)]
    for index in range(2):
        pod_name = f"orbit-api-new-killed{index}"
        cluster.pods.append(
            pod(
                pod_name,
                name,
                "orbit-api-new",
                container,
                new_digest,
                ready=False,
                restarts=6,
                last={"exitCode": last_exit, "reason": "Error", "finishedAt": ts(1)},
                waiting="CrashLoopBackOff",
            )
        )
        cluster.events += [
            event("api", pod_name, "Unhealthy", LIVENESS_UNHEALTHY, 2),
            event("api", pod_name, "Killing", LIVENESS_KILLING, 2),
        ]
        cluster.logs[pod_name] = logs
    cluster.pods += [pod(f"orbit-api-old{index}", name, "orbit-api-old", container, OLD) for index in range(4)]
    busy_cluster(cluster)
    return cluster


DEEP_SEARCH = service(
    "deep-search",
    [
        ("prod-deep-search-v3-worker-deployment-entity-backfill", "worker"),
        ("prod-wal-delivery", "wal"),
        ("prod-wal-readiness", "wal"),
        ("prod-wal-compactor", "wal"),
    ],
)
ENTITY = "prod-deep-search-v3-worker-deployment-entity-backfill"
LATER = ["prod-wal-compactor", "prod-wal-delivery", "prod-wal-readiness"]


def deep_search_incident(*, pending_reason: str = "FailedScheduling") -> Cluster:
    """deep-search 20261025T232748Z-6f3a5e7bf: step 12 timed out with entity-backfill at 63/64 (one new pod
    waits for a node); the WAL workloads of the later steps never got the release image."""
    cluster = Cluster("deep-search")
    cluster.deployments[ENTITY] = deployment(
        ENTITY,
        "worker",
        NEW,
        64,
        updated=64,
        available=63,
        grace=600,
        strategy={"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": "25%"}},
    )
    cluster.replica_sets.append(replica_set("entity-new", ENTITY, "worker", NEW, 5))
    cluster.pods += [pod(f"entity-new-{index}", ENTITY, "entity-new", "worker", NEW) for index in range(63)]
    cluster.pods.append(pod("entity-new-pending", ENTITY, "entity-new", "worker", None, ready=False, phase="Pending"))
    if pending_reason != "FailedScheduling":
        cluster.pods[-1]["status"]["conditions"][1].update(status="True", reason=None)
    message = (
        "0/73 nodes are available: 43 Insufficient cpu, 67 Insufficient memory."
        if pending_reason == "FailedScheduling"
        else "pod has unbound immediate PersistentVolumeClaims"
    )
    cluster.events.append(event("deep-search", "entity-new-pending", pending_reason, message, 3))
    for name in LATER:
        cluster.deployments[name] = deployment(name, "wal", OLD, 1)
        cluster.replica_sets.append(replica_set(f"{name}-rs", name, "wal", OLD, 3))
        cluster.pods.append(pod(f"{name}-0", name, f"{name}-rs", "wal", OLD))
    busy_cluster(cluster)
    return cluster


def record_for(svc: rc.Service, **overrides) -> dict:
    """A prod release record of the release image NEW over OLD."""
    record = {
        "schema_version": 1,
        "service": "cap",
        "environment": "prod",
        "release_id": "20261025T234959Z-ccccccccc",
        "kind": "promote",
        "commit": COMMIT,
        "status": "deployed",
        "times": {"deploy_started": ts(30), "deploy_finished": ts(5)},
        "images": {"app": f"{REPO}@{NEW}"},
        "previous": {
            "release_id": "20261025T200000Z-bbbbbbbbb",
            "commit": "b" * 40,
            "images": {"app": f"{REPO}@{OLD}"},
            "workloads": {f"deployment/{item.name}": {"digest": OLD} for item in svc.prod.deployments},
        },
        "commits": [{"sha": COMMIT, "title": "Hold an owner's discovery budget (#1564)", "pr": 1564, "author": "x"}],
        "checks": {},
        "verdict": {"status": "pending"},
        "rollback_suggestion": None,
    }
    record.update(overrides)
    return record


def deploy_check(svc: rc.Service, cluster: Cluster, record: dict, *, wait_seconds: int = 300) -> dict:
    """release_verify's deploy phase against the fake cluster (no smoke checks, no Datadog, no store)."""
    clients = rr.Clients(env=svc.prod, runner=cluster)
    with (
        mock.patch.object(rr, "wait_rollouts", side_effect=cluster.wait_rollouts),
        cluster.fake_time(),
        redirect_stdout(io.StringIO()),
    ):
        return rv.evaluate(
            svc, record, phase="deploy", clients=clients, dd=None, now=NOW, wait_rollouts_seconds=wait_seconds
        )


def finish_on_second_wait(cluster: Cluster) -> None:
    """The first wait times out; during the polled second wait all 8 orbit-api pods become Ready on the release."""
    cluster.rollout_results = [{"prod-orbit-api": False}]

    def finish() -> None:
        """The rollout completes while the second wait polls."""
        cluster.deployments["prod-orbit-api"]["status"].update(updatedReplicas=8, availableReplicas=8)
        cluster.pods = [
            pod(f"orbit-api-new-{i}", "prod-orbit-api", "orbit-api-new", "orbit-api", NEW) for i in range(8)
        ]

    cluster.on_sleep = finish


def saved_verdict(svc: rc.Service, record: dict, result: dict) -> dict:
    """The record after apply_result: the verdict and the rollback suggestion a failure would get."""
    with tempfile.TemporaryDirectory() as tmp:
        store = rr.LocalStore(Path(tmp))
        rr.create_record(store, record, "test")
        return rv.apply_result(svc, store, record, result, final=False, token=None)


# --------------------------------------------------------------------------- Rule 3


class RolloutTimeoutTest(unittest.TestCase):
    def test_the_incident_deployments(self) -> None:
        """orbit-api (8), entity-backfill (64) and ai-worker (128) with their prod strategies and probes."""
        api = deployment(
            "a", "c", NEW, 8, grace=60, readiness={"initialDelaySeconds": 20, "periodSeconds": 5, "failureThreshold": 1}
        )
        self.assertEqual(rcap.rollout_plan(api)["waves"], 4)
        self.assertEqual(rcap.rollout_timeout(api), 1020)
        entity = deployment(
            "e",
            "c",
            NEW,
            64,
            grace=600,
            strategy={"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": "25%"}},
        )
        self.assertEqual(rcap.rollout_timeout(entity), 3180)
        worker = deployment(
            "w",
            "c",
            NEW,
            128,
            grace=60,
            startup={"periodSeconds": 5, "failureThreshold": 36},
            readiness={"initialDelaySeconds": 5, "periodSeconds": 5, "failureThreshold": 2},
        )
        self.assertEqual(rcap.rollout_timeout(worker), 1260)

    def test_scaling_and_rounding(self) -> None:
        """Surge rounds up, unavailable down; one pod per wave scales to the cap; Recreate is one wave."""
        self.assertEqual(rcap.resolve_count("25%", 10, round_up=True), 3)
        self.assertEqual(rcap.resolve_count("25%", 10, round_up=False), 2)
        one = {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}
        timeouts = [
            rcap.rollout_timeout(deployment("d", "c", NEW, count, grace=30, strategy=one)) for count in (8, 64, 128)
        ]
        self.assertEqual(timeouts, [1500, 9900, rcap.ROLLOUT_CAP_SECONDS])
        recreate = deployment("d", "c", NEW, 128, grace=30, strategy={"type": "Recreate"})
        self.assertEqual(rcap.rollout_plan(recreate)["waves"], 1)
        defaults = deployment("d", "c", NEW, 128, grace=30, strategy={"type": "RollingUpdate"})
        self.assertEqual(rcap.rollout_plan(defaults)["per_wave_pods"], 64)
        self.assertEqual(rcap.rollout_timeout(defaults), rcap.ROLLOUT_BASE_SECONDS)

    def test_scaled_timeouts_less_elapsed(self) -> None:
        """The helper both waits use: the timeout x factor less the time spent, never below the minimum."""
        cluster = orbit_api_incident()
        clients = rr.Clients(env=ORBIT_API.prod, runner=cluster)
        self.assertEqual(rr.scaled_rollout_timeouts(clients, ["prod-orbit-api"], minimum=300), {"prod-orbit-api": 1020})
        self.assertEqual(
            rr.scaled_rollout_timeouts(clients, ["prod-orbit-api"], minimum=120, factor=2, elapsed=1800),
            {"prod-orbit-api": 240},
        )
        self.assertEqual(rr.scaled_rollout_timeouts(clients, ["missing"], minimum=0), {"missing": 900})


# --------------------------------------------------------------------------- Rule 1: the pure classifier


def evidence(cluster: Cluster, names: list[str], svc: rc.Service, release: str = NEW) -> dict:
    """The classifier's input, read from the fake cluster by gather()."""
    return rcap.gather(rr.Clients(env=svc.prod, runner=cluster), names, release, NOW)


class ClassifierTest(unittest.TestCase):
    def test_orbit_api_liveness_kills_while_starting_are_capacity(self) -> None:
        verdict = rcap.classify(evidence(orbit_api_incident(), ["prod-orbit-api"], ORBIT_API))
        self.assertEqual(verdict["verdict"], "capacity", verdict)
        self.assertEqual((verdict["status"], verdict["reason"]), ("environmental", "capacity"))
        self.assertEqual(verdict["workloads"]["prod-orbit-api"]["killed_while_starting"], 2)
        self.assertEqual(verdict["workloads"]["prod-orbit-api"]["ready"], 2)
        self.assertTrue(any("HPAs at max" in line for line in verdict["indicators"]))

    def test_deep_search_pending_pod_is_capacity(self) -> None:
        verdict = rcap.classify(evidence(deep_search_incident(), [ENTITY], DEEP_SEARCH))
        self.assertEqual(verdict["verdict"], "capacity", verdict)
        self.assertTrue(any("FailedScheduling" in line for line in verdict["indicators"]))

    def test_a_traceback_is_the_release(self) -> None:
        """Probe kills, but the logs show a traceback before the kill."""
        verdict = rcap.classify(evidence(orbit_api_incident(logs=TRACEBACK_LOGS), ["prod-orbit-api"], ORBIT_API))
        self.assertEqual(verdict["verdict"], "not_capacity")
        self.assertIn("ImportError", " ".join(verdict["blockers"]))

    def test_another_exit_code_is_the_release(self) -> None:
        verdict = rcap.classify(evidence(orbit_api_incident(last_exit=1), ["prod-orbit-api"], ORBIT_API))
        self.assertEqual(verdict["verdict"], "not_capacity")
        self.assertIn("exit 1", " ".join(verdict["blockers"]))

    def test_oom_and_image_pull_are_the_release(self) -> None:
        cluster = orbit_api_incident()
        cluster.pods[2]["status"]["containerStatuses"][0]["lastState"]["terminated"]["reason"] = "OOMKilled"
        cluster.pods[3]["status"]["containerStatuses"][0]["state"] = {"waiting": {"reason": "ImagePullBackOff"}}
        blockers = " ".join(rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))["blockers"])
        self.assertIn("OOMKilled", blockers)
        self.assertIn("ImagePullBackOff", blockers)

    def test_wrong_image_is_the_release(self) -> None:
        verdict = rcap.classify(evidence(orbit_api_incident(new_digest=WRONG), ["prod-orbit-api"], ORBIT_API))
        self.assertEqual(verdict["verdict"], "not_capacity")
        self.assertIn(WRONG, " ".join(verdict["blockers"]))

    def test_no_indicator_is_not_capacity(self) -> None:
        cluster = orbit_api_incident()
        quiet_cluster(cluster)
        verdict = rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))
        self.assertEqual(verdict["verdict"], "not_capacity")
        self.assertEqual(verdict["blockers"], [])
        self.assertEqual(verdict["indicators"], [])

    def test_each_indicator_alone(self) -> None:
        """Hot nodes, probe kills on the old version, or kills in two other namespaces each suffice."""
        cluster = orbit_api_incident()
        quiet_cluster(cluster)
        cluster.all_pods = [requesting(f"p{index}", f"n{index}", "7700m") for index in range(3)]
        self.assertIn(
            "node CPU", " ".join(rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))["indicators"])
        )
        quiet_cluster(cluster)
        cluster.events.append(event("api", "orbit-api-old0", "Killing", LIVENESS_KILLING, 60))
        self.assertIn(
            "old version", " ".join(rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))["indicators"])
        )
        cluster.events.pop()
        cluster.cluster_events = [
            event("kube-system", "a", "Killing", "Container a failed liveness probe, will be restarted", 30),
            event("company-intel", "b", "Killing", "Container b failed startup probe, will be restarted", 30),
            event("company-intel", "c", "Killing", "Container c failed liveness probe, will be restarted", 400),
        ]
        indicators = rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))["indicators"]
        self.assertEqual(len(indicators), 1)
        self.assertIn("2 other namespaces", indicators[0])

    def test_a_pinned_hpa_is_not_at_max(self) -> None:
        self.assertEqual(rcap.hpas_at_max([hpa("x", "pinned", 2, 2, 2), hpa("x", "full", 10, 10, 2)]), ["x/full 10/10"])

    def test_killed_while_running_and_not_ready_is_not_explained(self) -> None:
        """Probe kills after a long run, with most new pods not Ready: not a slow start."""
        cluster = orbit_api_incident()
        cluster.pods[1] = copy.deepcopy(cluster.pods[2])
        cluster.pods[1]["metadata"]["name"] = "orbit-api-new-killed2"
        cluster.events += [event("api", "orbit-api-new-killed2", "Killing", LIVENESS_KILLING, 2)]
        cluster.logs["orbit-api-new-killed2"] = STARTUP_LOGS
        for item in cluster.pods[1:4]:
            item["status"]["containerStatuses"][0]["lastState"]["terminated"].update(
                startedAt=ts(200), finishedAt=ts(2)
            )
        verdict = rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))
        self.assertEqual(verdict["verdict"], "not_capacity")
        self.assertEqual(verdict["workloads"]["prod-orbit-api"]["killed_while_running"], 3)
        self.assertIn("1/4 new pods that ran are Ready", " ".join(verdict["blockers"]))

    def test_a_preempted_new_pod_is_node_pressure(self) -> None:
        """Calibration 10-26: an entity-backfill pod "preempted by Kubelet to accommodate a critical pod"
        (phase Failed, exit 143) is capacity evidence, not an application failure."""
        cluster = deep_search_incident()
        quiet_cluster(cluster)
        cluster.events = []
        cluster.pods[-1] = pod("entity-new-preempted", ENTITY, "entity-new", "worker", NEW, ready=False, phase="Failed")
        cluster.pods[-1]["status"]["containerStatuses"][0]["state"] = {
            "terminated": {"exitCode": 143, "reason": "Error"}
        }
        cluster.pods[-1]["status"]["conditions"].append(
            {
                "type": "DisruptionTarget",
                "status": "True",
                "message": "Pod was preempted by Kubelet to accommodate a critical pod.",
            }
        )
        verdict = rcap.classify(evidence(cluster, [ENTITY], DEEP_SEARCH))
        self.assertEqual(verdict["verdict"], "capacity", verdict)
        self.assertIn("preempted by Kubelet", " ".join(verdict["indicators"]))
        self.assertEqual(verdict["workloads"][ENTITY]["displaced"], 1)

    def test_killed_while_starting_then_ready(self) -> None:
        """Pods killed by their liveness probe and Ready since count as Ready."""
        cluster = orbit_api_incident()
        for item in cluster.pods[2:4]:
            item["spec"]["containers"][0]["livenessProbe"] = {"initialDelaySeconds": 30, "periodSeconds": 10}
            item["status"]["conditions"][0].update(status="True", lastTransitionTime=ts(1))
            state = item["status"]["containerStatuses"][0]
            state["state"] = {"running": {"startedAt": ts(2)}}
            state["lastState"]["terminated"].update(startedAt=ts(4), finishedAt=ts(4 - 65 / 60))
        verdict = rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))
        self.assertEqual(verdict["verdict"], "capacity", verdict)
        self.assertEqual(verdict["workloads"]["prod-orbit-api"]["ready"], 4)
        # orbit-api #1573: a pod killed by a probe after its start and Ready again is Ready, not a failure.
        for item in cluster.pods[2:4]:
            item["status"]["containerStatuses"][0]["lastState"]["terminated"]["startedAt"] = ts(200)
        verdict = rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))
        self.assertEqual(verdict["verdict"], "capacity", verdict)
        self.assertEqual(verdict["blockers"], [])

    def test_every_attempt_counts_current_first(self) -> None:
        """deep-search #2379: a current run that exits 1 is not hidden behind an earlier probe kill."""
        cluster = orbit_api_incident()
        state = cluster.pods[2]["status"]["containerStatuses"][0]
        state["state"] = {"terminated": {"exitCode": 1, "reason": "Error", "finishedAt": ts(0.5)}}
        verdict = rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))
        self.assertEqual(verdict["verdict"], "not_capacity")
        self.assertIn("orbit-api-new-killed0 exit 1 (Error)", " ".join(verdict["blockers"]))
        # The current run's logs are read (no --previous), since that container has ended.
        reads = [command for command in cluster.commands if "logs" in command and "orbit-api-new-killed0" in command]
        self.assertNotIn("--previous", reads[-1])

    def test_sigterm_handler_exits_count_within_the_grace_period(self) -> None:
        """orbit-api #1573: exit 0 or 139 is a probe kill only when the run ended within its grace after the kill."""
        for code in (0, 139):
            cluster = orbit_api_incident()
            for item in cluster.pods[2:4]:
                item["status"]["containerStatuses"][0]["lastState"]["terminated"].update(
                    exitCode=code, startedAt=ts(4), finishedAt=ts(3)
                )
            cluster.events = [
                event("api", f"orbit-api-new-killed{index}", "Killing", LIVENESS_KILLING, 3.5) for index in range(2)
            ]
            self.assertEqual(rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))["verdict"], "capacity")
            # The kill came 10 min before the end: the container exited on its own.
            for item in cluster.events:
                item["lastTimestamp"] = ts(13)
            for item in cluster.pods[2:4]:
                item["status"]["containerStatuses"][0]["lastState"]["terminated"]["startedAt"] = ts(14)
            verdict = rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))
            self.assertEqual(verdict["verdict"], "not_capacity", code)
            self.assertIn(f"exit {code}", " ".join(verdict["blockers"]))

    def test_a_failed_probe_alone_does_not_explain_exit_0_or_139(self) -> None:
        """deep-search #2383: only the kubelet's Killing event sends SIGTERM. A failed probe (Unhealthy, maybe below
        the failure threshold) right before an exit 0 or a segfault does not make it a probe kill."""
        for code in (0, 139):
            cluster = orbit_api_incident()
            for item in cluster.pods[2:4]:
                item["status"]["containerStatuses"][0]["lastState"]["terminated"].update(
                    exitCode=code, startedAt=ts(4), finishedAt=ts(3)
                )
            cluster.events = [
                event("api", f"orbit-api-new-killed{index}", "Unhealthy", LIVENESS_UNHEALTHY, 3.5) for index in range(2)
            ]
            verdict = rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))
            self.assertEqual(verdict["verdict"], "not_capacity", code)
            self.assertIn(f"orbit-api-new-killed0 exit {code} (Error)", " ".join(verdict["blockers"]))
            # The kubelet's kill for the failed probe, 30 s before the end: a probe kill.
            cluster.events += [
                event("api", f"orbit-api-new-killed{index}", "Killing", LIVENESS_KILLING, 3.5) for index in range(2)
            ]
            self.assertEqual(rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))["verdict"], "capacity")
        # Exit 137 with only the failed probe inside the run stays a probe kill (SIGKILL ends it either way).
        cluster = orbit_api_incident()
        for item in cluster.pods[2:4]:
            item["status"]["containerStatuses"][0]["lastState"]["terminated"].update(startedAt=ts(4), finishedAt=ts(3))
        cluster.events = [
            event("api", f"orbit-api-new-killed{i}", "Unhealthy", LIVENESS_UNHEALTHY, 3.5) for i in range(2)
        ]
        self.assertEqual(rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))["verdict"], "capacity")

    def test_a_kill_outside_the_run_does_not_explain_exit_137(self) -> None:
        cluster = orbit_api_incident()
        for item in cluster.pods[2:4]:
            item["status"]["containerStatuses"][0]["lastState"]["terminated"].update(startedAt=ts(4), finishedAt=ts(3))
        for item in cluster.events:
            item["lastTimestamp"] = ts(60)
        verdict = rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))
        self.assertEqual(verdict["verdict"], "not_capacity")
        self.assertIn("exit 137", " ".join(verdict["blockers"]))

    def test_crash_logs_past_the_sample_block(self) -> None:
        """deep-search #2379: a crashed new pod whose logs the check did not read blocks the verdict."""
        cluster = orbit_api_incident()
        with mock.patch.object(rcap, "MAX_LOG_READS", 1):
            verdict = rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))
        self.assertEqual(verdict["verdict"], "not_capacity")
        self.assertIn("orbit-api-new-killed1 crashed and its logs could not be read", " ".join(verdict["blockers"]))

    def test_poll_reads_are_bounded_by_the_time_left(self) -> None:
        """deep-search #2379: the wait after a capacity verdict polls; no read outlives the Deployment's deadline."""
        cluster = orbit_api_incident()
        clients = rr.Clients(env=ORBIT_API.prod, runner=cluster)
        with cluster.fake_time(), redirect_stdout(io.StringIO()):
            left = rcap.poll_rollouts(clients, {"prod-orbit-api": 45})
        bounds = [part for command in cluster.commands for part in command if part.startswith("--request-timeout=")]
        self.assertEqual(bounds, ["--request-timeout=30s", "--request-timeout=15s"])
        self.assertEqual(cluster.sleeps, [30, 15])
        self.assertEqual(left, ["deployment/prod-orbit-api rollout did not complete: 4/8 updated, 6 available"])

    def test_crash_loop_without_termination_details_still_checks_logs(self) -> None:
        """Greptile #500: CrashLoopBackOff with restarts but no kept termination details: its logs still decide."""
        cluster = orbit_api_incident(logs=TRACEBACK_LOGS)
        for item in cluster.pods[2:4]:
            item["status"]["containerStatuses"][0].pop("lastState")
        verdict = rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))
        self.assertEqual(verdict["verdict"], "not_capacity")
        self.assertIn("ImportError", " ".join(verdict["blockers"]))

    def test_a_deadline_that_passes_before_any_read_is_a_failure(self) -> None:
        """Greptile #500: a Deployment never read before its deadline is reported, not dropped."""
        cluster = orbit_api_incident()

        def slow(command, timeout=120):
            """Each read takes 20 s on the fake clock."""
            cluster.clock += 20
            return cluster(command, timeout)

        with cluster.fake_time(), redirect_stdout(io.StringIO()):
            left = rcap.poll_rollouts(rr.Clients(env=ORBIT_API.prod, runner=slow), {"prod-orbit-api": 60, "other": 10})
        self.assertIn("deployment/other rollout did not complete: not read before its deadline", left)
        self.assertEqual(len(left), 2)

    def test_unreadable_pods_or_no_new_pods_are_not_capacity(self) -> None:
        """Greptile #499: a failed pod read, or a new ReplicaSet without pods, never counts as capacity."""
        cluster = orbit_api_incident()
        real = cluster.__call__

        def no_pods(command, timeout=120):
            """Pod reads of the workload fail."""
            if "-l" in command and "pods" in command:
                raise rr.RecordError("the server was unable to return a response")
            return real(command, timeout)

        found = rcap.gather(rr.Clients(env=ORBIT_API.prod, runner=no_pods), ["prod-orbit-api"], NEW, NOW)
        verdict = rcap.classify(found)
        self.assertEqual(verdict["verdict"], "not_capacity")
        self.assertIn("could not be read", " ".join(verdict["blockers"]))
        cluster.pods = [item for item in cluster.pods if "new" not in item["metadata"]["name"]]
        verdict = rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))
        self.assertEqual(verdict["verdict"], "not_capacity")
        self.assertIn("has no pods", " ".join(verdict["blockers"]))

    def test_unreadable_logs_of_a_crashed_pod_are_not_capacity(self) -> None:
        cluster = orbit_api_incident()
        cluster.logs.clear()
        verdict = rcap.classify(evidence(cluster, ["prod-orbit-api"], ORBIT_API))
        self.assertEqual(verdict["verdict"], "not_capacity")
        self.assertIn("logs could not be read", " ".join(verdict["blockers"]))

    def test_node_cpu_shares(self) -> None:
        nodes = [node("a", "4"), node("b", "2")]
        pods = [requesting("x", "a", "3"), requesting("y", "b", "500m"), requesting("z", None, "9")]
        pods.append({**requesting("done", "a", "1"), "status": {"phase": "Succeeded"}})
        self.assertEqual(rcap.node_cpu_shares(nodes, pods), [0.25, 0.75])

    def test_gather_reads_only(self) -> None:
        cluster = orbit_api_incident()
        evidence(cluster, ["prod-orbit-api"], ORBIT_API)
        verbs = {command[command.index("-n") + 2] if "-n" in command else command[1] for command in cluster.commands}
        self.assertEqual(verbs, {"get", "logs"})


# --------------------------------------------------------------------------- release cases (deploy phase)


class ReleaseCaseTest(unittest.TestCase):
    """The two false rollbacks give no rollback; a traceback and a wrong image still roll back."""

    def test_orbit_api_incident_no_rollback(self) -> None:
        cluster = orbit_api_incident()
        cluster.rollout_results = [{"prod-orbit-api": False}, {"prod-orbit-api": False}]
        record = record_for(ORBIT_API)
        result = deploy_check(ORBIT_API, cluster, record)
        self.assertEqual(result["regressions"], [])
        self.assertEqual((result["result"], result["cause"]), ("inconclusive", "capacity"))
        statuses = {check["name"]: check["status"] for check in result["checks"]}
        self.assertEqual(statuses, {"images": "environmental", "rollouts": "environmental"})
        # The first wait: at least 300 s (the build already spent 30 min). Then the polled wait (deep-search
        # #2379: rollout status fails at once after the progress deadline): Rule 3 x2 less those 30 min.
        waits = [part for command in cluster.commands for part in command if part.startswith("--timeout=")]
        self.assertEqual(waits, ["--timeout=300s"])
        self.assertEqual(result["capacity"]["extended_wait_seconds"], {"prod-orbit-api": 240})
        self.assertEqual(sum(cluster.sleeps), 240)
        saved = saved_verdict(ORBIT_API, record, result)
        self.assertEqual((saved["verdict"]["status"], saved["verdict"]["cause"]), ("inconclusive", "capacity"))
        self.assertIsNone(saved["rollback_suggestion"])
        with tempfile.TemporaryDirectory() as tmp:
            store = rr.LocalStore(Path(tmp))
            line = rv.result_detail(ORBIT_API, saved, result, store)
            with mock.patch.object(rr, "post_slack", return_value=True) as slack:
                rv.post_result(ORBIT_API, store, saved, result)
        self.assertIn("no rollback", line)
        # Capacity is environmental: a warning line that mentions nobody.
        self.assertEqual(
            slack.call_args[0][0], rr.status_line("warning", ORBIT_API.name, release=rr.release_label(saved))
        )

    def test_the_capacity_wait_counts_the_first_wait(self) -> None:
        """Greptile #499: the first wait took 10 min, so the capacity wait gets what is left of 2 x 1,020 s."""
        cluster = orbit_api_incident()
        cluster.rollout_results = [{"prod-orbit-api": False}]
        ticks = iter([0.0, 600.0])
        with mock.patch.object(rv.time, "monotonic", side_effect=lambda: next(ticks, 600.0)):
            result = deploy_check(ORBIT_API, cluster, record_for(ORBIT_API, times={"deploy_started": ts(15)}))
        waits = [part for command in cluster.commands for part in command if part.startswith("--timeout=")]
        # First: 1,020 - 900 s spent before the check -> the 300 s floor. Then 2 x 1,020 - (900 + 600) = 540 s.
        self.assertEqual(waits, ["--timeout=300s"])
        self.assertEqual(result["capacity"]["extended_wait_seconds"], {"prod-orbit-api": 540})

    def test_orbit_api_incident_finishes_after_the_extended_wait(self) -> None:
        cluster = orbit_api_incident()
        finish_on_second_wait(cluster)
        result = deploy_check(ORBIT_API, cluster, record_for(ORBIT_API))
        self.assertEqual(result["result"], "passed")
        self.assertTrue(result["capacity"]["finished"])
        self.assertNotIn("cause", result)

    def test_an_old_build_marked_deploy_failed_for_capacity(self) -> None:
        """A build without rollout-wait wrote deploy_failed on the timeout; the rollout then finished."""
        cluster = orbit_api_incident()
        finish_on_second_wait(cluster)
        result = deploy_check(ORBIT_API, cluster, record_for(ORBIT_API, status="deploy_failed"))
        self.assertEqual(result["regressions"], [])
        self.assertEqual((result["result"], result["cause"]), ("inconclusive", "capacity"))
        self.assertEqual(result["capacity"]["environmental_checks"], ["deploy"])

    def test_an_unnamed_failure_of_a_step_naming_build_is_never_capacity(self) -> None:
        """A build that names its failed steps (finish --failed-step-file) and still wrote deploy_failed without
        one failed outside a rollout wait (an apply, a script): capacity elsewhere never excuses it."""
        cluster = orbit_api_incident()
        finish_on_second_wait(cluster)
        record = record_for(ORBIT_API, status="deploy_failed", deploy_steps_named=True)
        result = deploy_check(ORBIT_API, cluster, record)
        self.assertEqual(result["result"], "failed")
        self.assertEqual(result["capacity"]["verdict"], "not_capacity")
        self.assertIn("outside a rollout wait", result["capacity"]["summary"])
        self.assertEqual(cluster.sleeps, [])

    def test_deep_search_incident_one_finding_and_no_rollback(self) -> None:
        cluster = deep_search_incident()
        cluster.rollout_results = [{ENTITY: False}]
        # The build of 10-25 wrote deploy_failed without a step id (its rollout wait had no capacity verdict).
        record = record_for(DEEP_SEARCH, status="deploy_failed")
        result = deploy_check(DEEP_SEARCH, cluster, record)
        deploy = next(check for check in result["checks"] if check["name"] == "deploy")
        self.assertEqual(
            deploy["details"][0], f"deploy incomplete at step unrecorded: 3 workloads not updated ({', '.join(LATER)})"
        )
        self.assertEqual(deploy["status"], "environmental")
        images = next(check for check in result["checks"] if check["name"] == "images")
        self.assertFalse(
            [line for line in images["details"] if line.startswith(tuple(f"deployment/{name}" for name in LATER))]
        )
        self.assertEqual(result["regressions"], [])
        self.assertEqual(result["cause"], "capacity")
        self.assertIsNone(saved_verdict(DEEP_SEARCH, record, result)["rollback_suggestion"])

    def test_a_named_failed_step_is_never_excused_by_capacity(self) -> None:
        """deep-search #2379: a step that names itself failed for a cause other than capacity (rollout-wait names
        a step only after a not-capacity verdict); capacity evidence on another workload does not excuse it."""
        cluster = deep_search_incident()
        cluster.rollout_results = [{ENTITY: False}]
        record = record_for(DEEP_SEARCH, status="deploy_failed", failed_step={"id": "12", "deployments": [ENTITY]})
        result = deploy_check(DEEP_SEARCH, cluster, record)
        self.assertEqual(result["result"], "failed")
        self.assertEqual(result["capacity"]["blockers"], ["deploy step 12 failed (not a capacity wait)"])
        texts = [item["text"] for item in result["regressions"]]
        self.assertEqual(sum("deploy incomplete at step 12: 3 workloads" in text for text in texts), 1)
        self.assertEqual(cluster.sleeps, [])
        self.assertIsNotNone(saved_verdict(DEEP_SEARCH, record, result)["rollback_suggestion"])

    def test_deploy_incomplete_without_capacity_is_one_regression(self) -> None:
        """Rule 2 alone: not capacity, one deploy-incomplete regression, never one per workload."""
        cluster = deep_search_incident(pending_reason="FailedMount")
        quiet_cluster(cluster)
        cluster.rollout_results = [{ENTITY: False}]
        record = record_for(DEEP_SEARCH, status="deploy_failed", failed_step={"id": "12", "deployments": [ENTITY]})
        result = deploy_check(DEEP_SEARCH, cluster, record)
        self.assertEqual(result["result"], "failed")
        texts = [item["text"] for item in result["regressions"]]
        self.assertEqual(sum("deploy incomplete at step 12: 3 workloads" in text for text in texts), 1)
        self.assertFalse([text for text in texts if any(f"{name}: set to" in text for name in LATER)])
        self.assertIsNotNone(saved_verdict(DEEP_SEARCH, record, result)["rollback_suggestion"])

    def test_counter_case_crash_loop_with_a_traceback_rolls_back(self) -> None:
        cluster = orbit_api_incident(logs=TRACEBACK_LOGS, last_exit=1)
        cluster.rollout_results = [{"prod-orbit-api": False}]
        record = record_for(ORBIT_API)
        result = deploy_check(ORBIT_API, cluster, record)
        self.assertEqual(result["result"], "failed")
        self.assertEqual(result["capacity"]["verdict"], "not_capacity")
        saved = saved_verdict(ORBIT_API, record, result)
        self.assertEqual(saved["verdict"]["status"], "failed")
        self.assertEqual(saved["rollback_suggestion"]["target"]["commit"], "b" * 40)

    def test_counter_case_wrong_image_rolls_back(self) -> None:
        cluster = orbit_api_incident(new_digest=WRONG)
        cluster.rollout_results = [{"prod-orbit-api": False}]
        record = record_for(ORBIT_API)
        result = deploy_check(ORBIT_API, cluster, record)
        self.assertEqual(result["result"], "failed")
        self.assertIn(WRONG, " ".join(item["text"] for item in result["regressions"]))
        self.assertEqual(saved_verdict(ORBIT_API, record, result)["verdict"]["status"], "failed")

    def test_a_failed_smoke_check_on_ready_pods_still_rolls_back(self) -> None:
        """Capacity covers availability checks only: an http check that fails on Ready pods is the release's."""
        svc = service(
            "api",
            [("prod-orbit-api", "orbit-api")],
            [{"type": "http_public", "name": "home", "url": "https://x.test/"}],
        )
        cluster = orbit_api_incident()
        cluster.rollout_results = [{"prod-orbit-api": False}, {"prod-orbit-api": False}]
        clients = rr.Clients(env=svc.prod, runner=cluster)
        with (
            mock.patch.object(rr, "wait_rollouts", side_effect=cluster.wait_rollouts),
            cluster.fake_time(),
            redirect_stdout(io.StringIO()),
        ):
            result = rv.evaluate(
                svc,
                record_for(svc),
                phase="deploy",
                clients=clients,
                dd=None,
                now=NOW,
                wait_rollouts_seconds=300,
                get=lambda request: (500, b"down"),
            )
        self.assertEqual(result["result"], "failed")
        self.assertEqual([item["name"] for item in result["regressions"]], ["home"])

    def test_window_keeps_the_deploy_incomplete_finding_environmental(self) -> None:
        """At +15 the never-updated workloads are not image regressions; capacity at deploy keeps them environmental."""
        cluster = deep_search_incident()
        cluster.deployments[ENTITY]["status"]["availableReplicas"] = 64
        cluster.pods[63] = pod("entity-new-63", ENTITY, "entity-new", "worker", NEW)
        record = record_for(
            DEEP_SEARCH,
            status="deploy_failed",
            failed_step={"id": "12", "deployments": [ENTITY]},
            checks={"deploy": {"cause": "capacity", "result": "inconclusive"}},
        )
        clients = rr.Clients(env=DEEP_SEARCH.prod, runner=cluster)
        with redirect_stdout(io.StringIO()):
            result = rv.evaluate(DEEP_SEARCH, record, phase="window", clients=clients, dd=None, now=NOW)
        self.assertEqual(result["regressions"], [])
        self.assertEqual(
            {check["name"]: check["status"] for check in result["checks"]},
            {"images": "passed", "deploy": "environmental"},
        )
        record["checks"] = {}
        with redirect_stdout(io.StringIO()):
            result = rv.evaluate(DEEP_SEARCH, record, phase="window", clients=clients, dd=None, now=NOW)
        self.assertEqual([item["name"] for item in result["regressions"]], ["deploy"])


# --------------------------------------------------------------------------- the build side


class RolloutWaitTest(unittest.TestCase):
    """release_capacity.py rollout-wait: what the prod build's deploy step runs instead of a fixed wait."""

    def run_wait(self, cluster: Cluster, svc: rc.Service, *extra: str) -> tuple[int, str, list[str]]:
        """Run the command with the fake cluster; return (exit, failed-step file text, rollout timeouts)."""
        clients = rr.Clients(env=svc.prod, runner=cluster)
        with tempfile.TemporaryDirectory() as tmp:
            failed = Path(tmp) / "deploy_failed_step"
            argv = [
                "rollout-wait",
                "--image",
                f"{REPO}@{NEW}",
                "--step-id",
                "deploy-prod",
                "--failed-step-file",
                str(failed),
                *extra,
            ]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), cluster.fake_time():
                code = rcap.main(argv, clients=clients, popen=cluster.popen, now=lambda: NOW)
            text = failed.read_text(encoding="utf-8") if failed.exists() else ""
        waits = [part for command in cluster.commands for part in command if part.startswith("--timeout=")]
        return code, text, waits

    def test_complete(self) -> None:
        cluster = orbit_api_incident()
        self.assertEqual(self.run_wait(cluster, ORBIT_API), (0, "", ["--timeout=1020s"]))

    def test_capacity_waits_again_and_goes_on(self) -> None:
        """The second wait polls the Deployment for the scaled timeout; still capacity after it: go on."""
        cluster = orbit_api_incident()
        cluster.rollout_results = [{"prod-orbit-api": False}]
        self.assertEqual(self.run_wait(cluster, ORBIT_API), (0, "", ["--timeout=1020s"]))
        self.assertEqual(sum(cluster.sleeps), 1020)

    def test_capacity_then_finished_while_polling(self) -> None:
        cluster = orbit_api_incident()
        finish_on_second_wait(cluster)
        self.assertEqual(self.run_wait(cluster, ORBIT_API), (0, "", ["--timeout=1020s"]))
        self.assertEqual(cluster.sleeps, [30])

    def test_not_capacity_after_the_second_wait_fails_the_step(self) -> None:
        """Judged again after the second wait (like deep-search and orbit-api): a traceback now fails the step."""
        cluster = orbit_api_incident()
        cluster.rollout_results = [{"prod-orbit-api": False}]

        def crash() -> None:
            """The killed pods now log a traceback."""
            cluster.logs.update(dict.fromkeys(cluster.logs, TRACEBACK_LOGS))

        cluster.on_sleep = crash
        code, text, _ = self.run_wait(cluster, ORBIT_API)
        self.assertEqual(code, 1)
        self.assertIn("ImportError", json.loads(text)["verdict"])

    def test_not_capacity_fails_the_step_and_names_it(self) -> None:
        cluster = orbit_api_incident(logs=TRACEBACK_LOGS)
        cluster.rollout_results = [{"prod-orbit-api": False}]
        code, text, waits = self.run_wait(cluster, ORBIT_API)
        self.assertEqual((code, waits), (1, ["--timeout=1020s"]))
        step = json.loads(text)
        self.assertEqual((step["id"], step["deployments"]), ("deploy-prod", ["prod-orbit-api"]))
        self.assertEqual(rr.read_failed_step(None), None)

    def test_deep_search_step_with_several_deployments(self) -> None:
        cluster = deep_search_incident()
        for name in LATER:
            cluster.deployments[name] = deployment(name, "wal", NEW, 1)
        cluster.rollout_results = [{ENTITY: False}]
        code, text, waits = self.run_wait(cluster, DEEP_SEARCH)
        self.assertEqual((code, text), (0, ""))
        self.assertEqual(waits, ["--timeout=3180s", "--timeout=900s", "--timeout=900s", "--timeout=900s"])
        self.assertEqual(sum(cluster.sleeps), 3180)

    def test_timeout_command(self) -> None:
        cluster = deep_search_incident()
        out = io.StringIO()
        with redirect_stdout(out):
            code = rcap.main(
                ["timeout", "--deployment", ENTITY], clients=rr.Clients(env=DEEP_SEARCH.prod, runner=cluster)
            )
        self.assertEqual(code, 0)
        self.assertIn(f"deployment/{ENTITY}: 3180 s (64 replicas, 4 waves of 16, 720 s per wave)", out.getvalue())

    def test_finish_keeps_the_failed_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "step"
            self.assertIsNone(rr.read_failed_step(str(path)))
            path.write_text(json.dumps({"id": "12", "deployments": [ENTITY]}), encoding="utf-8")
            self.assertEqual(rr.read_failed_step(str(path))["id"], "12")
            path.write_text("deploy-prod\n", encoding="utf-8")
            self.assertEqual(rr.read_failed_step(str(path)), {"id": "deploy-prod"})


if __name__ == "__main__":
    unittest.main()
