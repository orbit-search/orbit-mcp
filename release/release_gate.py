#!/usr/bin/env python3
"""Prod deploys of one service: the gate after dev verify, the hold after a rollback, the freeze.

Full tier: a merge deploys to dev (trigger <service>-dev). When dev verify
passes, the dev build runs `request-promote`, which starts the prod build for
the same commit (trigger <service>-prod, `--sha`). The prod build runs
`check-prod` again before it changes anything. Nobody moves a tag.

Tiers without a dev stage: the prod build of a merge runs `check-direct`
before it changes anything: the freeze and the hold apply, dev verify does not.

A promote goes ahead only when all of these hold:

- No freeze: a human can stop every automatic prod deploy (`freeze`) and start
  them again (`unfreeze`). The switch is `promote_freeze.json` in the prod store.
- Full tier: dev verify of the commit passed (`commits/<sha>.json` in the dev
  store), the commit is the newest verified one (prod does not run it or a newer
  commit, and no newer commit passed dev verify), and the prod build deploys
  exactly the image digest that dev verify checked.
- No hold: after a rollback, prod deploys pause. Otherwise the next merge ships
  the bad change again and rolls back again. The hold is the `promote_hold` field
  of the rollback record (the newest prod record). It ends when the commit to
  deploy contains, after the rolled-back release:
    * a revert of every held PR, or
    * a commit with the trailer `Fixes-release: <rolled-back release id>`,
  or when a human runs `resume`. A fix in the history lets only that commit and
  its descendants pass (`promote_hold.fixed_by` records it); the hold ends for
  good when a deploy replaces the rollback as the newest record, or when a
  human resumes.

    python3 release/release_gate.py status
    python3 release/release_gate.py freeze --reason "<why>"
    python3 release/release_gate.py unfreeze --reason "<why>"
    python3 release/release_gate.py resume --reason "<why>"
    python3 release/release_gate.py promote [--commit <sha>]   # full tier, break glass

Part of the release kit (orbit-infrastructure/release-kit); ported from
deep-search bin/release_gate.py (#2313) and orbit-api #1531.
Stdlib only; it runs in the Cloud Build kubectl and gcloud images (Python 3.10).
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_config as rc  # noqa: E402
import release_record as rr  # noqa: E402
from promote_release_guard import GoogleApi, GuardError, github_compare_status  # noqa: E402

EXIT_GO = 0
EXIT_ERROR = 1
EXIT_SKIP = 4

FREEZE_OBJECT = "promote_freeze.json"
# The newest commit whose dev verify passed (release_verify.py writes it in the dev store).
DEV_VERIFIED_POINTER = "verified_latest.json"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
TRAILER_RE = re.compile(r"^Fixes-release:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE)
REVERTS_COMMIT_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})", re.IGNORECASE)


def dev_commit_index(commit: str) -> str:
    """The dev store object that holds a commit's newest dev verify verdict."""
    return f"commits/{commit}.json"


@dataclass
class Decision:
    status: str  # go, frozen, held, stale, unverified
    reason: str
    resume_hint: str = ""
    # When the commit's own history ends a hold: (rollback record id, how). Only this commit and its
    # descendants pass; the hold stays until a deploy replaces the rollback or a human resumes.
    fix_seen: tuple[str, str] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def go(self) -> bool:
        return self.status == "go"


# --------------------------------------------------------------------------- hold


def hold_prs(record: dict) -> list[dict]:
    """The PRs a rollback of this release holds on: the suspects that change source, else all its PRs."""
    suspects = (record.get("rollback_suggestion") or {}).get("suspected_prs") or []
    if suspects:
        changed = [item for item in suspects if item.get("score", 0) >= 2]
        items = (changed or suspects[:1])[:3]
    else:
        items = [item for item in record.get("commits") or [] if item.get("sha")]
    return [{key: item.get(key) for key in ("pr", "sha", "title", "author")} for item in items]


def new_hold(service: rc.Service, rollback_id: str, rolled_back: dict) -> dict:
    """The promote_hold field that a completed rollback writes on its record."""
    prs = hold_prs(rolled_back)
    return {
        "status": "held",
        "since": rr.utc_now(),
        "rolled_back_release": rolled_back["release_id"],
        "rolled_back_commit": rolled_back.get("commit"),
        "prs": prs,
        "resume": resume_hint(service, rolled_back["release_id"], prs),
    }


