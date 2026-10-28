#!/usr/bin/env python3
"""One-step rollback of a service's prod to an earlier release.

    python3 release/release_rollback.py plan                    # dry run: what would change
    python3 release/release_rollback.py run --reason "<why>"    # start the rollback build

`run` starts the `<service>-rollback` Cloud Build trigger
(release/services/<service>.rollback.yaml, the same identity as the prod
build); without the trigger it submits that config from this checkout. Inside
the build, `apply` does the work:

1. Guards: no prod deploy build and no other rollback runs, and the target
   digests are still in the registry.
2. Writes a release record of kind "rollback" (release_record.py).
3. Sets the target digests, with no image build, on every Deployment of the
   service.
4. Waits for the rollouts, checks that each runs its target, marks the
   rolled-back release, moves latest.json and posts one Slack line.

The default target is "previous": what ran before the newest record, from that
record's "before" snapshot. Only images change. Manifest and environment
changes made since the target stay; the plan lists those files. After a
rollback, automatic prod deploys hold (the record's `promote_hold`) until a
revert or a commit with `Fixes-release: <release id>` merges, or a human runs
`release_gate.py resume`.

A human runs this command, or the watch-window build of a release that failed
release-verify starts the trigger itself (release_verify.start_rollback, with
_EXPECT_LATEST so that it refuses when prod moved on).

Services that are not GKE Deployments (release_light.py, tier full) use the same
commands. A Cloud Run rollback sends the traffic back to the revisions of the
target release; a Composer rollback syncs the target commit's DAGs to dags/; a
Vercel rollback points the production domains back at the target's deployment.
The lock, the record, `_EXPECT_LATEST` and the hold are the same code.

Part of the release kit (orbit-infrastructure/release-kit); ported from
deep-search bin/release_rollback.py (#2232) and orbit-api #1525.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_config as rc  # noqa: E402
import release_gate as rg  # noqa: E402
import release_record as rr  # noqa: E402
import release_verify as rv  # noqa: E402
from promote_release_guard import ONGOING_STATUSES, GoogleApi, GuardError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Step:
    workload: str
    name: str
    container: str
    current: str | None
    target: str | None
    action: str  # set, none, skip

    def line(self) -> str:
        """One plan line for the step: already at the target, skipped, or current -> target."""
        if self.action == "none":
            return f"  = {self.workload}: already {self.target}"
        if self.action == "skip":
            return f"  ? {self.workload}: no target digest in the record; left at {self.current}"
        return f"  > {self.workload}: {self.current} -> {self.target}"


@dataclass
class Plan:
    from_release: str | None
    from_commit: str | None
    target_release: str | None
    target_commit: str | None
    target_images: dict
    steps: list[Step]
    not_rolled_back: list[str]
    notes: list[str]
    problems: list[str]
    settings_compared: bool = False
    target_artifacts: dict[str, str] = field(default_factory=dict)

    def changes(self) -> list[Step]:
        """The steps that set a new image."""
        return [step for step in self.steps if step.action == "set"]


PREVIOUS_OF = "previous-of:"


def resolve_target(store: rr.Store, target: str) -> tuple[dict, dict]:
    """Return (newest record, target) where target has release_id, commit, images, workloads.

    "previous" is what ran before the newest record; "previous-of:<release id>" is what ran before
    that release, from its own "before" snapshot (it works when that release has no recorded
    predecessor); a release id is that release.
    """
    latest = rr.read_record(store, "latest")
    if target == "previous" and latest.get("kind") == "rollback":
        # "previous" of a rollback is the release it rolled back: a retry would undo the rollback.
        rolled_back = (latest.get("rollback") or {}).get("rolled_back_release")
        raise rr.RecordError(
            f"prod already rolled back to {latest.get('commit', '')[:9]} ({latest['release_id']}, undoing "
            f"{rolled_back}); to deploy another release pass --to <release id>"
        )
    if target == "previous" or target.startswith(PREVIOUS_OF):
        holder = latest if target == "previous" else rr.read_record(store, target[len(PREVIOUS_OF) :])
        previous = holder.get("previous") or {}
        return latest, {
            "release_id": previous.get("release_id"),
            "commit": previous.get("commit"),
            "images": previous.get("images") or {},
            "workloads": previous.get("workloads") or {},
            "artifacts": previous.get("artifacts") or {},
        }
    record = rr.read_record(store, target)
    if record.get("status") not in ("deployed", "rolled_back"):
        raise rr.RecordError(f"release {target} has status {record.get('status')}; pick a release that deployed")
    return latest, {
        "release_id": record["release_id"],
        "commit": record["commit"],
        "images": record.get("images") or {},
        "workloads": record.get("workloads") or {},
        "artifacts": record.get("artifacts") or {},
    }


def target_reference(env: rc.Environment, digest: str, commit: str | None) -> str:
    """The image to set: repo:<commit>@digest keeps the commit visible (Kubernetes uses the digest)."""
    if commit and len(commit) == 40:
        return f"{env.image_repository}:{commit}@{digest}"
    return f"{env.image_repository}@{digest}"


def build_plan(service: rc.Service, latest: dict, target: dict, live: dict[str, dict], token: str | None) -> Plan:
    """Plan the rollback: one step per prod Deployment, the deploy files it leaves as they are, notes and problems."""
    env = service.prod
    steps = []
    for item in env.deployments:
        key = f"deployment/{item.name}"
        current = (live.get(key) or {}).get("digest")
        recorded = (target.get("workloads") or {}).get(key) or {}
        digest = recorded.get("digest") or rr.digest_of((target.get("images") or {}).get(rr.IMAGE_ROLE))
        if not digest:
            steps.append(Step(key, item.name, item.container, current, None, "skip"))
            continue
        reference = target_reference(env, digest, target.get("commit"))
        steps.append(Step(key, item.name, item.container, current, reference, "none" if current == digest else "set"))
    notes = []
    not_rolled_back: list[str] = []
    settings_compared = bool(
        re.fullmatch(r"[0-9a-f]{40}", str(target.get("commit") or "")) and target["commit"] == latest.get("commit")
    )
    if target.get("commit") and latest.get("commit") and token and target["commit"] != latest["commit"]:
        try:
            payload = rr.github_json(f"repos/{env.github_repo}/compare/{target['commit']}...{latest['commit']}", token)
            files = payload.get("files")
            settings_compared = isinstance(files, list) and len(files) < 300
            if not settings_compared:
                notes.append("the manifest comparison is missing or may be truncated")
            not_rolled_back = sorted(
                item["filename"]
                for item in payload.get("files") or []
                if service.code.is_deploy(item["filename"]) and not item["filename"].startswith("release/")
            )
        except GuardError as error:
            notes.append(f"could not list changed manifests: {error}")
    if not target.get("commit"):
        notes.append("the target commit is unknown; the Deployments get repo@digest without a commit tag")
    notes.append(
        f"{env.release_branch} still has {str(latest.get('commit'))[:9]}. Automatic prod deploys hold after the "
        f"rollback until a revert or a commit with the trailer 'Fixes-release: {latest.get('release_id')}' merges."
    )
    problems = [f"{step.workload}: no target digest in the record" for step in steps if step.action == "skip"]
    return Plan(
        from_release=latest.get("release_id"),
        from_commit=latest.get("commit"),
        target_release=target.get("release_id"),
        target_commit=target.get("commit"),
        target_images=target.get("images") or {},
        steps=steps,
        not_rolled_back=not_rolled_back,
        settings_compared=settings_compared,
        notes=notes,
        problems=problems,
    )


def missing_images(service: rc.Service, plan: Plan, api: GoogleApi) -> list[str]:
    """Target digests that the registry no longer holds (a cleanup policy may delete old images)."""
    image = service.image
    problems = []
    for digest in sorted({rr.digest_of(step.target) for step in plan.steps if step.target} - {None}):
        try:
            api.require_version(image.registry_location, image.registry_repository, image.package, digest)
        except GuardError as error:
            problems.append(str(error))
    return problems


def print_plan(service: rc.Service, plan: Plan) -> None:
    """Print the plan: the target, each step, the files it does not roll back, the notes and the problems."""
    print(f"Rollback of {service.name} prod from {plan.from_release} ({str(plan.from_commit)[:9]})")
    print(f"  to {plan.target_release or 'the unrecorded release before it'} ({str(plan.target_commit)[:9]})")
    for role, image in sorted(plan.target_images.items()):
        print(f"  target {role} image: {image}")
    print(f"Deployments ({len(plan.changes())} to change of {len(plan.steps)}):")
    for step in plan.steps:
        print(step.line())
    if plan.not_rolled_back:
        print("Not rolled back (image-only rollback; these files changed since the target):")
        for path in plan.not_rolled_back:
            print(f"  - {path}")
    for note in plan.notes:
        print(f"Note: {note}")
    for problem in plan.problems:
        print(f"BLOCKED: {problem}")


# --------------------------------------------------------------------------- apply


def assert_alone(service: rc.Service, own_build_id: str | None) -> None:
    """Refuse while a prod deploy build or another rollback (not a dry run) runs.

    The rollback holds the prod lock here. A deploy build past its gate holds that lock until its
    `finish` step, so this check only catches builds that take no lock (retired triggers). A deploy
    build before its first cluster change waits at the gate for this rollback, so it does not block.
    """
    api = GoogleApi(service.project, service.region)
    running = [build for build in api.list_builds(service.prod.running_deploys_filter()) if build.changing_cluster]
    if running:
        raise rr.RecordError(f"a prod deploy build is changing prod ({running[0].describe()}); wait for it, then retry")
    others = [
        b
        for b in api.ongoing_rollbacks(service.rollback_build_tag)
        if b.id != own_build_id and b.status in ONGOING_STATUSES
    ]
    if others:
        raise rr.RecordError(f"another rollback build is running ({others[0].describe()})")


def rolled_back_images(env: rc.Environment, latest: dict, live: dict[str, dict]) -> tuple[dict, dict]:
    """What the release that is rolled back ran: its own record first, the live snapshot second.

    After an incomplete attempt the cluster already runs the target, while
    latest.json still names the release that is rolled back; its record keeps
    the images it deployed, so a retry still records them as "previous".
    """
    recorded = latest.get("workloads") or {}
    workloads = {
        key: recorded[key] if (recorded.get(key) or {}).get("digest") else workload for key, workload in live.items()
    }
    image = (latest.get("images") or {}).get(rr.IMAGE_ROLE) or rr.image_from_snapshot(env, workloads)
    return workloads, {rr.IMAGE_ROLE: image}


def rollback_env(env: rc.Environment, name: str) -> tuple[tuple[str, str], ...]:
    """The env values a rollback sets on this Deployment with its image (the service config's rollback_env)."""
    return next((item.rollback_env for item in env.deployments if item.name == name), ())


def apply_plan(plan: Plan, clients: rr.Clients) -> list[str]:
    """Set every target image (with the Deployment's rollback_env in the same patch), then wait for the rollouts.

    A Deployment with rollback_env is patched on every attempt, also when its image is already
    the target (a retry after a patch that set the image but not the env); returns the failures.
    """
    failures: list[str] = []
    changing = {step.name for step in plan.changes()}
    for step in plan.steps:
        values = rollback_env(clients.env, step.name)
        if not step.target or (step.name not in changing and not values):
            continue
        base = [*clients.kubectl_base(), "-n", clients.env.namespace]
        try:
            if values:
                container = {"name": step.container, "image": step.target}
                # "valueFrom": None removes a secret or field source, which Kubernetes refuses beside "value".
                container["env"] = [{"name": key, "value": value, "valueFrom": None} for key, value in values]
                patch = json.dumps({"spec": {"template": {"spec": {"containers": [container]}}}})
                clients.runner([*base, "patch", f"deployment/{step.name}", "--type=strategic", "-p", patch])
                shown = ", ".join(f"{key}={value}" for key, value in values)
                print(f"patched deployment/{step.name} {step.container}={step.target} ({shown})", flush=True)
            else:
                clients.runner([*base, "set", "image", f"deployment/{step.name}", f"{step.container}={step.target}"])
                print(f"set deployment/{step.name} {step.container}={step.target}", flush=True)
        except rr.RecordError as error:
            failures.append(f"deployment/{step.name}: {error}")
    # Every targeted Deployment, also one this attempt did not change (a retry).
    failures += rr.wait_rollouts(clients, [step.name for step in plan.steps if step.target])
    return failures


def _plan_for(service: rc.Service, store: rr.Store, clients: rr.Clients, target: str) -> tuple[dict, dict, dict, Plan]:
    """Resolve the target, read what runs now and build the plan, with the images the registry no longer holds."""
    latest, resolved = resolve_target(store, target)
    live = rr.snapshot_workloads(clients)
    plan = build_plan(service, latest, resolved, live, _token(service))
    plan.problems += missing_images(service, plan, GoogleApi(service.project, service.region))
    return latest, resolved, live, plan


def cmd_plan(args: argparse.Namespace, service: rc.Service, store: rr.Store, clients: rr.Clients) -> int:
    """Dry run: print the plan (and write it as JSON with --json); 1 when the rollback is blocked."""
    _, _, _, plan = _plan_for(service, store, clients, args.to)
    print_plan(service, plan)
    if args.json:
        Path(args.json).write_text(json.dumps(asdict(plan), indent=2, sort_keys=True), encoding="utf-8")
    if plan.problems:
        print("\nDry run: nothing changed. This rollback is blocked; pick another target with --to.")
        return 1
    print(
        "\nDry run: nothing changed. Start the rollback with: "
        f"{service.kit_command('release_rollback.py', 'run')} --reason '<why>'"
    )
    return 0


# How long a rollback build waits for the prod lock (a deploy build holds it from its gate to its
# `finish` step). The rollback step's timeout leaves room for the apply after that wait.
LOCK_WAIT_SECONDS = 1800


def cmd_apply(
    args: argparse.Namespace,
    service: rc.Service,
    store: rr.Store,
    clients: rr.Clients,
    *,
    alive: Callable[[str], bool] | None = None,
) -> int:
    """Run inside the rollback build: take the prod lock, then guard, record, apply, verify, record."""
    return locked_apply(args, service, store, lambda: _apply(args, service, store, clients), alive=alive)


def locked_apply(
    args: argparse.Namespace,
    service: object,
    store: rr.Store,
    apply: Callable[[], int],
    *,
    alive: Callable[[str], bool] | None = None,
) -> int:
    """Run a rollback apply under the prod lock (any kind of service); a dry run takes no lock."""
    if args.dry_run:
        # A dry run changes nothing, so it takes no lock.
        return apply()
    if not args.reason.strip():
        raise rr.RecordError("--reason is required: say why prod rolls back")
    if not args.build_id:
        raise rr.RecordError("--build-id is required: the rollback build holds the prod lock while it applies")
    alive = alive or rr.build_alive(service.prod)
    rr.acquire_lock(store, args.build_id, "rollback", alive, wait_seconds=LOCK_WAIT_SECONDS)
    try:
        return apply()
    finally:
        rr.release_lock(store, args.build_id)


def check_expect_latest(args: argparse.Namespace, latest: dict) -> None:
    """Refuse when --expect-latest names another release than the newest record (prod moved on)."""
    if args.expect_latest and latest["release_id"] != args.expect_latest:
        raise rr.RecordError(
            f"prod moved on: the newest record is {latest['release_id']}, not {args.expect_latest}; nothing changed"
        )


def create_rollback_record(
    service: object,
    store: rr.Store,
    args: argparse.Namespace,
    latest: dict,
    target: dict,
    previous: tuple[dict, dict],
    plan: Plan,
) -> dict:
    """Write the "rollback" record before the first change; previous = (workloads, images) of the rolled-back release."""
    env = service.prod
    workloads, images = previous
    record = rr.new_record(
        env,
        kind="rollback",
        commit=target.get("commit") or "unknown",
        images=target.get("images") or {},
        build=rr.build_info(env, args.build_id),
        previous={
            "release_id": latest["release_id"],
            "commit": latest.get("commit"),
            "recorded": True,
            "images": images,
            "workloads": workloads,
            "deploy_finished": (latest.get("times") or {}).get("deploy_finished"),
        },
        started_at=rr.utc_now(),
        token=None,
    )
    record["rollback"] = {
        "requested_by": args.requested_by,
        "reason": args.reason,
        "rolled_back_release": latest["release_id"],
        "plan": [asdict(step) for step in plan.steps],
        "not_rolled_back": plan.not_rolled_back,
        "settings_compared": plan.settings_compared,
    }
    if latest.get("artifacts"):
        record["previous"]["artifacts"] = latest["artifacts"]
    if plan.target_artifacts:
        record["artifacts"] = dict(plan.target_artifacts)
    return rr.create_record(store, record, "rollback_started")


def finish_rollback(
    service: object,
    store: rr.Store,
    args: argparse.Namespace,
    record: dict,
    latest: dict,
    target: dict,
    after: dict,
    checks: list,
    failures: list[str],
    *,
    cleanup_failures: list[str] | None = None,
) -> int:
    """Write the result on the rollback record; a complete rollback moves latest.json and holds prod deploys."""
    title = service.title
    cleanup_failures = cleanup_failures or []
    all_failures = failures + cleanup_failures
    status = "deploy_failed" if failures else "deployed"

    def finish(current: dict) -> None:
        """Write the result on the rollback record; a complete rollback also gets the promote hold."""
        current["workloads"] = after
        current["status"] = status
        if target.get("artifacts"):
            current["artifacts"] = dict(target["artifacts"])
        current["times"]["deploy_finished"] = rr.utc_now()
        current["checks"] = {
            "rollback": {
                "at": rr.utc_now(),
                "checks": [asdict(check) for check in checks],
                "failures": all_failures,
                "cleanup_failures": cleanup_failures,
            }
        }
        current["verdict"] = {"status": "failed" if all_failures else "passed", "at": rr.utc_now(), "phase": "rollback"}
        if not failures:
            # Automatic prod deploys pause until the bad change is reverted or fixed (release_gate.py).
            current["promote_hold"] = rg.new_hold(service, current["release_id"], latest)

    saved = rr.update_record(store, record["release_id"], finish, status)
    where = store.location(rr.record_name(saved["release_id"]))
    summary = (
        f"{str(latest.get('commit'))[:9]} -> {str(target.get('commit'))[:9]}, started by {args.requested_by}: "
        f"{args.reason[:200]}"
    )
    if failures:
        # latest.json stays on the release that was rolled back, so a retry with
        # the default target "previous" aims at the same good release again.
        rr.post_status(
            "down",
            service.name,
            detail=(
                f"{title} prod rollback incomplete ({summary}). {failures[0][:300]}. "
                f"Retry with the same target. Record {where}"
            ),
        )
        for failure in failures:
            print(f"FAILED: {failure}", file=sys.stderr)
        return 1
    rr.point_latest(store, saved)

    def mark_rolled_back(current: dict) -> None:
        """Mark the release that was rolled back and name the rollback record."""
        current["status"] = "rolled_back"
        current["rolled_back_by"] = saved["release_id"]

    rr.update_record(
        store, latest["release_id"], mark_rolled_back, "rolled_back", {"by": saved["release_id"], "reason": args.reason}
    )
    hold = saved.get("promote_hold") or {}
    # One line for the rolled-back, held state; a failed candidate cleanup is in the record and the log.
    cleanup = (
        f" Candidate cleanup failed: {cleanup_failures[0][:300]}. Repair the candidate tags; do not roll back again."
        if cleanup_failures
        else ""
    )
    rr.post_status(
        "on hold",
        service.name,
        detail=(
            f"{title} prod rolled back ({summary}); automatic prod deploys are on hold after rollback "
            f"{saved['release_id']} of {latest['release_id']}. To resume: {hold.get('resume')}.{cleanup} Record {where}"
        ),
    )
    if cleanup_failures:
        return 1
    print(f"Rollback complete: {where}")
    return 0


def _apply(args: argparse.Namespace, service: rc.Service, store: rr.Store, clients: rr.Clients) -> int:
    """Apply the rollback: write a rollback record, set the images, verify, and hold automatic prod deploys."""
    env = service.prod
    assert_alone(service, args.build_id)
    latest, target, before, plan = _plan_for(service, store, clients, args.target)
    check_expect_latest(args, latest)
    print_plan(service, plan)
    if plan.problems:
        raise rr.RecordError(f"rollback blocked before any change: {plan.problems[0]}")
    if args.dry_run:
        print("\nDry run (_DRY_RUN=true): nothing changed.")
        return 0
    if not plan.changes():
        # A retry after an attempt that set every image but did not finish: wait,
        # verify and record as usual instead of reporting success.
        print("Every Deployment is already set to the target; waiting for rollouts and verifying.")
    record = create_rollback_record(service, store, args, latest, target, rolled_back_images(env, latest, before), plan)
    failures = apply_plan(plan, clients)
    after = rr.snapshot_workloads(clients)
    # Each Deployment is checked against its own planned digest (a snapshot target can hold two).
    targets = {step.workload: rr.digest_of(step.target) for step in plan.steps if step.target}
    check = rv.check_images(record, after, targets=targets)
    if check.status == "failed":
        failures += check.details[1:]
    return finish_rollback(service, store, args, record, latest, target, after, [check], failures)


def cmd_run(args: argparse.Namespace, service: rc.Service, store: rr.Store, clients: rr.Clients) -> int:
    """Start the rollback build by hand."""
    if not args.reason.strip():
        raise rr.RecordError("--reason is required: say why prod rolls back")
    requested_by = args.requested_by or os.environ.get("USER") or getpass.getuser()
    # Substitution values go through a custom delimiter; keep the reason on one line without it.
    reason = " ".join(args.reason.replace("|", "/").split())
    dry_run = "true" if args.dry_run else "false"
    substitutions = f"^|^_TARGET={args.to}|_REASON={reason}|_REQUESTED_BY={requested_by}|_DRY_RUN={dry_run}"
    trigger = [
        "gcloud",
        "builds",
        "triggers",
        "run",
        service.rollback_trigger,
        f"--region={service.region}",
        f"--project={service.project}",
        f"--branch={service.release_branch}",
        f"--substitutions={substitutions}",
        "--format=value(metadata.build.id)",
    ]
    try:
        build_id = rr.run_command(trigger).strip()
    except rr.RecordError as error:
        if "NOT_FOUND" not in str(error) and "not found" not in str(error).lower():
            raise
        config = REPO_ROOT / service.rollback_config
        print(f"No {service.rollback_trigger} trigger; submitting {config} from this checkout", flush=True)
        submit = [
            "gcloud",
            "builds",
            "submit",
            str(REPO_ROOT),
            f"--config={config}",
            f"--region={service.region}",
            f"--project={service.project}",
            f"--substitutions={substitutions}",
            "--async",
            "--format=value(id)",
        ]
        build_id = rr.run_command(submit, timeout=900).strip()
    env = service.prod
    print(f"Rollback build {build_id} started{' (dry run)' if args.dry_run else ''}: {rr.build_url(env, build_id)}")
    print(f"Follow it: gcloud builds log --stream --region={env.region} --project={env.project} {build_id}")
    if args.wait:
        os.execvp(
            "gcloud",
            shlex.split(f"gcloud builds log --stream --region={env.region} --project={env.project} {build_id}"),
        )
    return 0


def _token(service: rc.Service) -> str | None:
    """The GitHub token of prod, or None (with a warning) when there is none."""
    try:
        return rr.github_token(service.prod)
    except GuardError as error:
        print(f"WARNING: no GitHub token, the plan cannot list changed manifests: {error}", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run plan, run or apply on prod; return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="dry run: print what a rollback would change")
    rr.add_common_args(plan)
    plan.add_argument("--to", default="previous", help='"previous", "previous-of:<release id>" or a release id')
    plan.add_argument("--json", help="also write the plan as JSON to this file")

    run = commands.add_parser("run", help="start the rollback build")
    rr.add_common_args(run)
    run.add_argument("--to", default="previous")
    run.add_argument("--reason", required=True)
    run.add_argument("--requested-by")
    run.add_argument("--dry-run", action="store_true", help="the build prints the plan and changes nothing")
    run.add_argument("--wait", action="store_true", help="stream the build log")

    apply = commands.add_parser("apply", help="inside the rollback build only: apply the rollback")
    rr.add_common_args(apply)
    apply.add_argument("--target", default="previous")
    apply.add_argument("--reason", default="")
    apply.add_argument("--requested-by", default="unknown")
    apply.add_argument("--build-id")
    apply.add_argument("--dry-run", action="store_true")
    apply.add_argument("--expect-latest", default="", help="refuse unless this release id is the newest record")

    args = parser.parse_args(argv)
    handlers = {"plan": cmd_plan, "run": cmd_run, "apply": cmd_apply}
    try:
        if args.env != "prod":
            raise rc.ConfigError("rollbacks are for prod only")
        if rc.config_kind(args.service) != "gke":
            # Cloud Run and Composer (release_light.py, tier full): the same run, lock, record and hold;
            # the plan and the change are the kind's own.
            import release_light as rl  # noqa: PLC0415 - release_light imports this module

            service = rl.load_light(args.service)
            if not service.has_gate:
                raise rc.ConfigError(
                    f"{service.name} is tier {service.tier}: run the command its alert prints "
                    f"({service.kit_command('release_light.py', 'rollback-command')})"
                )
            store = rl.open_store(service, args.dry_run_dir, uri=args.store)
            light = {"plan": rl.cmd_rollback_plan, "run": cmd_run, "apply": rl.cmd_rollback_apply}
            return light[args.command](args, service, store, None)
        service, _, store, clients = rr.open_from_args(args)
        return handlers[args.command](args, service, store, clients)
    except (rr.RecordError, GuardError, rc.ConfigError, subprocess.SubprocessError, KeyError, OSError) as error:
        print(f"release rollback: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
