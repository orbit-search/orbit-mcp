#!/usr/bin/env python3
"""Release check of a check-only service that is not a GKE Deployment: Cloud Run services or a Composer DAG folder.

The GKE scripts (release_record, release_verify, release_rollback) move image
digests on Deployments. Two Orbit services deploy another way, and both are tier
check_only, so this module records, checks and alerts, and prints the rollback
command for a human:

- kind "cloud_run": one build deploys one or more Cloud Run services. A release is
  the serving revision of each. Deploy check: the new revision is ready and
  serves all traffic, plus the config's public URLs. Rollback: send the traffic
  back to the previous revisions.
- kind "composer": one build syncs dags/ into a Cloud Composer environment. A
  release is the commit. Deploy check: no new DAG import error. Rollback: run the
  sync trigger at the previous commit.

Both get the watch window of the GKE services: new warning and error log lines
(the config's Cloud Logging filter) compared with the 24 h before the release,
each traced to a suspected commit; composer also gets DAGs whose runs fail after
the release and not in the baseline. A failed release posts one Slack alert that
mentions the suspected PR's author, with a fix prompt and the rollback command.

    python3 release/release_light.py turn --commit <sha> --build-id <id>       # single flight; exit 3: superseded
    python3 release/release_light.py begin --commit <sha> --build-id <id> --release-id-file /workspace/release_id
    python3 release/release_light.py finish --release-id-file /workspace/release_id --status deployed
    python3 release/release_light.py check --release-id-file /workspace/release_id
    python3 release/release_light.py schedule-window --release-id-file /workspace/release_id
    python3 release/release_light.py rollback-command [--release-id <id>]

Part of the release kit (orbit-infrastructure/release-kit). Stdlib only (Python 3.10).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_config as rc  # noqa: E402
import release_record as rr  # noqa: E402
import release_signals as rs  # noqa: E402
import release_verify as rv  # noqa: E402
from promote_release_guard import GoogleApi, GuardError, _http_json  # noqa: E402

KINDS = ("cloud_run", "composer")
KIT_DIR = Path(__file__).resolve().parent
REPO_ROOT = KIT_DIR.parent


@dataclass(frozen=True)
class LightService:
    name: str
    title: str
    kind: str
    github_repo: str
    connection: str
    connected_repository: str
    release_branch: str
    project: str
    region: str
    store: str
    docs: str
    code: rc.Code
    checkpoints: tuple[int, ...]
    smoke: tuple[dict, ...]
    log_filter: str
    log_min_spread: int
    # The triggers whose builds deploy this service (single flight counts them all).
    deploy_triggers: tuple[str, ...] = ()
    # cloud_run: the services one build deploys.
    services: tuple[str, ...] = ()
    # composer: the environment and the trigger that syncs dags/.
    environment: str = ""
    sync_trigger: str = ""
    dags_prefix: str = "dags/"
    parse_wait_seconds: int = 300
    path: Path | None = None

    tier = "check_only"
    auto_rollback = False
    has_gate = False
    metrics: tuple = ()
    signals = rc.Signals()

    @property
    def window_config(self) -> str:
        return f"release/services/{self.name}.release-verify.yaml"

    def kit_command(self, script: str) -> str:
        flag = "" if rc.single_service() else f" --service {self.name}"
        return f"python3 release/{script}{flag}"

    @property
    def env(self) -> rc.Environment:
        """The fields of an Environment that the shared record and GitHub helpers read."""
        return rc.Environment(
            name="prod",
            service=self.name,
            title=self.title,
            tier="check_only",
            project=self.project,
            region=self.region,
            cluster="",
            namespace="",
            image=rc.Image(repository="", registry_location="", registry_repository="", package=""),
            deployments=(),
            smoke=self.smoke,
            github_repo=self.github_repo,
            connection=self.connection,
            connected_repository=self.connected_repository,
            release_branch=self.release_branch,
            deploy_triggers=(),
        )


def parse_light(data: dict, path: Path | None = None) -> LightService:
    where = str(path or "service config")
    kind = str(data.get("kind") or "")
    if kind not in KINDS:
        raise rc.ConfigError(f"{where}: kind {kind!r} is not one of {KINDS}")
    if data.get("tier") != "check_only":
        raise rc.ConfigError(f"{where}: kind {kind} supports tier check_only only")
    name = str(rc._require(data, "service", where))
    smoke = tuple(data.get("smoke") or ())
    for check in smoke:
        if check.get("type") != "http_public":
            raise rc.ConfigError(f"{where}: kind {kind} supports http_public smoke checks only")
        rc.validate_smoke(check, set(), f"{where} smoke")
    verify = dict(data.get("verify") or {})
    code_data = dict(rc._require(data, "code", where))  # type: ignore[arg-type]
    defaults = rc.Code(source_prefixes=(), extensions=())
    code = rc.Code(
        source_prefixes=rc._names(code_data.get("source_prefixes"), f"{where} code.source_prefixes"),
        extensions=rc._names(code_data.get("extensions"), f"{where} code.extensions"),
        test_markers=tuple(code_data.get("test_markers") or defaults.test_markers),
        non_runtime_prefixes=tuple(code_data.get("non_runtime_prefixes") or defaults.non_runtime_prefixes),
        deploy_prefixes=tuple(code_data.get("deploy_prefixes") or defaults.deploy_prefixes),
    )
    common = dict(
        name=name,
        title=str(data.get("title") or name),
        kind=kind,
        github_repo=str(rc._require(data, "github_repo", where)),
        connection=str(data.get("connection") or "orbit-search"),
        connected_repository=str(rc._require(data, "connected_repository", where)),
        release_branch=str(data.get("release_branch") or "main"),
        project=str(data.get("project") or "orbit-search"),
        region=str(data.get("region") or "us-central1"),
        store=str(data.get("store") or f"gs://orbit-search-release-records/{name}"),
        docs=str(data.get("docs") or "docs/RELEASE_PIPELINE.md"),
        code=code,
        checkpoints=tuple(int(value) for value in verify.get("checkpoints") or (15, 60)),
        smoke=smoke,
        log_filter=str(rc._require(verify, "log_filter", f"{where} verify")),
        log_min_spread=int(verify.get("log_min_spread") or 1),
        path=path,
    )
    if kind == "cloud_run":
        services = rc._names(rc._require(data, "cloud_run_services", where), f"{where} cloud_run_services")
        triggers = rc._names(rc._require(data, "deploy_triggers", where), f"{where} deploy_triggers")
        return LightService(**common, services=services, deploy_triggers=triggers)
    composer = dict(rc._require(data, "composer", where))  # type: ignore[arg-type]
    sync_trigger = str(rc._require(composer, "sync_trigger", f"{where} composer"))
    return LightService(
        **common,
        deploy_triggers=tuple(data.get("deploy_triggers") or (sync_trigger,)),
        environment=str(rc._require(composer, "environment", f"{where} composer")),
        sync_trigger=sync_trigger,
        dags_prefix=str(composer.get("dags_prefix") or "dags/"),
        parse_wait_seconds=int(composer.get("parse_wait_seconds") or 300),
    )


def load_light(name: str | None = None) -> LightService:
    name = name or os.environ.get("RELEASE_SERVICE", "").strip() or None
    files = rc.service_files()
    if name:
        path = rc.SERVICES_DIR / f"{name}.json"
    elif len(files) == 1:
        path = files[0]
    else:
        raise rc.ConfigError(f"{len(files)} service configs; name one with --service")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise rc.ConfigError(f"{path}: {error}") from None
    service = parse_light(data, path)
    if service.name != path.stem:
        raise rc.ConfigError(f"{path}: the file name must be the service name {service.name!r}")
    return service


# --------------------------------------------------------------------------- snapshots

Runner = Callable[[list[str]], str]


def gcloud_json(runner: Runner, *args: str) -> dict:
    return json.loads(runner(["gcloud", *args, "--format=json"]))


def snapshot_cloud_run(service: LightService, runner: Runner) -> dict[str, dict]:
    """Per Cloud Run service: the newest created and ready revisions, the traffic split and the image."""
    out = {}
    for name in service.services:
        data = gcloud_json(
            runner, "run", "services", "describe", name, f"--region={service.region}", f"--project={service.project}"
        )
        status = data.get("status") or {}
        traffic = {}
        for item in status.get("traffic") or []:
            revision = item.get("revisionName") or (
                status.get("latestReadyRevisionName") if item.get("latestRevision") else ""
            )
            if revision:
                traffic[revision] = traffic.get(revision, 0) + int(item.get("percent") or 0)
        containers = (((data.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or [{}]
        out[f"cloud_run/{name}"] = {
            "kind": "cloud_run",
            "name": name,
            "latest_created": status.get("latestCreatedRevisionName"),
            "latest_ready": status.get("latestReadyRevisionName"),
            "traffic": traffic,
            "image": containers[0].get("image"),
            "url": status.get("url"),
        }
    return out


class Airflow:
    """Read-only Airflow REST API of a Composer environment (the build identity's token)."""

    def __init__(self, service: LightService, runner: Runner, token: Callable[[], str]) -> None:
        uri = runner(
            [
                "gcloud",
                "composer",
                "environments",
                "describe",
                service.environment,
                f"--location={service.region}",
                f"--project={service.project}",
                "--format=value(config.airflowUri)",
            ]
        ).strip()
        if not uri.startswith("https://"):
            raise rr.RecordError(f"no Airflow URI for {service.environment}: {uri!r}")
        self.uri = uri.rstrip("/")
        self.token = token

    def get(self, path: str) -> dict:
        request = urllib.request.Request(f"{self.uri}{path}", headers={"Authorization": f"Bearer {self.token()}"})
        return _http_json(request)

    def import_errors(self) -> dict[str, str]:
        """{filename: first line of the error} of every DAG file that does not import now."""
        payload = self.get("/api/v2/importErrors?limit=200")
        return {
            str(item.get("filename")): str(item.get("stack_trace") or "").strip().splitlines()[-1:][0]
            if item.get("stack_trace")
            else ""
            for item in payload.get("import_errors") or []
        }

    def failed_runs(self, start: datetime, end: datetime) -> dict[str, int]:
        """{dag_id: failed runs} that ended in [start, end)."""
        query = (
            f"/api/v2/dags/~/dagRuns?state=failed&limit=200"
            f"&end_date_gte={start.strftime('%Y-%m-%dT%H:%M:%SZ')}&end_date_lte={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )
        out: dict[str, int] = {}
        for run in self.get(query).get("dag_runs") or []:
            out[str(run.get("dag_id"))] = out.get(str(run.get("dag_id")), 0) + 1
        return out


def snapshot(service: LightService, runner: Runner, token: Callable[[], str]) -> dict[str, dict]:
    if service.kind == "cloud_run":
        return snapshot_cloud_run(service, runner)
    try:
        errors: dict[str, str] | None = Airflow(service, runner, token).import_errors()
        problem = ""
    except (GuardError, rr.RecordError) as error:
        # The build identity needs roles/composer.user to read the Airflow API; the log checks still run.
        errors, problem = None, f"the Airflow API cannot be read: {str(error)[:300]}"
    return {"composer/import_errors": {"kind": "composer", "errors": errors, "error": problem}}


# --------------------------------------------------------------------------- checks


def repo_path(service: LightService, filename: str) -> str:
    """A Composer DAG file path (/home/airflow/gcs/dags/x.py) as its repository path (dags/x.py)."""
    marker = "/dags/"
    return service.dags_prefix + filename.split(marker, 1)[1] if marker in filename else filename


def check_cloud_run(before: dict[str, dict], after: dict[str, dict]) -> rv.Check:
    """Each service's newest revision is ready and serves all traffic."""
    problems = []
    for key, now in sorted(after.items()):
        if now.get("latest_created") != now.get("latest_ready"):
            problems.append(
                f"{key}: revision {now.get('latest_created')} is not ready (ready: {now.get('latest_ready')})"
            )
            continue
        serving = (now.get("traffic") or {}).get(now.get("latest_ready") or "", 0)
        if serving != 100:
            problems.append(f"{key}: {now.get('latest_ready')} serves {serving}% of traffic, not 100%")
        previous = before.get(key) or {}
        if previous.get("latest_ready") == now.get("latest_ready") and previous.get("image") != now.get("image"):
            # A new image must make a new revision; the same image may keep the old one.
            problems.append(f"{key}: no new revision for {now.get('image')} (still {now.get('latest_ready')})")
    return rv.Check("revisions", "failed" if problems else "passed", [f"{len(after)} services checked", *problems])


def check_import_errors(service: LightService, record: dict, after: dict[str, dict]) -> rv.Check:
    """No DAG file fails to import that imported before the sync (or fails with another error now)."""
    before_snapshot = (record.get("previous_snapshot") or {}).get("composer/import_errors") or {}
    after_snapshot = after.get("composer/import_errors") or {}
    if before_snapshot.get("errors") is None or after_snapshot.get("errors") is None:
        reason = after_snapshot.get("error") or before_snapshot.get("error") or "no import error snapshot"
        return rv.Check("dag_import_errors", "skipped", [reason])
    before, now = before_snapshot["errors"], after_snapshot["errors"]
    new = {name: error for name, error in now.items() if before.get(name) != error}
    details = [f"{len(now)} import errors now, {len(before)} before the sync"]
    details += [f"{repo_path(service, name)}: {error[:200]}" for name, error in sorted(new.items())]
    return rv.Check("dag_import_errors", "failed" if new else "passed", details)


def deploy_checks(
    service: LightService, record: dict, runner: Runner, token: Callable[[], str], get: rv.HttpGet = rv.http_get
) -> tuple[list[rv.Check], dict[str, dict]]:
    after = snapshot(service, runner, token)
    if service.kind == "cloud_run":
        checks = [check_cloud_run(record.get("previous_snapshot") or {}, after)]
    else:
        checks = [check_import_errors(service, record, after)]
    checks += [rv.check_http_public(check, get) for check in service.smoke]
    return checks, after


def dag_failure_problems(
    service: LightService, airflow: Airflow, scope: rs.Scope, now: datetime, tree: rs.SourceTree
) -> list[rs.Problem]:
    """DAGs with failed runs in the window and none in the baseline; the literal is the dag_id the code names."""
    baseline: dict[str, int] = {}
    for start, end in scope.baseline:
        for dag, count in airflow.failed_runs(start, end).items():
            baseline[dag] = baseline.get(dag, 0) + count
    problems = []
    for dag, count in sorted(airflow.failed_runs(scope.window[0], now).items()):
        if baseline.get(dag):
            continue
        files = sorted({path for quote in ('"', "'") for path, _ in tree.find_text(f"{quote}{dag}{quote}")})
        problems.append(
            rs.Problem(
                kind="dag_failure",
                key=f"dag:{dag}",
                text=f"DAG {dag}: {count} failed run(s) after the release, none in the 24 h before"
                + (f" (defined in {', '.join(files[:2])})" if files else ""),
                count=count,
                identifiers=[dag],
                fields={"dag": dag},
            )
        )
    return problems


# --------------------------------------------------------------------------- record lifecycle


def open_store(service: LightService, dry_run_dir: Path | None) -> rr.Store:
    token = GoogleApi(service.project, service.region).access_token
    return rr.open_store(service.store, service.env, token, dry_run_dir)


def rollback_command(service: LightService, record: dict) -> str:
    """The command a human runs to undo this release (it prints; nothing runs it automatically)."""
    previous = record.get("previous") or {}
    if service.kind == "composer":
        commit = previous.get("commit") or "<previous commit>"
        return (
            f"gcloud builds triggers run {service.sync_trigger} --region={service.region} "
            f"--project={service.project} --sha={commit}"
        )
    snapshot_before = record.get("previous_snapshot") or {}
    commands = []
    for name in service.services:
        revision = (snapshot_before.get(f"cloud_run/{name}") or {}).get("latest_ready") or "<previous revision>"
        commands.append(
            f"gcloud run services update-traffic {name} --region={service.region} --project={service.project} "
            f"--to-revisions={revision}=100"
        )
    return " && ".join(commands)


def cmd_begin(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    started_at = rr.utc_now()
    token = GoogleApi(service.project, service.region).access_token
    before = snapshot(service, runner, token)
    pointer, _ = store.read("latest.json")
    previous: dict = {"release_id": None, "commit": None}
    if pointer:
        record, _ = store.read(rr.record_name(pointer["release_id"]))
        if record:
            previous = {
                "release_id": record["release_id"],
                "commit": record["commit"],
                "deploy_finished": (record.get("times") or {}).get("deploy_finished"),
            }
    if not previous["commit"] and args.previous_commit:
        previous["commit"] = args.previous_commit
    try:
        github = rr.github_token(service.env)
    except GuardError as error:
        print(f"WARNING: no GitHub token, the record has no commit list: {error}", file=sys.stderr)
        github = None
    record = rr.new_record(
        service.env,
        kind="promote",
        commit=args.commit,
        images={},
        build=rr.build_info(service.env, args.build_id),
        previous=previous,
        started_at=started_at,
        token=github,
    )
    record["previous_snapshot"] = before
    rr.create_record(store, record, "deploy_started")
    Path(args.release_id_file).write_text(record["release_id"] + "\n", encoding="utf-8")
    print(f"Release {record['release_id']}: {store.location(rr.record_name(record['release_id']))}", flush=True)
    print(f"Previous: {previous.get('release_id') or 'unrecorded'} commit {previous.get('commit')}", flush=True)
    return 0


def cmd_finish(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    release_id = (
        Path(args.release_id_file).read_text(encoding="utf-8").strip() if args.release_id_file else args.release_id
    )
    after = snapshot(service, runner, GoogleApi(service.project, service.region).access_token)

    def finish(record: dict) -> None:
        record["workloads"] = after
        record["status"] = args.status
        record.setdefault("times", {})["deploy_finished"] = rr.utc_now()

    saved = rr.update_record(store, release_id, finish, args.status)
    rr.point_latest(store, saved)
    print(f"Release {release_id}: {args.status}", flush=True)
    return 0


def _fail(service: LightService, store: rr.Store, record: dict, phase: str, regressions: list[dict]) -> dict:
    """Set the failed verdict and the suggestion (the rollback command for a human)."""
    suggestion = {
        "created_at": rr.utc_now(),
        "phase": phase,
        "summary": f"{len(regressions)} problem(s): " + "; ".join(item["text"] for item in regressions[:3]),
        "regressions": regressions,
        "suspected_prs": rv.suspected_prs(service, record, regressions, rr_token(service)),
        "target": {"commit": (record.get("previous") or {}).get("commit")},
        "how": [rollback_command(service, record)],
        "note": "Tier check_only: a human rolls back.",
    }

    def change(current: dict) -> None:
        if (current.get("verdict") or {}).get("status") != "failed":
            current["verdict"] = {
                "status": "failed",
                "at": rr.utc_now(),
                "phase": phase,
                "reasons": [item["text"] for item in regressions],
            }
            current["rollback_suggestion"] = suggestion

    return rr.update_record(store, record["release_id"], change, f"verify_{phase}", {"result": "failed"})


def rr_token(service: LightService) -> str | None:
    try:
        return rr.github_token(service.env)
    except GuardError:
        return None


def alert(service: LightService, store: rr.Store, record: dict) -> None:
    """The failed-release Slack alert with the author ping, a fix prompt and the rollback command."""
    if os.environ.get("RELEASE_SLACK_DISABLED", "").strip() == "1":
        print("Test run: no alert", flush=True)
        return
    location = store.location(rr.record_name(record["release_id"]))
    text, blocks = rv.rollback_alert(service, record, None, location, command=rollback_command(service, record))
    rr.post_slack(text, blocks)


def cmd_check(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Deploy checks right after the deploy (composer: after the DAG processor parsed the new files)."""
    record = rr.read_record(store, args.release_id)
    if service.kind == "composer" and args.wait:
        print(f"Waiting {service.parse_wait_seconds} s for the DAG processor", flush=True)
        time.sleep(service.parse_wait_seconds)
    checks, after = deploy_checks(service, record, runner, GoogleApi(service.project, service.region).access_token)
    for check in checks:
        print(f"   check {check.name}: {check.status}")
        for line in check.details[:12]:
            print(f"      {line}")
    failed = [check for check in checks if check.status == "failed"]
    result = {"phase": "deploy", "at": rr.utc_now(), "checks": [asdict(check) for check in checks]}
    saved = rr.update_record(
        store,
        record["release_id"],
        lambda current: current.setdefault("checks", {}).__setitem__("deploy", result),
        "verify_deploy",
        {"result": "failed" if failed else "passed"},
    )
    where = store.location(rr.record_name(record["release_id"]))
    if failed:
        regressions = [
            {
                "kind": "check",
                "name": check.name,
                "text": f"{check.name}: {'; '.join(check.details[1:] or check.details)}",
            }
            for check in failed
        ]
        saved = _fail(service, store, saved, "deploy", regressions)
        if args.force_fail:
            rr.post_slack(
                f"{rv.TEST_RUN_LINE}. A test of {service.title} release-verify failed on purpose; no action needed."
            )
        else:
            alert(service, store, saved)
        if args.failed_marker:
            Path(args.failed_marker).write_text("true\n", encoding="utf-8")
        return rv.EXIT_FAILED
    minutes = " and ".join(f"+{value}" for value in service.checkpoints)
    rr.post_slack(
        f":mag: {service.title} release-verify {record['commit'][:9]} deploy checks passed; window checks follow "
        f"at {minutes} min. Record {where}"
    )
    return rv.EXIT_OK


def evaluate_window(service: LightService, store: rr.Store, record: dict, runner: Runner, now: datetime) -> dict:
    """New error log lines (and composer: newly failing DAGs), traced to suspects."""
    token = GoogleApi(service.project, service.region).access_token
    scope = rs.release_scope(store, record, now)
    logs = rs.CloudLogging(service.project, token)
    tree = rs.SourceTree(REPO_ROOT, service.code)
    parts: dict[str, dict] = {}
    problems: list[rs.Problem] = []
    try:
        found, notes = rs.log_problems(
            logs,
            scope,
            now,
            env=service.env,
            containers=(),
            min_spread=service.log_min_spread,
            base_filter=service.log_filter,
        )
        parts["logs"] = {"status": "checked", "found": len(found), "detail": "; ".join(notes)}
        problems += found
    except (GuardError, rs.NoAccess, TimeoutError, OSError, ValueError, KeyError) as error:
        parts["logs"] = {"status": "inconclusive", "detail": str(error)[:300]}
    if service.kind == "composer":
        try:
            found = dag_failure_problems(service, Airflow(service, runner, token), scope, now, tree)
            parts["dag_failures"] = {"status": "checked", "found": len(found), "detail": ""}
            problems += found
        except (GuardError, rr.RecordError, OSError, ValueError, KeyError) as error:
            parts["dag_failures"] = {"status": "inconclusive", "detail": str(error)[:300]}
    rs.trace_suspects(problems, scope, token=rr_token(service), repo=service.github_repo, tree=tree, code=service.code)
    return {"scope": scope.summary(), "parts": parts, "problems": [problem.summary() for problem in problems]}


def cmd_watch(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    record = rr.read_record(store, args.release_id)
    if (record.get("verdict") or {}).get("status") == "failed":
        alert(service, store, record)
        return rv.EXIT_OK
    finished = rr.parse_utc(record["times"].get("deploy_finished") or record["times"]["deploy_started"])
    checkpoints = sorted(int(value) for value in (args.checkpoints or " ".join(map(str, service.checkpoints))).split())
    for index, minutes in enumerate(checkpoints):
        last = index == len(checkpoints) - 1
        wait = (finished + timedelta(minutes=minutes) - datetime.now(rr.UTC)).total_seconds()
        if wait > 0:
            print(f"Waiting {wait / 60:.1f} min for the +{minutes} min checkpoint", flush=True)
            time.sleep(wait)
        pointer, _ = store.read("latest.json")
        if pointer and pointer.get("release_id") != record["release_id"]:
            print(f"Superseded by {pointer['release_id']}; the watch stops", flush=True)
            return rv.EXIT_OK
        try:
            new_errors = evaluate_window(service, store, record, runner, datetime.now(rr.UTC))
        except (rr.RecordError, GuardError, OSError, ValueError, KeyError) as error:
            print(f"Checkpoint +{minutes} min could not run: {error}", file=sys.stderr, flush=True)
            continue
        key = f"window_{minutes}m"
        regressions = [rv.new_error_regression(problem) for problem in new_errors["problems"] if problem["clear"]]
        warnings = [
            rv.new_error_regression(problem)
            for problem in new_errors["problems"]
            if not problem["clear"] and problem.get("suspects") and problem["suspects"][0].get("linked")
        ]
        result = {"phase": "window", "at": rr.utc_now(), "new_errors": new_errors, "warnings": warnings}
        fresh: list[dict] = []

        def store_result(current: dict, key: str = key, result: dict = result) -> None:
            current.setdefault("checks", {})[key] = result
            known = current.setdefault("warnings", {})
            fresh.clear()
            for warning in result["warnings"]:
                entry = known.setdefault(
                    warning["name"], {"at": result["at"], "text": warning["text"], "posted": False}
                )
                if not entry.get("posted"):
                    fresh.append(warning)

        saved = rr.update_record(
            store, record["release_id"], store_result, f"verify_{key}", {"problems": len(new_errors["problems"])}
        )
        for problem in new_errors["problems"]:
            print(f"   {'FAILS' if problem['clear'] else 'warns'}  {problem['text'][:300]}")
        if regressions:
            saved = _fail(service, store, saved, key, regressions)
            alert(service, store, saved)
            return rv.EXIT_OK
        if fresh:
            rv.post_warnings(service, store, saved, fresh)
        if last:
            checked = new_errors["parts"].get("logs", {}).get("status") == "checked"
            status = "passed" if checked else "inconclusive"

            def done(current: dict, status: str = status) -> None:
                if (current.get("verdict") or {}).get("status") != "failed":
                    current["verdict"] = {"status": status, "at": rr.utc_now(), "phase": key}

            rr.update_record(store, record["release_id"], done, "verify_final", {"status": status})
            icon = ":white_check_mark:" if checked else ":grey_question:"
            rr.post_slack(
                f"{icon} {service.title} release-verify {record['commit'][:9]} {status}. "
                f"Record {store.location(rr.record_name(record['release_id']))}"
            )
    return rv.EXIT_OK


def window_sources(service: LightService) -> list[Path]:
    sources = [path for path in KIT_DIR.rglob("*") if path.is_file() and "__pycache__" not in path.parts]
    for prefix in service.code.source_prefixes:
        base = REPO_ROOT / prefix.rstrip("/")
        paths = [base] if base.is_file() else base.rglob("*") if base.is_dir() else []
        for path in paths:
            if (
                path.is_file()
                and "node_modules" not in path.parts
                and service.code.is_source(path.relative_to(REPO_ROOT).as_posix())
            ):
                sources.append(path)
    return sources


def cmd_schedule_window(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    source = Path(args.source_dir)
    for path in window_sources(service):
        target = source / path.relative_to(REPO_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    checkpoints = " ".join(str(value) for value in service.checkpoints)
    substitutions = (
        f"_SERVICE={service.name},_RELEASE_ID={args.release_id},_CHECKPOINTS={checkpoints},"
        f"_SLACK_DISABLED={'1' if args.no_slack else ''}"
    )
    build_id = rr.run_command(
        [
            "gcloud",
            "builds",
            "submit",
            str(source),
            f"--config={source / service.window_config}",
            f"--region={service.region}",
            f"--project={service.project}",
            f"--substitutions={substitutions}",
            "--async",
            "--format=value(id)",
        ],
        timeout=600,
    ).strip()
    rr.update_record(
        store,
        args.release_id,
        lambda current: current.setdefault("checks", {}).__setitem__("window_build", {"id": build_id}),
        "window_scheduled",
        {"build_id": build_id},
    )
    print(f"Watch window build {build_id} submitted", flush=True)
    return rv.EXIT_OK


def cmd_turn(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Single flight of the deploy build: an older one that runs is waited for, a newer one supersedes this one.

    The commit must be on the release branch. Exit 3: superseded (deploy nothing, not a failure).
    """
    import promote_release_guard as guard  # noqa: PLC0415

    api = GoogleApi(service.project, service.region)
    turn = guard.Guard(api=api, build_id=args.build_id, commit=args.commit, peer_triggers=service.deploy_triggers)
    try:
        own = turn.own_build()
        token = api.repository_read_token(service.connection, service.connected_repository)
        turn.check_on_branch(service.github_repo, service.release_branch, token)
        turn.wait_for_turn(own, args.older_timeout_seconds)
    except guard.Superseded as reason:
        print(f"SUPERSEDED: {reason}; this build deploys nothing", flush=True)
        return guard.EXIT_SUPERSEDED
    return rv.EXIT_OK


def cmd_rollback_command(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    record = rr.read_record(store, args.release_id)
    print(rollback_command(service, record))
    return rv.EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--service", help="service config name (release/services/<name>.json)")
    parser.add_argument("--dry-run-dir", type=Path, help="read the real store, write records only under this directory")
    commands = parser.add_subparsers(dest="command", required=True)

    def release_id_args(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--release-id", default="latest")
        sub.add_argument("--release-id-file")

    begin = commands.add_parser("begin", help="snapshot and write a deploying record (before the deploy)")
    begin.add_argument("--commit", required=True)
    begin.add_argument("--build-id")
    begin.add_argument("--release-id-file", required=True)
    begin.add_argument("--previous-commit", default="", help="the commit that ran before, when no record exists yet")
    finish = commands.add_parser("finish", help="snapshot after the deploy and set the status")
    release_id_args(finish)
    finish.add_argument("--status", required=True, choices=["deployed", "deploy_failed"])
    check = commands.add_parser("check", help="deploy checks")
    release_id_args(check)
    check.add_argument("--wait", action="store_true", help="composer: wait for the DAG processor first")
    check.add_argument("--failed-marker")
    check.add_argument("--force-fail", default="", help="test runs: the alert says TEST RUN")
    watch = commands.add_parser("watch", help="the window checks at each checkpoint")
    release_id_args(watch)
    watch.add_argument("--checkpoints", default="")
    schedule = commands.add_parser("schedule-window", help="submit the watch-window build")
    release_id_args(schedule)
    schedule.add_argument("--source-dir", default="/workspace/release-verify-src")
    schedule.add_argument("--no-slack", action="store_true")
    turn = commands.add_parser("turn", help="single flight of the deploy build (exit 3: superseded)")
    turn.add_argument("--build-id", required=True)
    turn.add_argument("--commit", required=True)
    turn.add_argument("--older-timeout-seconds", type=float, default=1800)
    command = commands.add_parser("rollback-command", help="print the command that undoes a release")
    release_id_args(command)

    args = parser.parse_args(argv)
    if getattr(args, "release_id_file", None) and args.command != "begin":
        args.release_id = Path(args.release_id_file).read_text(encoding="utf-8").strip()
    handlers = {
        "begin": cmd_begin,
        "finish": cmd_finish,
        "check": cmd_check,
        "watch": cmd_watch,
        "schedule-window": cmd_schedule_window,
        "turn": cmd_turn,
        "rollback-command": cmd_rollback_command,
    }
    try:
        service = load_light(args.service)
        store = open_store(service, args.dry_run_dir)
        return handlers[args.command](args, service, store, rr.run_command)
    except (
        rr.RecordError,
        GuardError,
        rc.ConfigError,
        subprocess.SubprocessError,
        KeyError,
        OSError,
        ValueError,
    ) as error:
        print(f"release light: {error}", file=sys.stderr, flush=True)
        return rv.EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