def resume_hint(service: rc.Service, release_id: str, prs: list[dict]) -> str:
    numbers = ", ".join(f"#{item['pr']}" if item.get("pr") else str(item.get("sha", ""))[:9] for item in prs)
    revert = f"merge a revert of {numbers}, or " if numbers else ""
    return (
        f"{revert}merge a commit whose message has the trailer `Fixes-release: {release_id}`, "
        f"or run `{service.kit_command('release_gate.py', 'resume')} --reason '<why>'`"
    )


def hold_of(service: rc.Service, rollback: dict) -> dict:
    """The hold of a rollback record; a rollback without the field holds too."""
    if rollback.get("promote_hold"):
        return rollback["promote_hold"]
    rolled_back = (rollback.get("rollback") or {}).get("rolled_back_release") or ""
    return {
        "status": "held",
        "rolled_back_release": rolled_back,
        "rolled_back_commit": (rollback.get("previous") or {}).get("commit"),
        "prs": [],
        "resume": resume_hint(service, rolled_back, []),
    }


def is_revert_of(message: str, item: dict, repo: str) -> bool:
    """Whether a commit message reverts the PR or commit of a hold item."""
    sha = str(item.get("sha") or "")
    if sha and any(sha.startswith(found) for found in REVERTS_COMMIT_RE.findall(message)):
        return True
    number = item.get("pr")
    if number and re.search(rf"\bReverts {re.escape(repo)}#{number}\b", message):
        return True
    title = rr.PR_IN_TITLE_RE.sub("", str(item.get("title") or "")).strip()
    if not title:
        return False
    first_line = message.splitlines()[0] if message else ""
    # The whole quoted title, as git and GitHub write it: Revert "<title>" or Revert "<title> (#<n>)".
    quoted = [f'Revert "{title}"'] + ([f'Revert "{title} (#{number})"'] if number else [])
    return any(first_line == text or first_line.startswith(f"{text} ") for text in quoted)


def hold_ended_by(hold: dict, rollback_id: str, messages: list[tuple[str, str]], repo: str) -> str | None:
    """How the commits after the rolled-back release end the hold, or None."""
    ids = {hold.get("rolled_back_release"), rollback_id} - {None, ""}
    for sha, message in messages:
        for value in TRAILER_RE.findall(message):
            if value in ids:
                return f"commit {sha[:9]} has Fixes-release: {value}"
    prs = hold.get("prs") or []
    if prs:
        reverted = [item for item in prs if any(is_revert_of(message, item, repo) for _, message in messages)]
        if len(reverted) == len(prs):
            names = ", ".join(f"#{item['pr']}" if item.get("pr") else str(item["sha"])[:9] for item in prs)
            return f"reverted {names}"
    return None


# --------------------------------------------------------------------------- GitHub


COMPARE_PAGE_SIZE = 250


def commit_messages(repo: str, base: str, head: str, token: str, max_pages: int = 40) -> list[tuple[str, str]]:
    """(sha, full message) of every commit in base..head, oldest first, over every page of the compare."""
    messages: list[tuple[str, str]] = []
    for page in range(1, max_pages + 1):
        payload = rr.github_json(
            f"repos/{repo}/compare/{base}...{head}?per_page={COMPARE_PAGE_SIZE}&page={page}", token
        )
        commits = payload.get("commits") or []
        messages += [(item["sha"], item["commit"]["message"]) for item in commits]
        if len(commits) < COMPARE_PAGE_SIZE or len(messages) >= int(payload.get("total_commits") or 0):
            return messages
    raise GuardError(f"more than {max_pages * COMPARE_PAGE_SIZE} commits in {base[:9]}..{head[:9]}")


def is_newer(repo: str, older: str, newer: str, token: str) -> bool:
    """Whether `newer` contains `older` and more (GitHub compare older...newer is "ahead")."""
    return github_compare_status(repo, older, newer, token) == "ahead"


def contains(repo: str, commit: str, head: str, token: str) -> bool:
    """Whether `head` is `commit` or contains it."""
    return commit == head or github_compare_status(repo, commit, head, token) in ("ahead", "identical")


# --------------------------------------------------------------------------- the gate


@dataclass
class Stores:
    service: rc.Service
    prod: rr.Store
    dev: rr.Store | None


def open_stores(service: rc.Service, uri: str = "", dry_run_dir: Path | None = None) -> Stores:
    token = GoogleApi(service.project, service.region).access_token
    uri = uri or service.store
    return Stores(
        service=service,
        prod=rr.open_store(uri, service.prod, token, dry_run_dir / "prod" if dry_run_dir else None),
        dev=(
            rr.open_store(uri, service.dev, token, dry_run_dir / "dev" if dry_run_dir else None)
            if service.dev is not None
            else None
        ),
    )


def freeze_state(stores: Stores) -> dict:
    state, _ = stores.prod.read(FREEZE_OBJECT)
    return state or {"frozen": False}


def dev_index(stores: Stores, commit: str) -> dict:
    if stores.dev is None:
        return {}
    index, _ = stores.dev.read(dev_commit_index(commit))
    return index or {}


def dev_verdict(stores: Stores, commit: str) -> dict:
    return dev_index(stores, commit).get("verdict") or {}


def _frozen(stores: Stores) -> Decision | None:
    freeze = freeze_state(stores)
    if not freeze.get("frozen"):
        return None
    return Decision(
        "frozen",
        f"automatic prod deploys are frozen by {freeze.get('by')} since {freeze.get('at')}: {freeze.get('reason')}",
        f"run `{stores.service.kit_command('release_gate.py', 'unfreeze')} --reason '<why>'`",
    )


def _hold(stores: Stores, commit: str, token: str, pointer: dict | None) -> Decision:
    """The hold part of the gate: go when the newest prod record is no rollback or the hold ended."""
    service = stores.service
    repo = service.github_repo
    if not pointer or pointer.get("kind") != "rollback":
        return Decision("go", "no hold")
    rollback = rr.read_record(stores.prod, pointer["release_id"])
    hold = hold_of(service, rollback)
    if hold.get("status") == "released":
        released = hold.get("released") or {}
        return Decision("go", f"a human ended the hold after rollback {rollback['release_id']}: {released.get('how')}")
    base = hold.get("rolled_back_commit")
    ended = None
    if base and base != commit:
        if not contains(repo, base, commit, token):
            return Decision(
                "held",
                f"{commit[:9]} does not contain the rolled-back commit {base[:9]}",
                hold.get("resume") or "",
            )
        ended = hold_ended_by(hold, rollback["release_id"], commit_messages(repo, base, commit, token), repo)
    if ended:
        return Decision("go", f"{ended}; the hold ends for this commit", fix_seen=(rollback["release_id"], ended))
    return Decision(
        "held",
        f"prod release {hold.get('rolled_back_release')} was rolled back ({rollback['release_id']})",
        hold.get("resume") or "",
    )


def decide(stores: Stores, commit: str, token: str, images: dict | None = None) -> Decision:
    """Whether the commit may be promoted to prod now (full tier).

    images (the prod build): the image it is about to deploy; it must be the digest that dev
    verify checked.
    """
    repo = stores.service.github_repo
    if frozen := _frozen(stores):
        return frozen
    index = dev_index(stores, commit)
    verdict = index.get("verdict") or {}
    if verdict.get("status") != "passed":
        return Decision("unverified", f"dev verify of {commit[:9]} is {verdict.get('status') or 'missing'}")
    if images is not None:
        verified = index.get("images") or {}
        moved = [
            f"{role} {rr.digest_of(image)} (dev verified {rr.digest_of(verified.get(role))})"
            for role, image in images.items()
            if rr.digest_of(image) != rr.digest_of(verified.get(role))
        ]
        if moved:
            return Decision("unverified", f"prod would deploy images that dev verify did not check: {'; '.join(moved)}")
    pointer, _ = stores.prod.read("latest.json")
    prod_commit = (pointer or {}).get("commit")
    if pointer and pointer.get("kind") != "rollback" and prod_commit and contains(repo, commit, prod_commit, token):
        # Only a release that deployed counts: a failed deploy may have left prod on the old image.
        status = rr.read_record(stores.prod, pointer["release_id"]).get("status")
        if status == "deployed":
            return Decision("stale", f"prod already runs {prod_commit[:9]}, which contains {commit[:9]}")
    verified_pointer, _ = stores.dev.read(DEV_VERIFIED_POINTER) if stores.dev is not None else (None, 0)
    verified_commit = (verified_pointer or {}).get("commit")
    if verified_commit and verified_commit != commit and is_newer(repo, commit, verified_commit, token):
        return Decision("stale", f"a newer commit {verified_commit[:9]} passed dev verify and promotes instead")
    hold = _hold(stores, commit, token, pointer)
    if hold.go and hold.reason == "no hold":
        return Decision("go", f"dev verify of {commit[:9]} passed")
    return hold


def decide_direct(stores: Stores, commit: str, token: str) -> Decision:
    """Whether a prod build without a dev stage may deploy the commit now: the freeze and the hold."""
    if frozen := _frozen(stores):
        return frozen
    pointer, _ = stores.prod.read("latest.json")
    hold = _hold(stores, commit, token, pointer)
    if hold.go and hold.reason == "no hold":
        return Decision("go", "no freeze and no hold")
    return hold


def release_hold(stores: Stores, rollback_id: str, how: str, by: str) -> None:
    """A human ends the hold of a rollback record: every commit may deploy again (idempotent)."""

    def change(current: dict) -> None:
        hold = hold_of(stores.service, current)
        if hold.get("status") == "released":
            return
        hold["status"] = "released"
        hold["released"] = {"at": rr.utc_now(), "by": by, "how": how}
        current["promote_hold"] = hold

    rr.update_record(stores.prod, rollback_id, change, "promote_hold_released", {"by": by, "how": how})


def note_fix(stores: Stores, rollback_id: str, how: str, commit: str, by: str) -> None:
    """Record on the hold which commit fixed the rolled-back release. The hold stays: only that commit and
    its descendants pass (decide checks each commit's own history), until a deploy replaces the rollback."""

    def change(current: dict) -> None:
        hold = hold_of(stores.service, current)
        if (hold.get("fixed_by") or {}).get("commit") == commit:
            return
        hold["fixed_by"] = {"at": rr.utc_now(), "by": by, "how": how, "commit": commit}
        current["promote_hold"] = hold

    rr.update_record(stores.prod, rollback_id, change, "promote_hold_fix_seen", {"commit": commit, "how": how})


# --------------------------------------------------------------------------- promote


def start_prod_build(service: rc.Service, commit: str, dev_build_id: str, *, dry_run: bool) -> str:
    command = [
        "gcloud",
        "builds",
        "triggers",
        "run",
        service.prod.deploy_trigger,
        f"--region={service.region}",
        f"--project={service.project}",
        f"--sha={commit}",
        f"--substitutions=_DEV_BUILD_ID={dev_build_id}",
        "--format=value(metadata.build.id)",
    ]
    if dry_run:
        print(f"Dry run: would run {' '.join(command)}", flush=True)
        return "dry-run"
    return rr.run_command(command, timeout=120).strip()


def _build_url(service: rc.Service, build_id: str) -> str:
    return f"https://console.cloud.google.com/cloud-build/builds;region={service.region}/{build_id}?project={service.project}"


def _note_promote(stores: Stores, commit: str, promote: dict) -> None:
    def change(current: dict | None) -> dict:
        updated = dict(current or {"commit": commit})
        updated["promote"] = promote
        return updated

    if stores.dev is None:
        return
    try:
        rr.update_object(stores.dev, dev_commit_index(commit), change)
    except (rr.RecordError, GuardError, OSError, ValueError) as error:
        print(f"WARNING: could not note the promote in the dev index: {error}", file=sys.stderr, flush=True)


def cmd_request_promote(args: argparse.Namespace, stores: Stores) -> int:
    """Dev build, last step: start the prod build when the gate allows it. Never fails the dev build."""
    service = stores.service
    title = service.title
    short = args.commit[:9]
    merge_trigger = service.dev.deploy_trigger if service.dev else ""
    if args.trigger_name != merge_trigger or args.auto_promote != "true":
        print(
            f"No automatic promote: this dev build ran from {args.trigger_name or 'no trigger'} "
            f"with _AUTO_PROMOTE={args.auto_promote}"
        )
        return EXIT_GO
    try:
        decision = decide(stores, args.commit, rr.github_token(service.prod))
        print(f"Gate: {decision.status}: {decision.reason}", flush=True)
        if not decision.go:
            _note_promote(stores, args.commit, {"status": decision.status, "reason": decision.reason})
            if decision.status in ("frozen", "held"):
                icon = ":no_entry_sign:" if decision.status == "frozen" else ":pause_button:"
                rr.post_slack(
                    f"{icon} {title} dev verify passed for {short}; prod promote {decision.status}: "
                    f"{decision.reason}. To resume: {decision.resume_hint}."
                )
            return EXIT_GO
        if decision.fix_seen and not args.dry_run:
            note_fix(stores, decision.fix_seen[0], decision.fix_seen[1], args.commit, "dev build")
        build_id = start_prod_build(service, args.commit, args.build_id, dry_run=args.dry_run)
        if args.dry_run:
            # A test run of the dev build: say what would happen, record nothing.
            rr.post_slack(
                f":test_tube: TEST RUN, nothing is wrong. {title} dev verify passed for {short}; the gate says go "
                f"({decision.reason}), and a real merge would start the prod promote now. Nothing was promoted."
            )
            return EXIT_GO
        _note_promote(stores, args.commit, {"status": "started", "build_id": build_id, "at": rr.utc_now()})
        rr.post_slack(
            f":arrow_double_up: {title} dev verify passed for {short}; prod promote started "
            f"(<{_build_url(service, build_id)}|prod build>). {decision.reason}."
        )
    except Exception as error:  # noqa: BLE001 - a failure here must reach a human, not end the dev build red
        print(f"WARNING: the prod promote did not start: {error}", file=sys.stderr, flush=True)
        rr.post_slack(
            f":x: {title} dev verify passed for {short}, but the prod promote did not start: {str(error)[:200]}. "
            f"Start it by hand: {service.kit_command('release_gate.py', 'promote')} --commit {args.commit}"
        )
    return EXIT_GO


def _skip(args: argparse.Namespace, decision: Decision) -> None:
    if args.skip_reason_file:
        hint = f" To resume: {decision.resume_hint}." if decision.resume_hint else ""
        Path(args.skip_reason_file).write_text(f"{decision.status}: {decision.reason}.{hint}\n", encoding="utf-8")


def cmd_check_prod(args: argparse.Namespace, stores: Stores) -> int:
    """Prod build of the full tier, after preflight: 0 go, 4 skip (frozen, held, stale), 1 blocked.

    The image preflight resolved must be the digest that dev verify checked. With --build-id the
    build takes the prod lock first and keeps it on go, until `release_record.py finish`.
    """
    images = {rr.IMAGE_ROLE: Path(args.image_file).read_text(encoding="utf-8").strip()}
    with gate_lock(stores, args.build_id) as lock:
        decision = decide(stores, args.commit, rr.github_token(stores.service.prod), images=images)
        print(f"Gate: {decision.status}: {decision.reason}", flush=True)
        if decision.go:
            lock.keep()
            if decision.fix_seen:
                note_fix(stores, decision.fix_seen[0], decision.fix_seen[1], args.commit, "prod build")
            return EXIT_GO
    _skip(args, decision)
    return EXIT_ERROR if decision.status == "unverified" else EXIT_SKIP


# How long a prod build waits at the gate for a running rollback, and for the prod lock.
ROLLBACK_WAIT_SECONDS = 2700


def wait_for_rollbacks(
    service: rc.Service,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    api: GoogleApi | None = None,
) -> None:
    """Wait while a rollback build runs: its result (a hold) decides whether this build may deploy.

    The single-flight step waited for rollbacks too, but one can start between that step and this one.
    """
    api = api or GoogleApi(service.project, service.region)
    deadline = clock() + ROLLBACK_WAIT_SECONDS
    while rollbacks := api.ongoing_rollbacks(service.rollback_build_tag):
        if clock() >= deadline:
            raise GuardError(
                f"rollback build {rollbacks[0].describe()} still runs after {ROLLBACK_WAIT_SECONDS // 60} min"
            )
        print(f"Waiting for rollback build {rollbacks[0].describe()}", flush=True)
        sleep(15)


def cmd_check_direct(args: argparse.Namespace, stores: Stores) -> int:
    """Prod build of a service without a dev stage, before its first change: 0 go, 4 skip (frozen, held).

    With --build-id the build takes the prod lock first and keeps it on go, until `release_record.py finish`.
    """
    with gate_lock(stores, args.build_id) as lock:
        decision = decide_direct(stores, args.commit, rr.github_token(stores.service.prod))
        print(f"Gate: {decision.status}: {decision.reason}", flush=True)
        if decision.go:
            lock.keep()
            if decision.fix_seen:
                note_fix(stores, decision.fix_seen[0], decision.fix_seen[1], args.commit, "prod build")
            return EXIT_GO
    _skip(args, decision)
    return EXIT_SKIP


class GateLock:
    """The prod lock of a deploy build at its gate: freed on leaving unless keep() was called."""

    def __init__(self, stores: Stores, build_id: str, **waits) -> None:
        self.stores = stores
        self.build_id = build_id
        self.waits = waits
        self.kept = False

    def keep(self) -> None:
        self.kept = True

    def __enter__(self) -> GateLock:
        if self.build_id:
            take_prod_lock(self.stores, self.build_id, **self.waits)
        else:
            # A gate check by hand changes nothing, so it takes no lock.
            wait_for_rollbacks(self.stores.service, **self.waits)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.build_id and not self.kept:
            rr.release_lock(self.stores.prod, self.build_id)


def gate_lock(stores: Stores, build_id: str, **waits) -> GateLock:
    return GateLock(stores, build_id, **waits)


def take_prod_lock(
    stores: Stores,
    build_id: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    api: GoogleApi | None = None,
    alive: Callable[[str], bool] | None = None,
) -> None:
    """Take the prod lock for a deploy build; a running rollback goes first.

    The deploy waits for running rollback builds before it takes the lock, and gives
    the lock back when one started meanwhile: that rollback's apply waits for the
    lock, and its hold must decide whether this build deploys.
    """
    service = stores.service
    api = api or GoogleApi(service.project, service.region)
    alive = alive or rr.build_alive(service.prod)
    while True:
        wait_for_rollbacks(service, sleep=sleep, clock=clock, api=api)
        rr.acquire_lock(
            stores.prod, build_id, "deploy", alive, wait_seconds=ROLLBACK_WAIT_SECONDS, sleep=sleep, clock=clock
        )
        if not api.ongoing_rollbacks(service.rollback_build_tag):
            return
        rr.release_lock(stores.prod, build_id)


# --------------------------------------------------------------------------- human commands


def _who(args: argparse.Namespace) -> str:
    return args.by or os.environ.get("USER") or getpass.getuser()


def cmd_status(args: argparse.Namespace, stores: Stores) -> int:
    service = stores.service
    freeze = freeze_state(stores)
    print(f"service: {service.name} (tier {service.tier})")
    print(f"freeze: {'FROZEN' if freeze.get('frozen') else 'off'} {freeze if freeze.get('by') else ''}")
    pointer, _ = stores.prod.read("latest.json")
    print(f"prod:   {(pointer or {}).get('release_id')} ({(pointer or {}).get('kind')})")
    if pointer and pointer.get("kind") == "rollback":
        hold = hold_of(service, rr.read_record(stores.prod, pointer["release_id"]))
        print(f"hold:   {hold.get('status')} after {hold.get('rolled_back_release')}; resume: {hold.get('resume')}")
    else:
        print("hold:   none")
    if stores.dev is not None:
        dev_pointer, _ = stores.dev.read("latest.json")
        if dev_pointer:
            commit = dev_pointer["commit"]
            print(
                f"dev:    {dev_pointer['release_id']} verify {dev_verdict(stores, commit).get('status') or 'pending'}"
            )
        verified, _ = stores.dev.read(DEV_VERIFIED_POINTER)
        print(f"newest verified: {(verified or {}).get('commit') or 'none'}")
    return EXIT_GO


def cmd_freeze(args: argparse.Namespace, stores: Stores, frozen: bool) -> int:
    if not args.reason.strip():
        raise rr.RecordError("--reason is required")
    state = {"frozen": frozen, "by": _who(args), "reason": args.reason, "at": rr.utc_now()}
    rr.update_object(stores.prod, FREEZE_OBJECT, lambda _: state)
    word = "frozen" if frozen else "unfrozen"
    rr.post_slack(
        f":ice_cube: {stores.service.title} automatic prod deploys {word} by {state['by']}: {args.reason[:200]}"
    )
    print(f"Automatic prod deploys {word}: {stores.prod.location(FREEZE_OBJECT)}")
    return EXIT_GO


def cmd_resume(args: argparse.Namespace, stores: Stores) -> int:
    service = stores.service
    if not args.reason.strip():
        raise rr.RecordError("--reason is required")
    pointer, _ = stores.prod.read("latest.json")
    if not pointer or pointer.get("kind") != "rollback":
        print("No hold: the newest prod record is not a rollback.")
        return EXIT_GO
    release_hold(stores, pointer["release_id"], f"resumed by a human: {args.reason}", _who(args))
    after = (
        f"`{service.kit_command('release_gate.py', 'promote')}` promotes the newest verified commit now."
        if service.has_dev
        else "the next merge deploys."
    )
    rr.post_slack(
        f":arrow_forward: {service.title} automatic prod deploys resumed by {_who(args)} after rollback "
        f"{pointer['release_id']}: {args.reason[:200]}. The next merge deploys; {after}"
    )
    print(f"Hold after {pointer['release_id']} released.")
    return EXIT_GO


def cmd_promote(args: argparse.Namespace, stores: Stores) -> int:
    """Break glass (full tier): start the prod build for a dev-verified commit (default: the newest verified one)."""
    service = stores.service
    if stores.dev is None:
        raise rr.RecordError(f"{service.name} has no dev stage; run its prod trigger {service.prod.deploy_trigger}")
    commit = args.commit
    if not commit:
        verified, _ = stores.dev.read(DEV_VERIFIED_POINTER)
        commit = (verified or {}).get("commit")
    if not commit or not COMMIT_RE.match(commit):
        raise rr.RecordError("no commit: pass --commit <40-character sha>")
    decision = decide(stores, commit, rr.github_token(service.prod))
    print(f"Gate for {commit[:9]}: {decision.status}: {decision.reason}")
    if not decision.go:
        if decision.resume_hint:
            print(f"To resume: {decision.resume_hint}")
        return EXIT_SKIP
    build_id = start_prod_build(service, commit, "manual", dry_run=args.dry_run)
    print(f"Prod build {build_id}: {_build_url(service, build_id)}")
    return EXIT_GO


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--service", help="service config name (release/services/<name>.json)")
    parser.add_argument(
        "--store", default=os.environ.get("RELEASE_RECORD_STORE", ""), help="default: the config's store"
    )
    parser.add_argument("--dry-run-dir", type=Path, help="read the real stores, write only under this directory")
    commands = parser.add_subparsers(dest="command", required=True)

    request = commands.add_parser("request-promote", help="dev build: start the prod build when the gate allows")
    request.add_argument("--commit", required=True)
    request.add_argument("--build-id", required=True)
    request.add_argument("--trigger-name", default="")
    request.add_argument("--auto-promote", default="true")
    request.add_argument("--dry-run", action="store_true", help="print the trigger command, start nothing")

    check = commands.add_parser("check-prod", help="full-tier prod build: 0 go, 4 skip, 1 blocked")
    check.add_argument("--commit", required=True)
    check.add_argument("--skip-reason-file")
    check.add_argument("--image-file", required=True, help="the image (repo@digest) preflight resolved")
    check.add_argument("--build-id", default="", help="the prod build ($BUILD_ID): it takes the prod lock")

    direct = commands.add_parser("check-direct", help="prod build without a dev stage: 0 go, 4 skip")
    direct.add_argument("--commit", required=True)
    direct.add_argument("--skip-reason-file")
    direct.add_argument("--build-id", default="", help="the prod build ($BUILD_ID): it takes the prod lock")

    commands.add_parser("status", help="freeze, hold and the newest dev release")
    for name in ("freeze", "unfreeze", "resume"):
        sub = commands.add_parser(name)
        sub.add_argument("--reason", required=True)
        sub.add_argument("--by", default="")
    promote = commands.add_parser("promote", help="full tier, break glass: start the prod build now")
    promote.add_argument("--commit", default="")
    promote.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    commit = getattr(args, "commit", "")
    if args.command in ("request-promote", "check-prod", "check-direct") and not COMMIT_RE.match(commit):
        print(f"release-gate: --commit must be a full 40-character sha, got {commit!r}", file=sys.stderr)
        return EXIT_ERROR
    try:
        service = rc.load_service(args.service)
        stores = open_stores(service, args.store, args.dry_run_dir)
        if args.command == "request-promote":
            return cmd_request_promote(args, stores)
        if args.command == "check-prod":
            return cmd_check_prod(args, stores)
        if args.command == "check-direct":
            return cmd_check_direct(args, stores)
        if args.command == "status":
            return cmd_status(args, stores)
        if args.command in ("freeze", "unfreeze"):
            return cmd_freeze(args, stores, args.command == "freeze")
        if args.command == "resume":
            return cmd_resume(args, stores)
        return cmd_promote(args, stores)
    except (rr.RecordError, GuardError, rc.ConfigError, KeyError, OSError, ValueError) as error:
        print(f"release-gate: {error}", file=sys.stderr, flush=True)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
