#!/usr/bin/env python3
"""Releases of services that are not GKE Deployments: Cloud Run services and a Composer DAG folder.

The GKE scripts (release_record, release_verify, release_rollback) move image
digests on Deployments. Two Orbit services deploy another way:

- kind "cloud_run": one build deploys one or more Cloud Run services. A release is
  the serving revision of each.
- kind "composer": one build syncs dags/ into a Cloud Composer environment. A
  release is the commit.

Tier check_only (records, the window check, an alert with a printed rollback
command for a human):

- cloud_run: the build deploys new revisions that take all traffic. Deploy
  check: the new revision is ready and serves all traffic, plus the config's
  public URLs. Rollback: send the traffic back to the previous revisions.
- composer: the build syncs dags/. Deploy check: no new DAG import error.
  Rollback: run the sync trigger at the previous commit.

Tier full (the GKE full tier: dev stage, dev verify, the same artifact to prod,
release_gate.py, automatic rollback, the hold):

- cloud_run: the dev build of a merge deploys a new revision of each prod
  service with no traffic and the tag `candidate`. Dev verify: the candidate
  revisions run the images this build pushed, and the smoke checks answer on the
  tag URLs (https://candidate---<host>). The prod build builds nothing: it moves
  100% of the traffic to the revisions that dev verify checked. Rollback: the
  traffic goes back to the revisions of the target release. The dev stage is a
  revision of the prod service: it shares the prod config, secrets and data.
- composer: the dev build copies the commit's dags/ to
  data/release-candidate/<commit>/dags/ in the environment's bucket (the DAG
  processor does not read data/). Dev verify: the build parses those files with
  the environment's Airflow version and fails on an import error that the DAGs
  in prod do not have (Airflow 3.1 has no `dags list-import-errors --subdir`).
  The prod build syncs that folder to dags/. Rollback: the target commit's
  folder goes to dags/.
- vercel (tier full only): Vercel builds the production deployment of a merge;
  a required Deployment Check (the commit status orbit-release/<project>) keeps
  the domains on the old deployment. The dev build waits for that deployment
  and verifies it on its own URL with the Protection Bypass for Automation
  header (smoke checks, no error log lines, no 5xx). The prod build posts the
  status `success`, so Vercel assigns the domains to exactly that deployment
  (it promotes by API when Vercel holds the project after a rollback).
  Rollback: the domains go back to the target release's deployment
  (POST /v9/projects/<id>/rollback/<dpl>). The window reads Vercel's request
  logs by deployment id: new error log lines and the 5xx share against the
  previous deployment. See release_vercel.py.
- railway (tiers full and check_rollback): Railway builds and runs the
  service; its own GitHub auto deploy is off. Tier full: the dev build deploys
  the commit to the Railway environment that is the dev stage (staging) through
  the GraphQL API and verifies it there; the prod build deploys the same commit
  to production (Railway builds it again: the same source, not the same bytes).
  Tier check_rollback (no dev environment yet): the merge build deploys the
  commit to production after the freeze and hold gate. The window reads
  Railway: the deployment status, its error log lines and the 5xx share of its
  HTTP logs. Rollback: Railway redeploys the deployment that served before with
  its image. A release that adds a database migration never rolls back by
  itself: it holds and pings (forward-only). See release_railway.py.
- static_site (tier check_only): a site that a hosted publisher (Mintlify)
  builds and publishes on merge. The kit waits for the publisher's GitHub check
  run of the commit, then checks the live site's pages and links at +0, +15
  and +60 min (read-only). A failed check pings the PR author with the exact
  revert command and a fix prompt; nothing reverts by itself. See
  release_static.py.

Both tiers get the watch window of the GKE services: new warning and error log
lines (the config's Cloud Logging filter) compared with the 24 h before the
release, each traced to a suspected commit; composer also gets DAGs whose runs
fail after the release and not in the baseline. A failed check_only release
posts one Slack alert with a fix prompt and the rollback command; a failed full
release starts the rollback trigger (_EXPECT_LATEST) and pings the author.

    python3 release/release_light.py turn [--env dev] --commit <sha> --build-id <id>   # exit 3: superseded
    python3 release/release_light.py begin [--env dev] --commit <sha> --build-id <id> --release-id-file <file>
    python3 release/release_light.py finish [--env dev] --release-id-file <file> --status deployed
    python3 release/release_light.py check [--env dev] --release-id-file <file>
    python3 release/release_light.py schedule-window --release-id-file <file>
    python3 release/release_light.py rollback-command [--release-id <id>]
    # tier full only
    python3 release/release_light.py stage --commit <sha>                       # composer: the candidate folder
    python3 release/release_light.py fetch-dags --commit <sha> --out-dir <dir>  # composer: files for the parse
    python3 release/release_light.py parse-dags --candidate <dir> --baseline <dir> --out <file>  # Airflow image
    python3 release/release_light.py preflight --commit <sha> --build-id <id>   # prod: turn, branch, dev, artifacts
    python3 release/release_light.py still-current --commit <sha> --build-id <id>
    python3 release/release_light.py apply --artifacts-file <file>              # prod: the promote itself
    # kind vercel only
    python3 release/release_light.py vercel-wait --commit <sha> --out-dir <dir>   # exit 5: Vercel skipped the app
    python3 release/release_light.py vercel-status --commit <sha> --state pending --description <text>
    python3 release/release_light.py vercel-status --commit <sha> --from-dev-index | --from-skip-file <file>
    # kind railway only
    python3 release/release_light.py railway-deploy --env dev --commit <sha> --out-dir <dir>
    # kind static_site only
    python3 release/release_light.py static-wait --commit <sha> --out-dir <dir>   # exit 5: nothing was published

`release_gate.py` and `release_rollback.py` take these services too.

Part of the release kit (orbit-infrastructure/release-kit). Stdlib only (Python 3.10).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import promote_release_guard as guard  # noqa: E402
import release_config as rc  # noqa: E402
import release_gate as rg  # noqa: E402
import release_record as rr  # noqa: E402
import release_railway as rwy  # noqa: E402
import release_rollback as rb  # noqa: E402
import release_signals as rs  # noqa: E402
import release_static as rst  # noqa: E402
import release_vercel as rvc  # noqa: E402
import release_verify as rv  # noqa: E402
from promote_release_guard import GoogleApi, GuardError, _http_json  # noqa: E402

KINDS = ("cloud_run", "composer", "vercel", "railway", "static_site")
LIGHT_TIERS = ("check_only", "check_rollback", "full")
# The tiers of each kind; check_rollback (no dev stage, automatic rollback) is for a Railway service without
# a dev environment.
KIND_TIERS = {
    "cloud_run": ("check_only", "full"),
    "composer": ("check_only", "full"),
    "vercel": ("full",),
    "railway": ("full", "check_rollback"),
    "static_site": ("check_only",),
}
# The file the prod build's preflight writes: what it promotes (release_gate.py check-prod --artifacts-file).
ARTIFACTS_FILE = "release_artifacts.json"
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
    # The triggers whose builds deploy prod (single flight counts them all; tier full: triggers.prod).
    deploy_triggers: tuple[str, ...] = ()
    # cloud_run: the services one build deploys.
    services: tuple[str, ...] = ()
    # composer: the environment and the trigger that syncs dags/.
    environment: str = ""
    sync_trigger: str = ""
    dags_prefix: str = "dags/"
    parse_wait_seconds: int = 300
    path: Path | None = None
    tier: str = "check_only"
    # Tier full: the dev triggers (the merge trigger first) and the rollback trigger.
    dev_triggers: tuple[str, ...] = ()
    rollback_trigger_name: str = ""
    # Tier full, cloud_run: (service, base URL) of each service; the tag of the dev revision.
    service_urls: tuple[tuple[str, str], ...] = ()
    candidate_tag: str = "candidate"
    # Tier full, composer: the environment's bucket when it cannot be read; the candidate folder.
    bucket: str = ""
    candidate_prefix: str = "data/release-candidate/"
    # The identity of the rendered window and rollback builds (and of the triggers that run them).
    build_service_account: str = "cloud-build@orbit-search.iam.gserviceaccount.com"
    # Kind vercel: the team id, the project (name or id), the production domains (the first is the primary),
    # the commit status context of the Deployment Check and the Secret Manager secret of the token.
    vercel_team: str = ""
    vercel_project: str = ""
    vercel_domains: tuple[str, ...] = ()
    check_context: str = ""
    token_secret: str = rvc.DEFAULT_TOKEN_SECRET
    github_token_secret: str = rvc.DEFAULT_GITHUB_SECRET
    deployment_wait_minutes: int = 30
    assign_wait_seconds: int = 180
    log_settle_seconds: int = 30
    log_min_count: int = 3
    share_limit: rvc.ShareLimit = rvc.ShareLimit()
    # Kind railway: the project and service ids; per environment (dev, prod): the Railway environment's
    # name and id, the Secret Manager secret of its project token and its public URL. Optional: the
    # migration files that make a release forward-only, and the JSON field of a URL path that names the
    # commit the deployment runs.
    railway_project_id: str = ""
    railway_service_id: str = ""
    railway_environments: tuple[tuple[str, str], ...] = ()
    railway_environment_ids: tuple[tuple[str, str], ...] = ()
    railway_tokens: tuple[tuple[str, str], ...] = ()
    railway_urls: tuple[tuple[str, str], ...] = ()
    migrations_prefix: tuple[str, ...] = ()
    migrations_suffix: str = ""
    version_path: str = ""
    version_field: str = ""
    # Kind static_site: the live site, the publisher's GitHub check run, the pages that must answer.
    site: rst.Site | None = None

    metrics: tuple = ()
    signals = rc.Signals()

    @property
    def auto_rollback(self) -> bool:
        """Whether a failed release rolls back by itself (tiers full and check_rollback)."""
        return self.tier in ("full", "check_rollback")

    @property
    def has_gate(self) -> bool:
        """Whether deploys pass release_gate.py (the freeze, the hold; tier full also dev verify)."""
        return self.tier in ("full", "check_rollback")

    @property
    def has_dev(self) -> bool:
        """Whether the service has a dev stage (tier full)."""
        return self.tier == "full"

    @property
    def window_config(self) -> str:
        """The repository path of the release verify build config of this service."""
        return f"release/services/{self.name}.release-verify.yaml"

    @property
    def rollback_config(self) -> str:
        """The repository path of the rollback build config (tier full)."""
        return f"release/services/{self.name}.rollback.yaml"

    @property
    def release_build_config(self) -> str:
        """Kinds vercel, railway, static_site: the merge build config (the push trigger, rendered by release_kit.py)."""
        return f"release/services/{self.name}.release.yaml"

    @property
    def promote_build_config(self) -> str:
        """Kinds vercel and railway (tier full): the prod build config (the manual promote trigger, rendered)."""
        return f"release/services/{self.name}.promote.yaml"

    @property
    def rollback_trigger(self) -> str:
        """The manual trigger that runs the rollback build (tier full)."""
        return self.rollback_trigger_name or f"{self.name}-rollback"

    @property
    def rollback_build_tag(self) -> str:
        """Builds with this tag change prod (the rollback config); a promote waits for them."""
        return f"{self.name}-rollback"

    @property
    def verify_build_tag(self) -> str:
        """The tag of the release verify builds of this service."""
        return f"{self.name}-release-verify"

    def kit_command(self, script: str, sub: str = "") -> str:
        """The command line of a kit script for this service (see release_config.kit_command)."""
        return rc.kit_command(self.name, script, sub)

    def environment_named(self, name: str) -> rc.Environment:
        """The fields of an Environment that the shared record, gate and GitHub helpers read."""
        return rc.Environment(
            name=name,
            service=self.name,
            title=self.title,
            tier=self.tier,
            project=self.project,
            region=self.region,
            cluster="",
            namespace="",
            image=rc.Image(repository="", registry_location="", registry_repository="", package=""),
            deployments=(),
            smoke=self.smoke if name == "prod" else tuple(dev_smoke(self)),
            github_repo=self.github_repo,
            connection=self.connection,
            connected_repository=self.connected_repository,
            release_branch=self.release_branch,
            deploy_triggers=self.deploy_triggers if name == "prod" else self.dev_triggers,
            deploy_target=self.deploy_target(name),
            domains=self.domains(name),
        )

    def deploy_target(self, name: str) -> tuple[tuple[str, str | tuple[str, ...]], ...]:
        """Where an environment runs (the release record's deploy_target). The dev stage of cloud_run and
        composer lives in prod's own resources, so both environments name the same workload."""
        platform = rc.PLATFORMS[self.kind]
        if self.kind == "vercel":
            return rc.deploy_target(platform, vercel_project=self.vercel_project)
        if self.kind == "railway":
            return rc.deploy_target(
                platform, railway_project=self.railway_project_id, railway_service=self.railway_service_id
            )
        if self.kind == "static_site":
            return rc.deploy_target(platform)
        workload = self.services if self.kind == "cloud_run" else (self.environment,)
        return rc.deploy_target(platform, project=self.project, region=self.region, workload=workload)

    def domains(self, name: str) -> tuple[str, ...]:
        """The host names an environment serves (the release record's domains). The dev stage of vercel,
        cloud_run and composer has none of its own: it is a deployment or revision without a domain."""
        if self.kind == "railway":
            return rc.url_hosts([dict(self.railway_urls).get(name) or ""])
        if name != "prod":
            return ()
        if self.kind == "vercel":
            return self.vercel_domains
        if self.kind == "static_site" and self.site:
            return rc.url_hosts([self.site.url])
        return rc.public_hosts(self.smoke)

    @property
    def prod(self) -> rc.Environment:
        """The prod environment."""
        return self.environment_named("prod")

    @property
    def dev(self) -> rc.Environment | None:
        """The dev stage (tier full), else None."""
        return self.environment_named("dev") if self.has_dev else None

    @property
    def env(self) -> rc.Environment:
        """The prod environment (the name the check_only code reads)."""
        return self.prod

    @property
    def environments(self) -> dict[str, rc.Environment]:
        """The environments by name: prod, and dev for tier full."""
        return {"prod": self.prod, **({"dev": self.dev} if self.has_dev else {})}


def parse_light(data: dict, path: Path | None = None) -> LightService:
    """Parse a cloud_run or composer service config; raise ConfigError when it is invalid."""
    where = str(path or "service config")
    kind = str(data.get("kind") or "")
    if kind not in KINDS:
        raise rc.ConfigError(f"{where}: kind {kind!r} is not one of {KINDS}")
    tier = str(data.get("tier") or "")
    if tier not in KIND_TIERS[kind]:
        raise rc.ConfigError(f"{where}: kind {kind} supports tiers {KIND_TIERS[kind]}, not {tier!r}")
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
        build_service_account=build_service_account(data, where),
        store=str(data.get("store") or f"gs://orbit-search-release-records/{name}"),
        docs=str(data.get("docs") or "docs/RELEASE_PIPELINE.md"),
        code=code,
        checkpoints=tuple(int(value) for value in verify.get("checkpoints") or (15, 60)),
        smoke=smoke,
        # Kinds vercel and railway read their platform's logs; a static site has none.
        log_filter=""
        if kind in ("vercel", "railway", "static_site")
        else str(rc._require(verify, "log_filter", f"{where} verify")),
        log_min_spread=int(verify.get("log_min_spread") or 1),
        path=path,
        tier=tier,
    )
    if kind == "vercel":
        if tier != "full":
            raise rc.ConfigError(f"{where}: kind vercel supports tier full only")
        return parse_vercel(data, where, common, verify)
    if kind == "railway":
        return parse_railway(data, where, common, verify)
    if kind == "static_site":
        return parse_static(data, where, common)
    if tier == "full":
        return parse_full(data, where, common)
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


BUILD_SERVICE_ACCOUNT_RE = re.compile(
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$"
)


def build_service_account(data: dict, where: str) -> str:
    """The build identity of the rendered window and rollback builds: cloud-build@<project> unless the config names one.

    A service with its own least-privilege build identity names it here, so the window build that the prod build
    submits and the rollback build run as that identity, not as the shared cloud-build@.
    """
    project = str(data.get("project") or "orbit-search")
    account = str(data.get("build_service_account") or f"cloud-build@{project}.iam.gserviceaccount.com")
    if not BUILD_SERVICE_ACCOUNT_RE.fullmatch(account) or not account.endswith(f"@{project}.iam.gserviceaccount.com"):
        raise rc.ConfigError(f"{where}: build_service_account must be <name>@{project}.iam.gserviceaccount.com")
    return account


def full_triggers(data: dict, where: str, common: dict) -> dict:
    """The triggers of a tier full config: triggers.dev, triggers.prod and triggers.rollback."""
    if "deploy_triggers" in data:
        raise rc.ConfigError(f"{where}: tier full names its triggers in triggers.dev, triggers.prod, triggers.rollback")
    triggers = dict(rc._require(data, "triggers", where))  # type: ignore[arg-type]
    return dict(
        deploy_triggers=rc._names(triggers.get("prod"), f"{where} triggers.prod"),
        dev_triggers=rc._names(triggers.get("dev"), f"{where} triggers.dev"),
        rollback_trigger_name=str(triggers.get("rollback") or f"{common['name']}-rollback"),
    )


DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)


def parse_vercel(data: dict, where: str, common: dict, verify: dict) -> LightService:
    """The parts of a kind vercel config: the triggers, the team, project and domains, the window limits."""
    full = full_triggers(data, where, common)
    vercel = dict(rc._require(data, "vercel", where))  # type: ignore[arg-type]
    team = str(rc._require(vercel, "team", f"{where} vercel"))
    if not re.fullmatch(r"team_[A-Za-z0-9]+", team):
        raise rc.ConfigError(f"{where}: vercel.team must be the team id (team_...)")
    project = str(rc._require(vercel, "project", f"{where} vercel"))
    if not re.fullmatch(r"prj_[A-Za-z0-9]+|[a-z0-9][a-z0-9._-]{0,99}", project):
        raise rc.ConfigError(f"{where}: vercel.project must be the project name or id")
    domains = rc._names(vercel.get("domains"), f"{where} vercel.domains")
    for domain in domains:
        if not DOMAIN_RE.fullmatch(domain):
            raise rc.ConfigError(f"{where}: vercel.domains: {domain!r} is not a host name")
    if not common["smoke"]:
        raise rc.ConfigError(f"{where}: kind vercel needs a smoke check (dev verify calls it on the deployment URL)")
    for check in common["smoke"]:
        host = urllib.parse.urlsplit(str(check["url"])).hostname or ""
        if host not in domains or not str(check["url"]).startswith(f"https://{host}/"):
            raise rc.ConfigError(
                f"{where}: smoke url {check['url']} is not https://<a vercel.domains host>/..., so dev verify cannot "
                "call it on the deployment URL"
            )
    limit = rvc.ShareLimit(
        ratio=float(verify.get("max_5xx_ratio") or 3.0),
        absolute=float(verify.get("max_5xx_absolute") or 0.02),
        min_requests=int(verify.get("min_requests") or 50),
        min_errors=int(verify.get("min_5xx") or 5),
    )
    return LightService(
        **common,
        **full,
        vercel_team=team,
        vercel_project=project,
        vercel_domains=domains,
        check_context=str(vercel.get("check_context") or f"orbit-release/{common['name']}"),
        token_secret=str(vercel.get("token_secret") or rvc.DEFAULT_TOKEN_SECRET),
        github_token_secret=str(vercel.get("github_token_secret") or rvc.DEFAULT_GITHUB_SECRET),
        deployment_wait_minutes=int(vercel.get("deployment_wait_minutes", 30)),
        assign_wait_seconds=int(vercel.get("assign_wait_seconds", 180)),
        log_settle_seconds=int(vercel.get("log_settle_seconds", 30)),
        log_min_count=int(verify.get("log_min_count") or 3),
        share_limit=limit,
    )


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
BASE_URL_RE = re.compile(r"^https://[a-z0-9.-]+$")


def parse_railway(data: dict, where: str, common: dict, verify: dict) -> LightService:
    """The parts of a kind railway config: triggers, the project, service and environments, migrations, limits."""
    full = common["tier"] == "full"
    if "deploy_triggers" in data:
        raise rc.ConfigError(f"{where}: kind railway names its triggers in triggers.prod (and triggers.dev, tier full)")
    triggers = dict(rc._require(data, "triggers", where))  # type: ignore[arg-type]
    if not full and triggers.get("dev"):
        raise rc.ConfigError(f"{where}: tier check_rollback has no dev stage; set tier full to use triggers.dev")
    railway = dict(rc._require(data, "railway", where))  # type: ignore[arg-type]
    ids = {}
    for field in ("project_id", "service_id"):
        ids[field] = str(rc._require(railway, field, f"{where} railway"))
        if not UUID_RE.fullmatch(ids[field]):
            raise rc.ConfigError(f"{where}: railway.{field} must be a Railway id (a UUID)")
    environments = dict(rc._require(railway, "environments", f"{where} railway"))  # type: ignore[arg-type]
    needed = ("prod", "dev") if full else ("prod",)
    if sorted(environments) != sorted(needed):
        raise rc.ConfigError(
            f"{where}: tier {common['tier']} needs railway.environments {list(needed)}, not {sorted(environments)}"
        )
    names, env_ids, tokens, urls = [], [], [], []
    for env in needed:
        entry = dict(rc._require(environments, env, f"{where} railway.environments"))  # type: ignore[arg-type]
        at = f"{where} railway.environments.{env}"
        environment_id = str(rc._require(entry, "environment_id", at))
        secret = str(rc._require(entry, "token_secret", at))
        url = str(rc._require(entry, "url", at)).rstrip("/")
        if not UUID_RE.fullmatch(environment_id):
            raise rc.ConfigError(f"{at}: environment_id must be a Railway id (a UUID)")
        if not SECRET_NAME_RE.fullmatch(secret):
            raise rc.ConfigError(f"{at}: token_secret must be a Secret Manager secret name")
        if not BASE_URL_RE.fullmatch(url):
            raise rc.ConfigError(f"{at}: url must be https://<host> with no path")
        names.append((env, str(entry.get("name") or env)))
        env_ids.append((env, environment_id))
        tokens.append((env, secret))
        urls.append((env, url))
    if full and (len({value for _, value in env_ids}) < 2 or len({value for _, value in tokens}) < 2):
        raise rc.ConfigError(f"{where}: the dev and prod environments need their own environment id and token secret")
    prod_url = dict(urls)["prod"]
    if not common["smoke"]:
        raise rc.ConfigError(f"{where}: kind railway needs a smoke check")
    for check in common["smoke"]:
        if not str(check["url"]).startswith(prod_url + "/"):
            raise rc.ConfigError(
                f"{where}: smoke url {check['url']} is not under {prod_url}/, so dev verify cannot call it on the dev URL"
            )
    migrations = dict(railway.get("migrations") or {})
    # prefix: a folder (backend/migrations/) or a list of folders and files whose changes change the stored schema.
    prefixes = migrations.get("prefix") or ()
    prefixes = (prefixes,) if isinstance(prefixes, str) else tuple(prefixes)
    if migrations and (
        not prefixes or not all(isinstance(item, str) and item and not item.startswith("/") for item in prefixes)
    ):
        raise rc.ConfigError(f"{where}: railway.migrations.prefix must be a repository path or a list of them")
    version = dict(railway.get("version") or {})
    if version and (not str(version.get("path") or "").startswith("/") or not version.get("field")):
        raise rc.ConfigError(f"{where}: railway.version needs a path that starts with / and a JSON field")
    return LightService(
        **common,
        deploy_triggers=rc._names(triggers.get("prod"), f"{where} triggers.prod"),
        dev_triggers=rc._names(triggers.get("dev"), f"{where} triggers.dev") if full else (),
        rollback_trigger_name=str(triggers.get("rollback") or f"{common['name']}-rollback"),
        railway_project_id=ids["project_id"],
        railway_service_id=ids["service_id"],
        railway_environments=tuple(names),
        railway_environment_ids=tuple(env_ids),
        railway_tokens=tuple(tokens),
        railway_urls=tuple(urls),
        migrations_prefix=prefixes,
        migrations_suffix=str(migrations.get("suffix") or ""),
        version_path=str(version.get("path") or ""),
        version_field=str(version.get("field") or ""),
        deployment_wait_minutes=int(railway.get("deploy_wait_minutes", 20)),
        log_settle_seconds=int(railway.get("log_settle_seconds", 30)),
        log_min_count=int(verify.get("log_min_count") or 3),
        share_limit=rvc.ShareLimit(
            ratio=float(verify.get("max_5xx_ratio") or 3.0),
            absolute=float(verify.get("max_5xx_absolute") or 0.02),
            min_requests=int(verify.get("min_requests") or 50),
            min_errors=int(verify.get("min_5xx") or 5),
        ),
    )


def parse_static(data: dict, where: str, common: dict) -> LightService:
    """The parts of a kind static_site config: the merge trigger and the site (release_static.parse_site)."""
    triggers = rc._names(rc._require(data, "deploy_triggers", where), f"{where} deploy_triggers")
    site = rst.parse_site(dict(rc._require(data, "static_site", where)), where)  # type: ignore[arg-type]
    for check in common["smoke"]:
        if not str(check["url"]).startswith(site.url + "/"):
            raise rc.ConfigError(f"{where}: smoke url {check['url']} is not under {site.url}/")
    return LightService(**common, deploy_triggers=triggers, site=site)


def parse_full(data: dict, where: str, common: dict) -> LightService:
    """The tier full parts of a cloud_run or composer config: triggers, URLs, the candidate folder."""
    full = full_triggers(data, where, common)
    if common["kind"] == "cloud_run":
        services = rc._names(rc._require(data, "cloud_run_services", where), f"{where} cloud_run_services")
        urls = dict(rc._require(data, "cloud_run_urls", where))  # type: ignore[arg-type]
        for name in services:
            url = str(urls.get(name) or "")
            if not re.match(r"^https://[a-z0-9.-]+\.run\.app$", url):
                raise rc.ConfigError(f"{where}: cloud_run_urls.{name} must be the service's https://<host>.run.app URL")
        if set(urls) - set(services):
            raise rc.ConfigError(f"{where}: cloud_run_urls names services that cloud_run_services does not")
        tagged = set()
        for check in common["smoke"]:
            owner = [name for name in services if str(check["url"]).startswith(urls[name] + "/")]
            if not owner:
                raise rc.ConfigError(
                    f"{where}: smoke url {check['url']} is not under a cloud_run_urls base URL, so dev verify cannot "
                    "call it on the candidate tag"
                )
            tagged.add(owner[0])
        if missing := sorted(set(services) - tagged):
            raise rc.ConfigError(f"{where}: tier full needs a smoke check of every service; none for {missing}")
        return LightService(
            **common,
            **full,
            services=services,
            service_urls=tuple((name, str(urls[name])) for name in services),
            candidate_tag=str(data.get("candidate_tag") or "candidate"),
        )
    composer = dict(rc._require(data, "composer", where))  # type: ignore[arg-type]
    bucket = str(composer.get("bucket") or "").rstrip("/")
    if bucket and not bucket.startswith("gs://"):
        raise rc.ConfigError(f"{where}: composer.bucket must be gs://<bucket>")
    prefix = str(composer.get("candidate_prefix") or "data/release-candidate/").strip("/") + "/"
    if not prefix.startswith("data/"):
        raise rc.ConfigError(f"{where}: composer.candidate_prefix must be under data/ (the DAG processor reads dags/)")
    return LightService(
        **common,
        **full,
        environment=str(rc._require(composer, "environment", f"{where} composer")),
        dags_prefix=str(composer.get("dags_prefix") or "dags/"),
        parse_wait_seconds=int(composer.get("parse_wait_seconds") or 300),
        bucket=bucket,
        candidate_prefix=prefix,
    )


def load_light(name: str | None = None) -> LightService:
    """Load the named service config, or the only one; the file name must be the service name."""
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

Runner = rr.Runner


def gcloud_json(runner: Runner, *args: str) -> dict:
    """Run a gcloud command with JSON output and decode it."""
    return json.loads(runner(["gcloud", *args, "--format=json"]))


def snapshot_cloud_run(service: LightService, runner: Runner) -> dict[str, dict]:
    """Per Cloud Run service: the newest created and ready revisions, the traffic split and the image."""
    out = {}
    for name in service.services:
        data = gcloud_json(
            runner, "run", "services", "describe", name, f"--region={service.region}", f"--project={service.project}"
        )
        status = data.get("status") or {}
        traffic: dict[str, int] = {}
        tags: dict[str, str] = {}
        for item in status.get("traffic") or []:
            revision = item.get("revisionName") or (
                status.get("latestReadyRevisionName") if item.get("latestRevision") else ""
            )
            if revision:
                traffic[revision] = traffic.get(revision, 0) + int(item.get("percent") or 0)
                if item.get("tag"):
                    tags[str(item["tag"])] = revision
        containers = (((data.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or [{}]
        out[f"cloud_run/{name}"] = {
            "kind": "cloud_run",
            "name": name,
            "latest_created": status.get("latestCreatedRevisionName"),
            "latest_ready": status.get("latestReadyRevisionName"),
            "traffic": traffic,
            "tags": tags,
            "image": containers[0].get("image"),
            "url": status.get("url"),
        }
    return out


def serving_split(workload: dict) -> dict[str, int]:
    """The revisions that serve traffic in a Cloud Run snapshot, with their percent (0% tags left out)."""
    split = {revision: int(percent) for revision, percent in (workload.get("traffic") or {}).items() if percent}
    if not split and workload.get("latest_ready"):
        split = {str(workload["latest_ready"]): 100}
    return split


def split_text(split: dict[str, int]) -> str:
    """A traffic split as gcloud's --to-revisions value: rev-a=90,rev-b=10."""
    return ",".join(f"{revision}={percent}" for revision, percent in sorted(split.items()))


def candidate_url(service: LightService, name: str, url: str | None = None) -> str:
    """The URL of the candidate tag of a Cloud Run service: https://<tag>---<host>[path]."""
    base = dict(service.service_urls)[name]
    host = base.split("://", 1)[1]
    return f"https://{service.candidate_tag}---{host}" + (url[len(base) :] if url else "")


def dev_smoke(service: LightService) -> list[dict]:
    """The smoke checks of dev verify: cloud_run calls the prod checks on the candidate tag URLs, railway on the
    dev environment's URL."""
    if service.kind == "railway" and service.has_dev:
        urls = dict(service.railway_urls)
        return [
            {
                **check,
                "name": f"{check.get('name') or check['url']} ({dict(service.railway_environments)['dev']})",
                "url": urls["dev"] + str(check["url"])[len(urls["prod"]) :],
            }
            for check in service.smoke
        ]
    if service.kind != "cloud_run" or not service.service_urls:
        return []
    out = []
    for check in service.smoke:
        for name, base in service.service_urls:
            if str(check["url"]).startswith(base + "/"):
                out.append(
                    {
                        **check,
                        "name": f"{check.get('name') or name} (candidate)",
                        "url": candidate_url(service, name, check["url"]),
                    }
                )
                break
    return out


def describe_revision(service: LightService, runner: Runner, revision: str) -> dict:
    """A Cloud Run revision: its name, whether it is Ready, and the image digest it runs."""
    data = gcloud_json(
        runner, "run", "revisions", "describe", revision, f"--region={service.region}", f"--project={service.project}"
    )
    status = data.get("status") or {}
    ready = any(item.get("type") == "Ready" and item.get("status") == "True" for item in status.get("conditions") or [])
    containers = (data.get("spec") or {}).get("containers") or [{}]
    image = status.get("imageDigest") or containers[0].get("image") or ""
    return {"name": revision, "ready": ready, "image": image, "digest": rr.digest_of(image)}


def revision_artifacts(service: LightService, runner: Runner, revisions: dict[str, str]) -> dict[str, str]:
    """{service: revision@digest} of Ready revisions; raise RecordError when one is missing, not Ready or has no digest."""
    out = {}
    for name in service.services:
        revision = revisions.get(name)
        if not revision:
            raise rr.RecordError(f"no {service.candidate_tag} revision of {name}")
        info = describe_revision(service, runner, revision)
        if not info["ready"]:
            raise rr.RecordError(f"revision {revision} of {name} is not Ready")
        if not info["digest"]:
            raise rr.RecordError(f"revision {revision} of {name} has no image digest ({info['image']!r})")
        out[name] = f"{revision}@{info['digest']}"
    return out


def candidate_revisions(service: LightService, snapshot: dict[str, dict]) -> dict[str, str]:
    """{service: the revision the candidate tag names} in a Cloud Run snapshot."""
    return {
        name: rev
        for name in service.services
        if (rev := ((snapshot.get(f"cloud_run/{name}") or {}).get("tags") or {}).get(service.candidate_tag))
    }


def remove_candidate_tags(service: LightService, revisions: dict[str, str]) -> list[str]:
    """Remove only these revisions' candidate tags, with an etag precondition against concurrent dev builds.

    The Cloud Run v2 traffic-only PATCH preserves percentages and other tags. A missing etag,
    conflict or incomplete operation fails cleanup rather than removing a newer build's tag.
    """
    failures = []
    api = GoogleApi(service.project, service.region)
    for name, revision in revisions.items():
        try:
            if (
                name not in service.services
                or not isinstance(revision, str)
                or not re.fullmatch(re.escape(name) + r"-[a-z0-9-]+", revision)
            ):
                raise rr.RecordError("cleanup needs an exact revision of a configured service")
            resource = f"projects/{service.project}/locations/{service.region}/services/{name}"
            url = f"https://run.googleapis.com/v2/{resource}"
            headers = {"Authorization": f"Bearer {api.access_token()}", "Content-Type": "application/json"}
            current = _http_json(urllib.request.Request(url, headers=headers))
            traffic = current.get("traffic") or []
            matched = [
                item
                for item in traffic
                if item.get("tag") == service.candidate_tag
                and str(item.get("revision") or "").rsplit("/", 1)[-1] == revision
            ]
            if not matched:
                continue
            if not current.get("etag"):
                raise rr.RecordError("Cloud Run returned no etag; refusing an unguarded tag removal")
            updated = [
                {key: value for key, value in item.items() if key != "tag"} if item in matched else dict(item)
                for item in traffic
            ]
            operation = _http_json(
                urllib.request.Request(
                    url + "?updateMask=traffic",
                    data=json.dumps({"name": resource, "etag": current["etag"], "traffic": updated}).encode(),
                    method="PATCH",
                    headers=headers,
                )
            )
            deadline = time.monotonic() + 120
            while not operation.get("done"):
                operation_name = str(operation.get("name") or "")
                if not operation_name.startswith(f"projects/{service.project}/locations/{service.region}/operations/"):
                    raise rr.RecordError("Cloud Run returned no valid cleanup operation")
                if time.monotonic() >= deadline:
                    raise rr.RecordError("Cloud Run tag cleanup did not finish within 120 seconds")
                time.sleep(2)
                operation = _http_json(
                    urllib.request.Request(
                        f"https://run.googleapis.com/v2/{operation_name}",
                        headers=headers,
                    )
                )
            if operation.get("error"):
                raise rr.RecordError(f"Cloud Run tag cleanup failed: {operation['error']}")
            after = _http_json(urllib.request.Request(url, headers=headers))
            if any(
                item.get("tag") == service.candidate_tag
                and str(item.get("revision") or "").rsplit("/", 1)[-1] == revision
                for item in [*(after.get("traffic") or []), *(after.get("trafficStatuses") or [])]
            ):
                raise rr.RecordError("the failed revision still has its candidate tag after cleanup")
            print(f"{name}: removed {service.candidate_tag} tag from {revision}", flush=True)
        except (rr.RecordError, GuardError, OSError) as error:
            failures.append(f"{name}: {str(error)[:300]}")
    return failures


def cmd_cleanup_candidates(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Failure cleanup of explicitly named Cloud Run candidate revisions; never infer ownership from a live tag."""
    _full(service)
    if service.kind != "cloud_run":
        raise rc.ConfigError("cleanup-candidates is only for Cloud Run")
    revisions = _json_file(args.revisions_file)
    if not revisions:
        raise rr.RecordError("candidate cleanup needs the revisions planned by this dev build")
    failures = remove_candidate_tags(service, revisions)
    for failure in failures:
        print(f"CLEANUP FAILED: {failure}", file=sys.stderr)
    return rv.EXIT_ERROR if failures else rv.EXIT_OK


def dags_uri(service: LightService, runner: Runner) -> str:
    """The environment's DAG folder (gs://<bucket>/dags); the config's bucket when it cannot be read."""
    try:
        uri = runner(
            [
                "gcloud",
                "composer",
                "environments",
                "describe",
                service.environment,
                f"--location={service.region}",
                f"--project={service.project}",
                "--format=value(config.dagGcsPrefix)",
            ]
        ).strip()
    except rr.RecordError as error:
        uri = ""
        print(f"WARNING: cannot read the DAG folder of {service.environment}: {str(error)[:200]}", file=sys.stderr)
    if uri.startswith("gs://"):
        return uri.rstrip("/")
    if not service.bucket:
        raise rr.RecordError(f"no DAG folder for {service.environment} and no composer.bucket in the config")
    print(f"Using the config's bucket {service.bucket}", flush=True)
    return f"{service.bucket}/dags"


def candidate_uri(service: LightService, dags: str, commit: str) -> str:
    """The candidate folder of a commit: gs://<bucket>/data/release-candidate/<commit>/dags."""
    bucket = dags.rsplit("/", 1)[0]
    return f"{bucket}/{service.candidate_prefix}{commit}/{service.dags_prefix.strip('/')}"


GCS_LIST_PAGES = 50


def _crc32c_table() -> tuple[int, ...]:
    """Build the Castagnoli lookup table used by GCS object integrity checks."""
    table = []
    for value in range(256):
        for _ in range(8):
            value = (value >> 1) ^ (0x82F63B78 if value & 1 else 0)
        table.append(value)
    return tuple(table)


CRC32C_TABLE = _crc32c_table()


def crc32c(data: bytes) -> str:
    """Return the base64 CRC32C representation used in GCS object metadata."""
    value = 0xFFFFFFFF
    for byte in data:
        value = CRC32C_TABLE[(value ^ byte) & 255] ^ (value >> 8)
    return base64.b64encode(((value ^ 0xFFFFFFFF) & 0xFFFFFFFF).to_bytes(4, "big")).decode("ascii")


def local_dag_manifest(folder: Path) -> str:
    """Compute the expected GCS manifest from the exact commit's downloaded files, before upload."""
    lines = []
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            content = path.read_bytes()
            lines.append(f"{path.relative_to(folder).as_posix()} {crc32c(content)} {len(content)}")
    if not lines:
        raise rr.RecordError("the target commit has no DAG files")
    return "sha256:" + hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def folder_manifest(uri: str, token: Callable[[], str]) -> tuple[str, int]:
    """(sha256 over every object's relative name, crc32c and size; object count) of a GCS folder. ("", 0) when empty.

    Two folders with the same manifest hold the same bytes, so the hash names the artifact.
    """
    bucket, _, prefix = uri.removeprefix("gs://").partition("/")
    prefix = prefix.rstrip("/") + "/"
    lines: list[str] = []
    page = ""
    for _ in range(GCS_LIST_PAGES):
        query = {"prefix": prefix, "fields": "items(name,crc32c,size),nextPageToken"}
        if page:
            query["pageToken"] = page
        url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o?{urllib.parse.urlencode(query)}"
        payload = _http_json(urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}"}))
        lines += [
            f"{item['name'][len(prefix) :]} {item.get('crc32c')} {item.get('size')}"
            for item in payload.get("items") or []
            if not item["name"].endswith("/")
        ]
        page = payload.get("nextPageToken") or ""
        if not page:
            break
    else:
        raise rr.RecordError(f"{uri} has more than {GCS_LIST_PAGES} pages of objects")
    if not lines:
        return "", 0
    return "sha256:" + hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest(), len(lines)


def composer_artifacts(service: LightService, runner: Runner, token: Callable[[], str], commit: str) -> dict[str, str]:
    """{"dags": "<candidate folder> <manifest hash>"}; raise RecordError when the folder is empty."""
    uri = candidate_uri(service, dags_uri(service, runner), commit)
    digest, count = folder_manifest(uri, token)
    if not count:
        raise rr.RecordError(f"the candidate folder {uri} is empty")
    return {"dags": f"{uri} {digest}"}


def live_artifacts(
    service: LightService, runner: Runner, token: Callable[[], str], commit: str, recorded: dict[str, str]
) -> dict[str, str]:
    """What prod would promote now, read live: the recorded revisions (cloud_run), the verified deployment (vercel),
    or the candidate folder (composer)."""
    if service.kind == "vercel":
        return vercel_live_artifacts(service, vercel_api(service), commit, recorded)
    if service.kind == "railway":
        # The artifact is the commit: Railway builds it again for production (the same source, not the same bytes).
        return {service.name: commit}
    if service.kind == "cloud_run":
        return revision_artifacts(service, runner, {name: value.split("@", 1)[0] for name, value in recorded.items()})
    return composer_artifacts(service, runner, token, commit)


# --------------------------------------------------------------------------- the DAG parse (composer dev verify)

# Errors that need the environment, not the code: Airflow Variables and Connections exist only in Composer.
ENVIRONMENT_ONLY_ERRORS = re.compile(
    r"VARIABLE_NOT_FOUND|CONNECTION_NOT_FOUND|Variable \S+ (?:not found|does not exist)|conn_id .{0,40} isn't defined",
    re.IGNORECASE,
)


def parse_dag_folder(folder: Path) -> dict:
    """Parse every DAG file of a folder the way the DAG processor does (one file at a time); runs in the Airflow image.

    Returns {"airflow": version, "files": n, "dags": n, "errors": {relative path: last line of the error}}.
    """
    sys.path.insert(0, str(folder))
    os.environ.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "False")
    import airflow  # noqa: PLC0415 - only the Airflow image has it
    from airflow.models.dagbag import DagBag  # noqa: PLC0415

    try:
        from airflow.utils.file import list_py_file_paths  # noqa: PLC0415

        files = list_py_file_paths(str(folder), safe_mode=True)
    except ImportError:
        files = sorted(str(path) for path in folder.rglob("*.py"))
    errors: dict[str, str] = {}
    dags = 0
    for path in sorted(files):
        bag = DagBag(dag_folder=path, include_examples=False)
        dags += len(bag.dags)
        for name, trace in bag.import_errors.items():
            lines = [line.strip() for line in str(trace).splitlines() if line.strip()]
            relative = os.path.relpath(name, folder)
            errors[relative] = (lines[-1] if lines else "").replace(str(folder), "<dags>")[:500]
    return {"airflow": getattr(airflow, "__version__", "?"), "files": len(files), "dags": dags, "errors": errors}


def compare_parse(result: dict) -> tuple[dict[str, str], dict[str, str]]:
    """(new errors, new errors that need the environment): candidate errors that prod's DAGs do not have."""
    baseline = (result.get("baseline") or {}).get("errors") or {}
    new = {
        name: error
        for name, error in ((result.get("candidate") or {}).get("errors") or {}).items()
        if baseline.get(name) != error
    }
    environment = {name: error for name, error in new.items() if ENVIRONMENT_ONLY_ERRORS.search(error)}
    return {name: error for name, error in new.items() if name not in environment}, environment


def check_parse(service: LightService, path: str) -> rv.Check:
    """The candidate DAGs import as well as prod's: no new import error in the parse result."""
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        return rv.Check("dag_parse", "failed", [f"the DAG parse did not run or wrote no result: {error}"])
    if not isinstance(result, dict):
        return rv.Check("dag_parse", "failed", ["the DAG parse result must be an object"])
    if result.get("error"):
        return rv.Check("dag_parse", "failed", [f"the DAG parse failed: {str(result['error'])[:300]}"])
    for role in ("candidate", "baseline"):
        parsed = result.get(role)
        if (
            not isinstance(parsed, dict)
            or not isinstance(parsed.get("airflow"), str)
            or not parsed["airflow"].strip()
            or any(type(parsed.get(count)) is not int or parsed[count] < 0 for count in ("files", "dags"))
            or not isinstance(parsed.get("errors"), dict)
            or any(not isinstance(error, str) for error in parsed["errors"].values())
        ):
            return rv.Check("dag_parse", "failed", [f"the DAG parse result has incomplete or invalid {role} metadata"])
    new, environment = compare_parse(result)
    candidate = result.get("candidate") or {}
    details = [
        f"Airflow {candidate.get('airflow')}: {candidate.get('files')} files, {candidate.get('dags')} DAGs, "
        f"{len(candidate.get('errors') or {})} import errors ({len((result.get('baseline') or {}).get('errors') or {})} "
        "in prod's DAGs)"
    ]
    details += [f"{service.dags_prefix}{name}: {error[:200]}" for name, error in sorted(new.items())]
    details += [
        f"not judged (needs a Composer Variable or Connection): {service.dags_prefix}{name}: {error[:160]}"
        for name, error in sorted(environment.items())
    ]
    return rv.Check("dag_parse", "failed" if new else "passed", details)


# Airflow list pages: a day of failures of the five-minute DAGs fits in a few pages.
AIRFLOW_PAGE_SIZE = 100
AIRFLOW_MAX_PAGES = 50


class Airflow:
    """Read-only Airflow REST API of a Composer environment (the build identity's token)."""

    def __init__(self, service: LightService, runner: Runner, token: Callable[[], str]) -> None:
        """Find the Airflow URI of the Composer environment; raise RecordError when there is none."""
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
        """GET an Airflow API path with the bearer token and decode the JSON."""
        request = urllib.request.Request(f"{self.uri}{path}", headers={"Authorization": f"Bearer {self.token()}"})
        return _http_json(request)

    def all_items(self, path: str, key: str) -> list[dict]:
        """Every item of a paged Airflow list; an answer that does not reach total_entries raises.

        A partial list would make an old failure look new, so it never passes as complete.
        """
        items: list[dict] = []
        separator = "&" if "?" in path else "?"
        for _ in range(AIRFLOW_MAX_PAGES):
            payload = self.get(f"{path}{separator}limit={AIRFLOW_PAGE_SIZE}&offset={len(items)}")
            page = payload.get(key) or []
            items.extend(page)
            total = int(payload.get("total_entries") or 0)
            if len(items) >= total or not page:
                if len(items) < total:
                    raise rr.RecordError(f"Airflow {path} returned {len(items)} of {total} entries")
                return items
        raise rr.RecordError(f"Airflow {path} has more than {AIRFLOW_MAX_PAGES * AIRFLOW_PAGE_SIZE} entries")

    def import_errors(self) -> dict[str, str]:
        """{filename: first line of the error} of every DAG file that does not import now."""
        return {
            str(item.get("filename")): str(item.get("stack_trace") or "").strip().splitlines()[-1:][0]
            if item.get("stack_trace")
            else ""
            for item in self.all_items("/api/v2/importErrors", "import_errors")
        }

    def failed_runs(self, start: datetime, end: datetime) -> dict[str, int]:
        """{dag_id: failed runs} that ended in [start, end)."""
        query = (
            f"/api/v2/dags/~/dagRuns?state=failed"
            f"&end_date_gte={start.strftime('%Y-%m-%dT%H:%M:%SZ')}&end_date_lte={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )
        out: dict[str, int] = {}
        for run in self.all_items(query, "dag_runs"):
            out[str(run.get("dag_id"))] = out.get(str(run.get("dag_id")), 0) + 1
        return out


def snapshot(service: LightService, runner: Runner, token: Callable[[], str]) -> dict[str, dict]:
    """What runs now: the Cloud Run revisions, the Vercel deployment on the domains, or the Composer import errors."""
    if service.kind == "cloud_run":
        return snapshot_cloud_run(service, runner)
    if service.kind == "vercel":
        return snapshot_vercel(service, vercel_api(service))
    if service.kind == "railway":
        return snapshot_railway(service, railway_api(service, "prod"))
    if service.kind == "static_site":
        return snapshot_static(service)
    try:
        errors: dict[str, str] | None = Airflow(service, runner, token).import_errors()
        problem = ""
    except (GuardError, rr.RecordError) as error:
        # The build identity needs roles/composer.user to read the Airflow API; the log checks still run.
        errors, problem = None, f"the Airflow API cannot be read: {str(error)[:300]}"
    return {"composer/import_errors": {"kind": "composer", "errors": errors, "error": problem}}


# --------------------------------------------------------------------------- kind vercel


def vercel_api(service: LightService) -> rvc.VercelApi:
    """The Vercel API of the service's team; the token is VERCEL_RELEASE_TOKEN or the Secret Manager secret."""
    google = GoogleApi(service.project, service.region)
    return rvc.VercelApi(
        service.vercel_team, rvc.token_source(service.project, service.token_secret, google.access_token)
    )


def vercel_key(service: LightService) -> str:
    """The workload key of a Vercel project in snapshots and rollback plans."""
    return f"vercel/{service.name}"


def snapshot_vercel(service: LightService, api: rvc.VercelApi) -> dict[str, dict]:
    """The deployment each production domain points at, the commit of the primary one, and Vercel's hold state."""
    project = api.project(service.vercel_project)
    now = rvc.aliases(api, service.vercel_domains)
    primary = now.get(service.vercel_domains[0]) or ""
    commit = ""
    if primary:
        try:
            commit = rvc.deployment_commit(api.deployment(primary))
        except rvc.VercelError as error:
            print(f"WARNING: cannot read deployment {primary}: {str(error)[:200]}", file=sys.stderr, flush=True)
    return {
        vercel_key(service): {
            "kind": "vercel",
            "project_id": project.get("id"),
            "deployment": primary,
            "commit": commit,
            "aliases": now,
            "auto_assign": not rvc.in_rollback_state(project),
            "last_rollback_target": project.get("lastRollbackTarget"),
        }
    }


def status_token(service: LightService) -> str:
    """A GitHub token that can set a commit status: GITHUB_TOKEN when set (a named github_token_secret), else a
    read-write token of the Cloud Build connection, minted for this status (a read token cannot set one)."""
    if token := os.environ.get("GITHUB_TOKEN", "").strip():
        return token
    if service.github_token_secret != rvc.CONNECTION_TOKEN:
        raise rr.RecordError(
            f"GITHUB_TOKEN is not set; the builds put Secret Manager {service.github_token_secret} in it"
        )
    try:
        return guard.GoogleApi(service.env.project, service.env.region).repository_read_write_token(
            service.env.connection, service.env.connected_repository
        )
    except (guard.GuardError, subprocess.SubprocessError) as error:
        raise rr.RecordError(f"no GitHub token for the commit status: {error}") from None


def post_commit_status(service: LightService, commit: str, state: str, description: str, target_url: str = "") -> None:
    """Set the Deployment Check's commit status (service.check_context) with the kit's GitHub token."""
    rvc.post_status(
        service.github_repo,
        commit,
        service.check_context,
        state,
        description,
        status_token(service),
        target_url=target_url,
    )


def vercel_deployment_artifacts(service: LightService, api: rvc.VercelApi, commit: str, wanted: str) -> dict[str, str]:
    """{service: deployment id} of a READY production deployment of the commit; raise RecordError otherwise."""
    deployment = api.deployment(wanted) if wanted else None
    if deployment is None:
        project = api.project(service.vercel_project)
        deployment = rvc.commit_deployment(api, str(project["id"]), commit)
    if not deployment:
        raise rr.RecordError(f"no Vercel production deployment of {commit[:9]}")
    found = rvc.deployment_id(deployment)
    if rvc.deployment_state(deployment) != rvc.READY:
        raise rr.RecordError(f"deployment {found} is {rvc.deployment_state(deployment)}, not READY")
    if rvc.deployment_commit(deployment) != commit:
        raise rr.RecordError(
            f"deployment {found} was built from {rvc.deployment_commit(deployment)[:9] or 'no commit'}"
        )
    if deployment.get("target") != "production":
        raise rr.RecordError(f"deployment {found} is not a production deployment")
    return {service.name: found}


def vercel_live_artifacts(service: LightService, api: rvc.VercelApi, commit: str, recorded: dict) -> dict[str, str]:
    """What the prod build lets go live: the verified deployment, still READY and the newest one of the branch.

    A newer production deployment of another commit on the release branch supersedes this one (exit 3):
    Vercel gives the domains to the newest deployment whose check passed, so an older one must not pass.
    """
    wanted = str(recorded.get(service.name) or "")
    if not wanted:
        raise rr.RecordError("dev verify recorded no Vercel deployment")
    artifacts = vercel_deployment_artifacts(service, api, commit, wanted)
    project = api.project(service.vercel_project)
    newer = rvc.newer_deployment(api, str(project["id"]), api.deployment(wanted), service.release_branch)
    if newer:
        raise guard.Superseded(
            f"a newer production deployment {rvc.deployment_id(newer)} of "
            f"{rvc.deployment_commit(newer)[:9]} ({rvc.deployment_state(newer)}) releases instead"
        )
    return artifacts


def vercel_hold_guard(service: LightService, store: rr.Store, api: rvc.VercelApi, project: dict | None = None) -> None:
    """Refuse a promote while Vercel holds the project after a rollback that the kit did not record.

    After any rollback Vercel turns auto-assignment off. A kit rollback is the newest prod record, and its
    hold (release_gate.py) decides. A rollback in the dashboard has no record: promoting would undo it.
    """
    if not rvc.in_rollback_state(project if project is not None else api.project(service.vercel_project)):
        return
    pointer, _ = store.read("latest.json")
    if pointer and pointer.get("kind") == "rollback":
        return
    raise GuardError(
        f"Vercel holds {service.vercel_project} after a rollback that the release kit did not record (the "
        "dashboard?). A human decides: undo it in Vercel ('Undo Rollback'), or record it with "
        f"`{rv.rollback_cli(service, 'run')} --reason '<why>'`; nothing was promoted"
    )


def vercel_dev_checks(
    service: LightService,
    record: dict,
    api: rvc.VercelApi,
    *,
    get: rv.HttpGet | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[rv.Check]:
    """Dev verify of kind vercel: the deployment of the commit is READY and staged, the smoke checks pass on its own
    URL (with the Protection Bypass for Automation header), and its request logs show no error line and no 5xx."""
    deployment_id = str((record.get("artifacts") or {}).get(service.name) or "")
    if not deployment_id:
        return [rv.Check("artifacts", "failed", [record.get("artifacts_error") or "the dev record has no deployment"])]
    try:
        vercel_deployment_artifacts(service, api, record["commit"], deployment_id)
        deployment = api.deployment(deployment_id)
        project = api.project(service.vercel_project)
    except (rr.RecordError, GuardError) as error:
        return [rv.Check("deployment", "failed", [str(error)[:300]])]
    url = rvc.deployment_url(deployment)
    checks = [
        rv.Check(
            "deployment",
            "passed",
            [
                f"{deployment_id} {url} {rvc.deployment_state(deployment)} {deployment.get('readySubstate') or ''}".strip()
            ],
        )
    ]
    secret = rvc.bypass_secret(project)
    if not secret:
        checks.append(
            rv.Check(
                "protection_bypass",
                "failed",
                [
                    f"{service.vercel_project} has no Protection Bypass for Automation secret, so dev verify cannot "
                    "call the deployment URL; create one in Settings > Deployment Protection"
                ],
            )
        )
        return checks

    def bypass_get(request: urllib.request.Request) -> tuple[int, bytes]:
        """GET with the bypass header (the secret is never printed)."""
        request.add_header(rvc.BYPASS_HEADER, secret)
        return (get or rv.http_get)(request)

    started = datetime.now(rr.UTC)
    for check in service.smoke:
        parts = urllib.parse.urlsplit(str(check["url"]))
        target = url + parts.path + (f"?{parts.query}" if parts.query else "")
        name = f"{check.get('name') or parts.path} (deployment URL)"
        checks.append(rv.check_http_public({**check, "name": name, "url": target}, bypass_get))
    # Vercel shows a request's log lines some seconds after the request.
    (sleep or time.sleep)(service.log_settle_seconds)
    try:
        span = (started - timedelta(minutes=1), datetime.now(rr.UTC))
        rows, _ = api.request_logs(str(project["id"]), *span, deployment=deployment_id, levels=rvc.ERROR_LEVELS)
        errors, _ = api.request_logs(str(project["id"]), *span, deployment=deployment_id, status_code="5xx")
    except (rr.RecordError, GuardError) as error:
        checks.append(rv.Check("deployment_logs", "failed", [f"the request logs cannot be read: {str(error)[:300]}"]))
        return checks
    lines = rvc.log_lines(rows, rvc.ERROR_LEVELS)
    details = [f"{len(lines)} error log lines and {len(errors)} 5xx responses on {deployment_id} during dev verify"]
    details += [f"{severity}: {text.splitlines()[0][:200]}" for _, _, _, severity, text in lines[:5]]
    details += [f"{row.get('statusCode')} {row.get('requestPath')}" for row in errors[:5]]
    checks.append(rv.Check("deployment_logs", "failed" if lines or errors else "passed", details))
    return checks


def check_vercel_domains(service: LightService, record: dict, after: dict[str, dict]) -> rv.Check:
    """Every production domain points at the deployment this release let go live."""
    wanted = (record.get("artifacts") or {}).get(service.name)
    now = (after.get(vercel_key(service)) or {}).get("aliases") or {}
    problems = [
        f"{domain} serves {value or 'nothing'}, not {wanted}"
        for domain, value in sorted(now.items())
        if value != wanted
    ]
    if not wanted:
        problems = ["the release record names no deployment"]
    return rv.Check("domains", "failed" if problems else "passed", [f"{len(now)} domains checked", *problems])


def vercel_promote(
    service: LightService,
    api: rvc.VercelApi,
    artifacts: dict[str, str],
    store: rr.Store,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:
    """Let the verified deployment go live: the commit status success, then Vercel assigns the domains.

    When Vercel holds the project after a rollback (auto-assignment off), or the domains did not move
    within assign_wait_seconds, the kit promotes the deployment by API (no rebuild). A refused commit
    status stops here: the kit never promotes around the Deployment Check. The hold guard runs
    again first: a rollback in the dashboard after preflight must not be undone.
    """
    key = vercel_key(service)
    wanted = str(artifacts.get(service.name) or "")
    try:
        if not wanted:
            raise rr.RecordError("no deployment to promote")
        deployment = api.deployment(wanted)
        project = api.project(service.vercel_project)
        vercel_hold_guard(service, store, api, project)
        post_commit_status(
            service, rvc.deployment_commit(deployment), "success", f"release verified; {wanted} goes live"
        )
        promoted = False
        if rvc.in_rollback_state(project):
            print("Vercel holds the project after a rollback: promote by API (this ends Vercel's hold)", flush=True)
            api.promote(str(project["id"]), wanted)
            promoted = True
        now = rvc.wait_for_aliases(
            api, service.vercel_domains, wanted, service.assign_wait_seconds, sleep=sleep, clock=clock
        )
        if any(value != wanted for value in now.values()) and not promoted:
            print(f"The domains did not move within {service.assign_wait_seconds} s: promote by API", flush=True)
            api.promote(str(project["id"]), wanted)
            now = rvc.wait_for_aliases(
                api, service.vercel_domains, wanted, service.assign_wait_seconds, sleep=sleep, clock=clock
            )
    except (rr.RecordError, GuardError) as error:
        return [f"{key}: {str(error)[:300]}"]
    failures = [
        f"{key}: {domain} serves {value or 'nothing'}, not {wanted}" for domain, value in now.items() if value != wanted
    ]
    if not failures:
        print(f"{key}: {', '.join(service.vercel_domains)} serve {wanted}", flush=True)
    return failures


def evaluate_vercel_window(service: LightService, store: rr.Store, record: dict, now: datetime) -> dict:
    """The window checks of kind vercel: new error log lines, the 5xx share, the smoke checks and the domains.

    The reads filter by deployment id, so a request served by another deployment (Skew Protection keeps old
    ones reachable) never counts against the release: control by version.
    """
    api = vercel_api(service)
    scope = rs.release_scope(store, record, now)
    tree = rs.SourceTree(REPO_ROOT, service.code)
    token = rr_token(service)
    parts: dict[str, dict] = {}
    problems: list[rs.Problem] = []
    regressions: list[dict] = []
    warnings: list[dict] = []
    newest = str((record.get("artifacts") or {}).get(service.name) or "")
    deployments = [
        str(value) for release in scope.releases if (value := (release.get("artifacts") or {}).get(service.name))
    ]
    oldest = scope.releases[-1]
    previous = (((oldest.get("previous") or {}).get("workloads") or {}).get(vercel_key(service)) or {}).get(
        "deployment"
    )
    try:
        project_id = str(api.project(service.vercel_project)["id"])
    except (rr.RecordError, GuardError, KeyError) as error:
        detail = f"the Vercel project cannot be read: {str(error)[:300]}"
        return {
            "scope": scope.summary(),
            "parts": {"logs": {"status": "inconclusive", "detail": detail}},
            "problems": [],
            "complete": False,
        }
    try:
        found, notes, judged = rvc.new_error_problems(
            api, project_id, deployments, scope, now, min_count=service.log_min_count
        )
        parts["logs"] = {
            "status": "checked" if judged else "inconclusive",
            "found": len(found),
            "detail": "; ".join(notes),
        }
        problems += found
    except (rr.RecordError, GuardError, OSError, ValueError, KeyError) as error:
        parts["logs"] = {"status": "inconclusive", "detail": str(error)[:300]}
    rs.trace_suspects(problems, scope, token=token, repo=service.github_repo, tree=tree, code=service.code)
    try:
        length = min(now - scope.window[0], rs.BASELINE)
        new = rvc.request_counts(api, project_id, newest, scope.window[0], now)
        old = None
        if previous and previous not in deployments:
            old = rvc.request_counts(api, project_id, previous, scope.release_start - length, scope.release_start)
        status, line = rvc.judge_share(new, old, service.share_limit)
        # A read that stopped at its page limit leaves the check unfinished (the verdict stays unchecked),
        # except a capped new total whose upper bound already passes.
        capped = not new.errors_complete or (old is not None and not (old.complete and old.errors_complete))
        parts["5xx"] = {"status": "inconclusive" if capped and status != "ok" else status, "detail": line}
        if status == "exceeded":
            runtime, why = rv.runtime_change(service, record, token, (oldest.get("previous") or {}).get("commit"))
            item = {"kind": "metric", "name": "vercel_5xx_share", "text": f"5xx share above the limit: {line}"}
            if runtime:
                regressions.append(item)
            else:
                item["text"] += f"; {why}, so it does not roll back"
                warnings.append(item)
    except (rr.RecordError, GuardError, OSError, ValueError, KeyError) as error:
        parts["5xx"] = {"status": "inconclusive", "detail": str(error)[:300]}
    try:
        serving = rvc.aliases(api, service.vercel_domains)
        moved = {domain: value for domain, value in serving.items() if value != newest}
        domains_detail = "; ".join(
            f"{domain} serves {value or 'nothing'}, not {newest}" for domain, value in moved.items()
        )
    except (rr.RecordError, GuardError) as error:
        moved, domains_detail = {"?": ""}, f"the domains cannot be read: {str(error)[:300]}"
    parts["domains"] = {"status": "inconclusive" if moved else "checked", "detail": domains_detail}
    if moved:
        # The domains serve another deployment (a rollback in the dashboard, or they cannot be read): the
        # public smoke checks do not test this release, and an automatic rollback could replace what a
        # human chose. Nothing fails; the findings are warnings and the checkpoint is inconclusive.
        parts["smoke"] = {"status": "inconclusive", "detail": "not judged: the domains do not serve this release"}
    else:
        failed = [c for c in (rv.check_http_public(check) for check in service.smoke) if c.status == "failed"]
        parts["smoke"] = {"status": "checked", "found": len(failed), "detail": "; ".join(c.details[0] for c in failed)}
        regressions += [{"kind": "check", "name": c.name, "text": f"{c.name}: {'; '.join(c.details)}"} for c in failed]
    complete = all(part.get("status") != "inconclusive" for part in parts.values())
    return {
        "scope": scope.summary(),
        "parts": parts,
        "problems": [problem.summary() for problem in problems],
        "regressions": regressions,
        "warnings": warnings,
        # cmd_watch: every part, not only the logs, decides "checked"; judge False turns failures into warnings.
        "complete": complete,
        "judge": not moved,
    }


def cmd_vercel_wait(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Dev build: wait for Vercel's production deployment of the commit and write it to the out dir.

    0: READY (vercel_deployment.json holds {service: deployment id}); 5: Vercel skipped the build (the
    app is not affected: the status is success, nothing to release); 1: failed, cancelled for another
    reason or never built (the status is failure).
    """
    _vercel(service)
    api = vercel_api(service)
    project_id = str(api.project(service.vercel_project)["id"])
    deployment = rvc.wait_for_deployment(api, project_id, args.commit, service.deployment_wait_minutes * 60)
    if deployment is None:
        post_commit_status(service, args.commit, "failure", "Vercel built no production deployment of this commit")
        print(f"No Vercel production deployment of {args.commit[:9]} in {service.deployment_wait_minutes} min")
        return rv.EXIT_ERROR
    found, state = rvc.deployment_id(deployment), rvc.deployment_state(deployment)
    if state == "CANCELED" and rvc.skipped_by_ignored_build_step(api.deployment(found)):
        post_commit_status(service, args.commit, "success", f"not affected: Vercel skipped the build ({found})")
        print(f"Vercel cancelled {found} (Ignored Build Step: the app is not affected); nothing to release")
        return rvc.EXIT_NOT_AFFECTED
    if state != rvc.READY:
        post_commit_status(service, args.commit, "failure", f"Vercel deployment {found} is {state}")
        print(f"Vercel deployment {found} of {args.commit[:9]} is {state}", flush=True)
        return rv.EXIT_ERROR
    out = Path(args.out_dir) / "vercel_deployment.json"
    out.write_text(json.dumps({service.name: found}, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Vercel deployment {found} {rvc.deployment_url(deployment)} is READY", flush=True)
    return rv.EXIT_OK


def cmd_vercel_status(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Set the Deployment Check's commit status: given, from the dev index of the commit, or from a gate skip."""
    _vercel(service)
    if args.from_dev_index:
        index, _ = open_store(service, args.dry_run_dir, env="dev").read(rg.dev_commit_index(args.commit))
        state, description = rvc.gate_status(index or {})
    elif args.from_skip_file:
        path = Path(args.from_skip_file)
        state, description = rvc.skip_status(path.read_text(encoding="utf-8") if path.exists() else "")
    else:
        state, description = args.state, args.description
    if (
        state == "failure"
        and description.startswith("stale:")
        and (live := serving_release(service, store, vercel_api(service), args.commit))
    ):
        # A retry of the commit prod already serves: the gate says stale, but the commit passed and is live.
        state, description = "success", f"already live in prod release {live}"
    if not state:
        print(f"Commit status {service.check_context} unchanged: {description}", flush=True)
        return rv.EXIT_OK
    post_commit_status(service, args.commit, state, description, args.target_url)
    return rv.EXIT_OK


def serving_release(service: LightService, store: rr.Store, api: rvc.VercelApi, commit: str) -> str:
    """The prod release id when this exact commit is live, else "".

    Live means: the newest prod record is a deployed promote of the commit, and every configured domain
    serves its deployment now. A rollback in the dashboard leaves the record as it was, so the domains
    decide.
    """
    pointer, _ = store.read("latest.json")
    if not pointer or pointer.get("kind") != "promote" or pointer.get("commit") != commit:
        return ""
    record = rr.read_record(store, pointer["release_id"])
    wanted = str((record.get("artifacts") or {}).get(service.name) or "")
    if record.get("status") != "deployed" or not wanted:
        return ""
    if any(value != wanted for value in rvc.aliases(api, service.vercel_domains).values()):
        return ""
    return str(record["release_id"])


def _vercel(service: LightService) -> None:
    """Raise ConfigError unless the service is kind vercel."""
    if service.kind != "vercel":
        raise rc.ConfigError(f"{service.name} is kind {service.kind}; this command is for kind vercel")


# --------------------------------------------------------------------------- kind railway


def railway_api(service: LightService, env: str) -> rwy.RailwayApi:
    """The Railway API with the project token of one environment (RAILWAY_TOKEN_<ENV> or its Secret Manager secret)."""
    google = GoogleApi(service.project, service.region)
    secret = dict(service.railway_tokens)[env]
    return rwy.RailwayApi(rwy.token_source(env, service.project, secret, google.access_token))


def railway_key(service: LightService) -> str:
    """The workload key of a Railway service in snapshots and rollback plans."""
    return f"railway/{service.name}"


def railway_env_id(service: LightService, env: str) -> str:
    """The Railway environment id of the kit environment (dev or prod)."""
    return dict(service.railway_environment_ids)[env]


def railway_label(service: LightService, env: str) -> str:
    """The Railway environment's name, for messages."""
    return dict(service.railway_environments)[env]


def snapshot_railway(service: LightService, api: rwy.RailwayApi, env: str = "prod") -> dict[str, dict]:
    """The deployment that serves the environment now: its id, status, commit and image digest."""
    live = rwy.serving(api, service.railway_service_id, railway_env_id(service, env)) or {}
    return {
        railway_key(service): {
            "kind": "railway",
            "environment": railway_label(service, env),
            "deployment": str(live.get("id") or ""),
            "status": str(live.get("status") or "none"),
            "commit": rwy.commit_of(live),
            "image_digest": rwy.image_of(live),
            "created_at": str(live.get("createdAt") or ""),
        }
    }


def railway_deploy(
    service: LightService,
    api: rwy.RailwayApi,
    env: str,
    commit: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[dict | None, list[str]]:
    """Deploy the commit to the environment and wait; (the deployment, problems). No problem: it is SUCCESS."""
    env_id = railway_env_id(service, env)
    label = railway_label(service, env)
    try:
        rwy.check_token_scope(api, service.railway_project_id, env_id, f"{env} ({label})")
        deployment_id = api.deploy(service.railway_service_id, env_id, commit)
    except (rr.RecordError, GuardError) as error:
        return None, [str(error)[:300]]
    print(f"Railway {label}: deployment {deployment_id} of {commit[:9]} started", flush=True)
    deployment = railway_wait_or_cancel(service, api, deployment_id, sleep=sleep, clock=clock)
    problems = []
    status = str(deployment.get("status") or "")
    if status != rwy.SUCCESS:
        problems.append(f"Railway {label} deployment {deployment.get('id')} is {status}")
    if rwy.commit_of(deployment) != commit:
        problems.append(
            f"Railway {label} deployment {deployment.get('id')} runs {rwy.commit_of(deployment)[:9] or 'no commit'}"
        )
    print(f"Railway {label}: {rwy.describe(deployment)}", flush=True)
    return deployment, problems


def railway_wait_or_cancel(
    service: LightService,
    api: rwy.RailwayApi,
    deployment_id: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Wait for the kit's own deployment. One that still builds when the wait ends, or whose status can no
    longer be read, is cancelled: it must not go live later, unchecked and after a rollback. Returns the
    deployment (always with its id; status says what happened)."""
    try:
        deployment = rwy.wait_for_deployment(
            api, deployment_id, service.deployment_wait_minutes * 60, sleep=sleep, clock=clock
        )
        status = str(deployment.get("status") or "")
        if status not in rwy.BUILDING:
            return deployment
        why = f"{status} after {service.deployment_wait_minutes} min"
    except (rr.RecordError, GuardError) as error:
        deployment = {"id": deployment_id, "status": "UNKNOWN"}
        why = f"status unreadable ({str(error)[:160]})"
    try:
        api.cancel(deployment_id)
        status = f"{why}, cancelled"
    except (rr.RecordError, GuardError) as error:
        status = f"{why}, and the cancel failed: {str(error)[:160]}"
    print(f"Railway deployment {deployment_id}: {status}", flush=True)
    return {**deployment, "id": deployment.get("id") or deployment_id, "status": status}


def cmd_railway_deploy(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Dev build: deploy the commit to the dev environment and write {service: deployment id} to the out dir."""
    _railway(service)
    if args.env != "dev":
        raise rc.ConfigError("railway-deploy is the dev stage's deploy; the prod build deploys with apply")
    deployment, problems = railway_deploy(service, railway_api(service, "dev"), "dev", args.commit)
    if deployment:
        out = Path(args.out_dir) / "railway_deployment.json"
        out.write_text(json.dumps({service.name: deployment["id"]}, sort_keys=True) + "\n", encoding="utf-8")
    for problem in problems:
        print(f"FAILED: {problem}", file=sys.stderr, flush=True)
    return rv.EXIT_ERROR if problems else rv.EXIT_OK


def railway_dev_artifacts(service: LightService, api: rwy.RailwayApi, record: dict) -> dict[str, str]:
    """{service: commit} when the dev deployment this build made is SUCCESS, of the commit, in the dev environment.

    The artifact is the commit: the prod build deploys the same source, and Railway builds it again.
    """
    wanted = str((record.get("images") or {}).get(service.name) or "")
    if not wanted:
        raise rr.RecordError("the dev record names no Railway deployment")
    deployment = api.deployment(wanted)
    if deployment.get("environmentId") and deployment["environmentId"] != railway_env_id(service, "dev"):
        raise rr.RecordError(f"deployment {wanted} is not in {railway_label(service, 'dev')}")
    if str(deployment.get("status")) not in rwy.SERVING:
        raise rr.RecordError(f"deployment {wanted} is {deployment.get('status')}")
    if rwy.commit_of(deployment) != record["commit"]:
        raise rr.RecordError(f"deployment {wanted} runs {rwy.commit_of(deployment)[:9] or 'no commit'}")
    return {service.name: record["commit"]}


def check_railway_version(
    service: LightService,
    env: str,
    commit: str,
    get: rv.HttpGet | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> rv.Check | None:
    """The environment's URL names the commit it runs (railway.version: a JSON field of a path); None when unset."""
    if not service.version_path:
        return None
    url = dict(service.railway_urls)[env] + service.version_path
    line = ""
    for attempt in range(3):
        try:
            status, body = (get or rv.http_get)(urllib.request.Request(url, headers={"User-Agent": rv.USER_AGENT}))
            value = json.loads(body.decode("utf-8", "replace")).get(service.version_field) if status == 200 else None
        except (OSError, ValueError, AttributeError) as error:
            status, value = 0, None
            line = f"error: {error}"
        if isinstance(value, str) and len(value) >= 7 and commit.startswith(value):
            return rv.Check("version", "passed", [f"GET {url}: {service.version_field} {value}"])
        line = line or f"HTTP {status}, {service.version_field} {value!r}"
        if attempt < 2:
            sleep(10)
    return rv.Check("version", "failed", [f"GET {url}: {line}, not {commit[:9]}"])


def railway_dev_checks(
    service: LightService,
    record: dict,
    api: rwy.RailwayApi,
    *,
    get: rv.HttpGet | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[rv.Check]:
    """Dev verify of kind railway: the deployment of the commit serves the dev environment, Railway's auto deploy is
    off there, the smoke checks pass on the dev URL, and its logs show no error line and no 5xx while they run."""
    label = railway_label(service, "dev")
    env_id = railway_env_id(service, "dev")
    deployment_id = str((record.get("images") or {}).get(service.name) or "")
    try:
        deployment = api.deployment(deployment_id)
        live = rwy.serving(api, service.railway_service_id, env_id)
    except (rr.RecordError, GuardError) as error:
        return [rv.Check("deployment", "failed", [str(error)[:300]])]
    problems = []
    if str(deployment.get("status")) != rwy.SUCCESS:
        problems.append(f"{deployment_id} is {deployment.get('status')}")
    if rwy.commit_of(deployment) != record["commit"]:
        problems.append(f"{deployment_id} runs {rwy.commit_of(deployment)[:9] or 'no commit'}")
    if not live or live.get("id") != deployment_id:
        problems.append(f"{label} serves {(live or {}).get('id') or 'nothing'}, not {deployment_id} (a newer deploy?)")
    checks = [rv.Check("deployment", "failed" if problems else "passed", [rwy.describe(deployment), *problems])]
    try:
        enabled = api.auto_deploy(service.railway_project_id, service.railway_service_id, env_id).get("enabled")
        if enabled is True:
            line = (
                f"Railway auto deploy is ON in {label}: Railway deploys {service.release_branch} by itself, around "
                "the release kit. Turn it off (Service > Settings > Source)."
            )
        elif enabled is False:
            line = f"Railway auto deploy is off in {label}"
        else:
            line = f"cannot read Railway's auto deploy setting in {label} (it answered {enabled!r}), so it may be on"
        checks.append(rv.Check("auto_deploy", "passed" if enabled is False else "failed", [line]))
    except (rr.RecordError, GuardError) as error:
        # Unread is not off: the commit stays unverified until Railway confirms that it cannot deploy by itself.
        checks.append(
            rv.Check(
                "auto_deploy",
                "failed",
                [f"cannot read Railway's auto deploy setting in {label}, so it may be on: {str(error)[:200]}"],
            )
        )
    started = datetime.now(rr.UTC)
    checks += [rv.check_http_public(check, get or rv.http_get) for check in dev_smoke(service)]
    version = check_railway_version(service, "dev", record["commit"], get, sleep or time.sleep)
    if version:
        checks.append(version)
    # Railway shows a request's log lines some seconds after the request.
    (sleep or time.sleep)(service.log_settle_seconds)
    span = (started - timedelta(minutes=1), datetime.now(rr.UTC))
    try:
        rows, _ = api.logs(deployment_id, *span, filter_text="@level:error")
        counts = rwy.request_counts(api, deployment_id, *span)
    except (rr.RecordError, GuardError) as error:
        checks.append(rv.Check("deployment_logs", "failed", [f"the logs cannot be read: {str(error)[:300]}"]))
        return checks
    lines = rwy.log_lines(rows, deployment_id)
    details = [f"{len(lines)} error log lines and {counts.errors} 5xx of {counts.total} requests during dev verify"]
    if not counts.complete:
        details.append("the HTTP log read stopped at its page limit; a 5xx after it would be unseen")
    details += [f"{severity}: {text.splitlines()[0][:200]}" for _, _, _, severity, text in lines[:5]]
    details += [f"5xx {path} x{count}" for path, count in sorted(counts.paths.items())[:5]]
    checks.append(
        rv.Check("deployment_logs", "failed" if lines or counts.errors or not counts.complete else "passed", details)
    )
    return checks


def check_railway_deployment(service: LightService, record: dict, after: dict[str, dict]) -> rv.Check:
    """Production serves a new deployment of the release's commit, and it is SUCCESS."""
    key = railway_key(service)
    now = after.get(key) or {}
    before = ((record.get("previous") or {}).get("workloads") or {}).get(key) or {}
    problems = []
    if now.get("commit") != record["commit"]:
        problems.append(
            f"{now.get('environment')} serves {now.get('deployment') or 'nothing'} of "
            f"{str(now.get('commit') or 'no commit')[:9]}, not {record['commit'][:9]}"
        )
    elif before.get("deployment") and now.get("deployment") == before.get("deployment"):
        problems.append(f"{now.get('environment')} still serves {now.get('deployment')}, the deployment from before")
    if now.get("status") not in rwy.SERVING:
        problems.append(f"deployment {now.get('deployment')} is {now.get('status')}")
    detail = f"{now.get('deployment')} {now.get('status')} commit {str(now.get('commit'))[:9]} image {str(now.get('image_digest'))[:19]}"
    return rv.Check("deployment", "failed" if problems else "passed", [detail, *problems])


def evaluate_railway_window(service: LightService, store: rr.Store, record: dict, now: datetime) -> dict:
    """The window checks of kind railway: the deployment status, new error log lines, the 5xx share, the smoke checks.

    The reads filter by deployment id: only the release's own deployment counts against it (control by version).
    """
    api = railway_api(service, "prod")
    key = railway_key(service)
    env_id = railway_env_id(service, "prod")
    scope = rs.release_scope(store, record, now)
    tree = rs.SourceTree(REPO_ROOT, service.code)
    token = rr_token(service)
    parts: dict[str, dict] = {}
    problems: list[rs.Problem] = []
    regressions: list[dict] = []
    warnings: list[dict] = []
    newest = str(((record.get("workloads") or {}).get(key) or {}).get("deployment") or "")
    deployments = [
        str(value)
        for release in scope.releases
        if (value := ((release.get("workloads") or {}).get(key) or {}).get("deployment"))
    ]
    oldest = scope.releases[-1]
    previous = str((((oldest.get("previous") or {}).get("workloads") or {}).get(key) or {}).get("deployment") or "")
    moved = True
    try:
        mine = api.deployment(newest) if newest else {}
        live = rwy.serving(api, service.railway_service_id, env_id) or {}
        moved = live.get("id") != newest
        status = str(mine.get("status") or "unknown")
        detail = f"{newest or 'no deployment recorded'} is {status}; production serves {live.get('id') or 'nothing'}"
        crashed = status in ("CRASHED", "FAILED")
        parts["deployment"] = {"status": "inconclusive" if moved and not crashed else "checked", "detail": detail}
        if crashed:
            moved = False  # the release's own deployment failed: judge it
            regressions.append({"kind": "check", "name": "deployment", "text": f"deployment: {detail}"})
    except (rr.RecordError, GuardError) as error:
        parts["deployment"] = {"status": "inconclusive", "detail": f"Railway cannot be read: {str(error)[:300]}"}
    try:
        since = min((start for start, _ in scope.baseline), default=scope.window[0])
        listed, listed_all = api.deployments(service.railway_service_id, env_id, since=since)
        if not listed_all:
            raise rr.RecordError("the deployments of the 24 h baseline do not fit the list pages; new lines not judged")
        baseline = [
            (rwy.baseline_deployments(listed, start, end, deployments), start, end) for start, end in scope.baseline
        ]
        found, notes, judged = rwy.new_error_problems(
            api, deployments, baseline, scope, now, min_count=service.log_min_count
        )
        parts["logs"] = {
            "status": "checked" if judged else "inconclusive",
            "found": len(found),
            "detail": "; ".join(notes),
        }
        problems += found
    except (rr.RecordError, GuardError, OSError, ValueError, KeyError) as error:
        parts["logs"] = {"status": "inconclusive", "detail": str(error)[:300]}
    rs.trace_suspects(problems, scope, token=token, repo=service.github_repo, tree=tree, code=service.code)
    try:
        length = min(now - scope.window[0], rs.BASELINE)
        new = rwy.request_counts(api, newest, scope.window[0], now)
        old = None
        if previous and previous not in deployments:
            old = rwy.request_counts(api, previous, scope.release_start - length, scope.release_start)
        status, line = rvc.judge_share(new, old, service.share_limit)
        # As for kind vercel: a read that stopped at its page limit leaves the check unfinished.
        capped = not new.errors_complete or (old is not None and not (old.complete and old.errors_complete))
        parts["5xx"] = {"status": "inconclusive" if capped and status != "ok" else status, "detail": line}
        if status == "exceeded":
            runtime, why = rv.runtime_change(service, record, token, (oldest.get("previous") or {}).get("commit"))
            item = {"kind": "metric", "name": "railway_5xx_share", "text": f"5xx share above the limit: {line}"}
            if runtime:
                regressions.append(item)
            else:
                item["text"] += f"; {why}, so it does not roll back"
                warnings.append(item)
    except (rr.RecordError, GuardError, OSError, ValueError, KeyError) as error:
        parts["5xx"] = {"status": "inconclusive", "detail": str(error)[:300]}
    if moved:
        # Production serves another deployment (a manual deploy or a dashboard rollback): the smoke checks
        # do not test this release, and a rollback could replace what a human chose. Nothing fails.
        parts["smoke"] = {"status": "inconclusive", "detail": "not judged: production does not serve this release"}
    else:
        checks = [rv.check_http_public(check) for check in service.smoke]
        version = check_railway_version(service, "prod", record["commit"])
        checks += [version] if version else []
        failed = [check for check in checks if check.status == "failed"]
        parts["smoke"] = {"status": "checked", "found": len(failed), "detail": "; ".join(c.details[0] for c in failed)}
        regressions += [{"kind": "check", "name": c.name, "text": f"{c.name}: {'; '.join(c.details)}"} for c in failed]
    complete = all(part.get("status") != "inconclusive" for part in parts.values())
    return {
        "scope": scope.summary(),
        "parts": parts,
        "problems": [problem.summary() for problem in problems],
        "regressions": regressions,
        "warnings": warnings,
        "complete": complete,
        "judge": not moved,
    }


def railway_migrations(service: LightService, base: str | None, head: str | None) -> dict | None:
    """The migration state of base..head ({"migrations": [...]} or {"unknown": why}); None without a migrations rule."""
    if not service.migrations_prefix:
        return None
    return rwy.migrations_between(
        service.github_repo, base, head, service.migrations_prefix, service.migrations_suffix, rr_token(service)
    )


def forward_state(service: LightService, record: dict) -> dict | None:
    """The migration state of the range a failed release would roll back, when it forbids the rollback; else None.

    The range runs from the suggested rollback target (an older release when a window judged several
    together) to the release; the release's own range alone could miss an older release's migration.
    """
    if not service.migrations_prefix:
        return None
    target = ((record.get("rollback_suggestion") or {}).get("target") or {}).get("commit")
    base = target or (record.get("previous") or {}).get("commit")
    state = record.get("migrations")
    if state is None or "unknown" in state or (target and target != (record.get("previous") or {}).get("commit")):
        state = railway_migrations(service, base, record["commit"])
    return state if rwy.forward_only(state) else None


def forward_resume(service: LightService, release_id: str) -> str:
    """How a forward-only hold ends."""
    return (
        f"merge a forward fix whose commit message has the trailer `Fixes-release: {release_id}` (a revert does not "
        "end this hold: the database keeps the migration), or run "
        f"`{service.kit_command('release_gate.py', 'resume')} --reason '<why>'`"
    )


def forward_hold(service: LightService, store: rr.Store, record: dict, *, force: bool = False) -> bool:
    """A failed release that changed the database schema: no rollback; hold prod deploys and ping the author.

    The hold lives on the failed release's own record (promote_hold with forward_only); release_gate.py
    holds every later deploy until a commit with `Fixes-release: <release id>` or `resume`. Returns whether
    the "on hold" line is in Slack.
    """
    if not force and os.environ.get("RELEASE_AUTO_ROLLBACK", "").strip() != "1":
        print("Forward-only release failed; this run holds nothing (RELEASE_AUTO_ROLLBACK is not 1)", flush=True)
        return False
    release_id = record["release_id"]
    state = record.get("migrations") or {}
    why = ", ".join(state.get("migrations") or []) or str(state.get("unknown") or "a migration")

    def change(current: dict) -> None:
        """Write the forward-only hold, unless it is there already."""
        if current.get("promote_hold"):
            return  # saved already, or a human resumed or a fix was seen: never overwrite it
        current["promote_hold"] = {
            "status": "held",
            "forward_only": True,
            "since": rr.utc_now(),
            "rolled_back_release": release_id,
            "rolled_back_commit": record["commit"],
            "prs": [],
            "migrations": state,
            "resume": forward_resume(service, release_id),
        }
        current["auto_rollback"] = {"status": "skipped", "reason": f"forward-only: {why[:300]}", "at": rr.utc_now()}

    saved = rr.update_record(store, release_id, change, "forward_hold", {"why": why[:200]})
    location = store.location(rr.record_name(release_id))
    mentions, detail = forward_alert(service, saved, location, why)
    return rv.alert_once(service, store, saved, "on hold", mentions, detail)


def forward_alert(service: LightService, record: dict, location: str, why: str) -> tuple[list[str], str]:
    """A failed forward-only release: (mentions, detail). The "on hold" line carries the mentions; the detail
    (what regressed, the hold, a fix-forward prompt) is for the build log."""
    mentions = rv.failure_mentions(record, rv.load_people(), rv.likely_prs(record))
    short = record["commit"][:9]
    detail = "\n".join(
        [
            f"{service.title} prod release {record['release_id']} (commit {short}) failed release-verify. It changes "
            f"the database schema ({why[:200]}), so it did not roll back: an older build does not start against the "
            "newer schema. Production still runs it, and automatic prod deploys hold until a commit with "
            f"`Fixes-release: {record['release_id']}` merges.",
            "",
            "What regressed:",
            *[
                f"- {rv._clip(reason, 300)}"
                for reason in ((record.get("verdict") or {}).get("reasons") or ["see the record"])[:5]
            ],
            "",
            f"In {service.github_repo}:",
            f"1. Read {service.docs} and the release record: gcloud storage cat {location}",
            "2. Confirm the cause with the Railway logs named above.",
            "3. Fix it forward on the release branch with a test (a new migration if the schema must change again). "
            f"Put the trailer 'Fixes-release: {record['release_id']}' in the commit message. A revert of the PR does "
            "not end the hold.",
            "4. Open the PR as me and follow the PR review loop in AGENTS.md. After it merges it deploys by itself.",
        ]
    )
    return mentions, detail


def railway_rollback_steps(
    service: LightService, api: rwy.RailwayApi, latest: dict, target: dict
) -> tuple[list[rb.Step], list[str], list[str]]:
    """(steps, problems, notes) of a Railway rollback: redeploy the deployment that served before the target release."""
    key = railway_key(service)
    wanted = str(((target.get("workloads") or {}).get(key) or {}).get("deployment") or "")
    live = rwy.serving(api, service.railway_service_id, railway_env_id(service, "prod")) or {}
    have = str(live.get("id") or "") or None
    problems: list[str] = []
    notes = [
        "Railway redeploys the target deployment with its image (no rebuild) and the current variables; "
        "a target whose image is gone is rebuilt from its commit"
    ]
    recorded = str(((latest.get("workloads") or {}).get(key) or {}).get("deployment") or "")
    for item in api.building(service.railway_service_id, railway_env_id(service, "prod")):
        # A deployment still in flight would replace the rollback when it finishes.
        problems.append(
            f"{key}: deployment {item.get('id')} of {rwy.commit_of(item)[:9] or 'no commit'} is still "
            f"{item.get('status')} in production; cancel it in Railway or wait, then roll back"
        )
    action = "skip"
    if not wanted:
        problems.append(f"{key}: the target release recorded no deployment")
    else:
        try:
            target_deployment = api.deployment(wanted)
            same = rwy.image_of(target_deployment) and rwy.image_of(target_deployment) == rwy.image_of(live)
            action = "none" if same and rwy.commit_of(target_deployment) == rwy.commit_of(live) else "set"
            if not target_deployment.get("canRedeploy") and not rwy.commit_of(target_deployment):
                problems.append(f"{key}: {wanted} can no longer be redeployed and has no commit to rebuild")
        except (rr.RecordError, GuardError) as error:
            problems.append(f"{key}: deployment {wanted} cannot be read: {str(error)[:200]}")
    if action == "set" and recorded and have and have != recorded:
        # Production serves a deployment the kit did not record (a `railway up`, a dashboard redeploy or
        # rollback): a rollback would replace what a human chose.
        problems.append(
            f"{key}: production serves {have}, not {recorded} that the release kit recorded (a deploy outside the "
            "kit?); a human decides"
        )
    state = railway_migrations(service, target.get("commit"), latest.get("commit"))
    if action == "set" and rwy.forward_only(state):
        found = ", ".join((state or {}).get("migrations") or []) or str((state or {}).get("unknown"))
        problems.append(
            f"{key}: the commits since {str(target.get('commit'))[:9]} change the database schema ({found[:200]}); "
            "an older build does not start against it. Forward-only: fix it forward"
        )
    return [rb.Step(key, service.name, "deployment", have, wanted or None, action)], problems, notes


def railway_rollback(service: LightService, api: rwy.RailwayApi, target: str, target_commit: str | None) -> list[str]:
    """Redeploy the target deployment (its image; a rebuild of its commit when the image is gone); return failures."""
    env_id = railway_env_id(service, "prod")
    rwy.check_token_scope(api, service.railway_project_id, env_id, "prod")
    old = api.deployment(target)
    if old.get("canRedeploy"):
        new_id = str(api.redeploy(target, service.railway_service_id, env_id).get("id") or "")
    else:
        commit = rwy.commit_of(old) or str(target_commit or "")
        print(f"{target} can no longer be redeployed; rebuilding its commit {commit[:9]}", flush=True)
        new_id = api.deploy(service.railway_service_id, env_id, commit)
    # It must not go live later, after the lock is free and without a record: the wait cancels it.
    deployment = railway_wait_or_cancel(service, api, new_id)
    print(f"railway/{service.name}: {target} redeployed as {rwy.describe(deployment)}", flush=True)
    failures = []
    if deployment.get("status") != rwy.SUCCESS:
        failures.append(f"railway/{service.name}: the rollback deployment {new_id} is {deployment.get('status')}")
    if rwy.image_of(old) and rwy.image_of(deployment) and rwy.image_of(old) != rwy.image_of(deployment):
        print(f"NOTE: the rollback runs image {rwy.image_of(deployment)[:19]}, not {rwy.image_of(old)[:19]} (rebuilt)")
    return failures


def _railway(service: LightService) -> None:
    """Raise ConfigError unless the service is kind railway."""
    if service.kind != "railway":
        raise rc.ConfigError(f"{service.name} is kind {service.kind}; this command is for kind railway")


# --------------------------------------------------------------------------- kind static_site


def snapshot_static(service: LightService) -> dict[str, dict]:
    """The site now: the sitemap's status and page count."""
    site = service.site
    assert site is not None
    status, text = rst.fetch(rst.http_get, site.url + site.sitemap_path)
    return {
        f"static/{service.name}": {
            "kind": "static_site",
            "url": site.url,
            "sitemap_status": status,
            "sitemap_urls": len(rst.sitemap_urls(text)) if status == 200 else None,
        }
    }


def last_published(store: rr.Store, record: dict | None, limit: int = 20) -> dict | None:
    """The newest record from this one back that went live (status deployed), else None."""
    for _ in range(limit):
        if not record or record.get("status") == "deployed":
            return record
        previous_id = (record.get("previous") or {}).get("release_id")
        record = rr.read_record(store, previous_id) if previous_id else None
    return None


def static_pages(
    service: LightService,
    store: rr.Store,
    record: dict,
    *,
    strict_content: bool,
    get: rst.Get | None = None,
    base: dict | None = None,
) -> rst.PageResult:
    """The page checks of a release: the pages changed since base (default: the release; the window passes the
    oldest release it judges), and the page count of the release before base."""
    site = service.site
    assert site is not None
    base = base or record
    previous_id = (base.get("previous") or {}).get("release_id")
    previous_count = None
    if previous_id:
        try:
            before = rr.read_record(store, previous_id)
            previous_count = ((before.get("workloads") or {}).get(f"static/{service.name}") or {}).get("sitemap_urls")
        except (rr.RecordError, GuardError) as error:
            print(f"WARNING: the previous release cannot be read: {str(error)[:200]}", file=sys.stderr, flush=True)

    def read(path: str) -> str | None:
        """A page file of the release's source tree (the build's checkout or the window build's copy)."""
        try:
            return (REPO_ROOT / path).read_text(encoding="utf-8")
        except OSError:
            return None

    token = rr_token(service)
    base_commit = (base.get("previous") or {}).get("commit")
    if not base_commit and not previous_id and token:
        # The first release of the service has no earlier release: its commit's parent is the base, so its
        # changed pages are checked, and the windows of later releases that judge it get a base too.
        base_commit = rst.parent_commit(service.github_repo, base["commit"], token)
    changes = rst.changed_pages(service.github_repo, base_commit, record["commit"], site, token, read)
    return rst.check_pages(
        site, record["commit"], changes, previous_count, strict_content=strict_content, get=get or rst.http_get
    )


def static_publish_check(service: LightService, record: dict) -> rv.Check:
    """The publisher's check run of the commit succeeded (read now, no wait)."""
    site = service.site
    assert site is not None
    token = rr_token(service)
    if not token:
        return rv.Check("publish", "failed", ["no GitHub token to read the publisher's check run"])
    try:
        run = rst.check_run(service.github_repo, record["commit"], site.publisher_check, token)
    except (GuardError, OSError, ValueError) as error:
        return rv.Check("publish", "failed", [f"the check run cannot be read: {str(error)[:200]}"])
    ok = bool(run) and run.get("status") == "completed" and run.get("conclusion") in rst.PUBLISHED  # type: ignore[union-attr]
    return rv.Check("publish", "passed" if ok else "failed", [rst.run_line(run)])


def static_deploy_checks(service: LightService, store: rr.Store, record: dict) -> list[rv.Check]:
    """+0: the publish succeeded, then the page checks; a changed page without its new title only warns."""
    checks = [static_publish_check(service, record)]
    if checks[0].status != "passed":
        return checks
    result = static_pages(service, store, record, strict_content=False)
    details = [json.dumps(result.facts, sort_keys=True), *result.failures, *[f"warning: {w}" for w in result.warnings]]
    checks.append(rv.Check("pages", "failed" if result.failures else "passed", details))
    return checks


def evaluate_static_window(service: LightService, store: rr.Store, record: dict, now: datetime) -> dict:
    """The window checks of kind static_site: every page check, a changed page without its title fails now."""
    scope = rs.release_scope(store, record, now)
    # The pages of every release this checkpoint judges: a newer merge must not drop an older one's checks.
    result = static_pages(service, store, record, strict_content=True, base=scope.releases[-1])
    regressions = [
        {"kind": "check", "name": f"pages_{index}", "text": text} for index, text in enumerate(result.failures)
    ]
    warnings = [
        {"kind": "check", "name": f"pages_warning_{index}", "text": text} for index, text in enumerate(result.warnings)
    ]
    return {
        "scope": scope.summary(),
        "parts": {
            "pages": {
                "status": "checked" if result.complete else "inconclusive",
                "found": len(result.failures),
                "detail": json.dumps(result.facts, sort_keys=True),
            }
        },
        "problems": [],
        "regressions": regressions,
        "warnings": warnings,
        "complete": result.complete,
        "judge": True,
    }


def static_alert(service: LightService, record: dict, location: str, command: str) -> tuple[str, list[str], str]:
    """A failed static-site release: (state, mentions, detail). "down" when it went live (nothing reverts by
    itself), else "warning" (the old site still serves). The detail (what failed, the revert command, a fix
    prompt) is for the build log."""
    suspects = rv.likely_prs(record) or [item for item in record.get("commits") or [] if item.get("sha")][-3:]
    mentions = rv.failure_mentions(record, rv.load_people(), suspects)
    short = record["commit"][:9]
    reasons = [
        rv._clip(reason, 300) for reason in ((record.get("verdict") or {}).get("reasons") or ["see the record"])[:5]
    ]
    published = record.get("status") != "deploy_failed"
    state = (
        "It is live: the publisher published it, and nothing reverts by itself."
        if published
        else "Nothing went live: the publisher's check run did not succeed."
    )
    detail = "\n".join(
        [
            f"{service.title} release {record['release_id']} (commit {short}) failed its release check. {state}",
            "",
            "What failed:",
            *[f"- {reason}" for reason in reasons],
            "",
            f"In {service.github_repo}:",
            f"1. Read {service.docs} and the release record: gcloud storage cat {location}",
            "2. Open the failing pages on the live site and confirm the cause.",
            f"3. Fix it on {service.release_branch} (run npm run check), or revert the release when a fix is not quick:",
            f"   {command}",
            "4. Open the PR as me and follow the PR review loop in AGENTS.md. It publishes when it merges.",
        ]
    )
    return ("down" if published else "warning"), mentions, detail


def cmd_static_wait(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Merge build: wait for the publisher's check run; 0 published, 5 nothing published, 1 failed or missing."""
    if service.kind != "static_site" or service.site is None:
        raise rc.ConfigError(f"{service.name} is kind {service.kind}; static-wait is for kind static_site")
    token = rr_token(service)
    if not token:
        raise rr.RecordError("no GitHub token to read the publisher's check run")
    outcome, run = rst.wait_for_publish(service.github_repo, args.commit, service.site, token)
    Path(args.out_dir, "static_publish.json").write_text(
        json.dumps({"outcome": outcome, "run": rst.run_line(run)}, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"{service.site.publisher_check} on {args.commit[:9]}: {outcome} ({rst.run_line(run)})", flush=True)
    if outcome == "nothing":
        return rst.EXIT_NOTHING_PUBLISHED
    return rv.EXIT_OK if outcome == "published" else rv.EXIT_ERROR


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


def check_promoted_revisions(service: LightService, record: dict, after: dict[str, dict]) -> rv.Check:
    """Tier full: every service sends 100% of its traffic to the revision this release promoted."""
    artifacts = record.get("artifacts") or {}
    problems = []
    for name in service.services:
        revision = str(artifacts.get(name) or "").split("@", 1)[0]
        serving = serving_split(after.get(f"cloud_run/{name}") or {})
        if not revision:
            problems.append(f"cloud_run/{name}: the release record names no revision")
        elif serving != {revision: 100}:
            problems.append(f"cloud_run/{name}: traffic is {split_text(serving) or 'unknown'}, not {revision}=100")
    return rv.Check(
        "revisions", "failed" if problems else "passed", [f"{len(service.services)} services checked", *problems]
    )


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
    """Run the deploy checks and the public smoke checks; return them with the snapshot after the deploy."""
    after = snapshot(service, runner, token)
    if service.kind == "railway":
        checks = [check_railway_deployment(service, record, after)]
        checks += [rv.check_http_public(check, get) for check in service.smoke]
        version = check_railway_version(service, "prod", record["commit"], get)
        return checks + ([version] if version else []), after
    if service.kind == "vercel":
        checks = [check_vercel_domains(service, record, after)]
    elif service.kind == "cloud_run" and service.has_gate:
        checks = [check_promoted_revisions(service, record, after)]
    elif service.kind == "cloud_run":
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


def open_store(service: LightService, dry_run_dir: Path | None, *, uri: str = "", env: str = "prod") -> rr.Store:
    """Open the record store of one environment of the service (dry_run_dir: writes go only there)."""
    token = GoogleApi(service.project, service.region).access_token
    return rr.open_store(uri or service.store, service.environment_named(env), token, dry_run_dir)


def rollback_command(service: LightService, record: dict, target: dict | None = None) -> str:
    """The command a human runs to undo this release (it prints; nothing runs it automatically).

    Tier full: release_rollback.py run (the rollback trigger, as for GKE); target: an older one than "previous".
    """
    if service.has_gate:
        return f"{rv.rollback_cli(service, 'run', target)} --reason '<why>'"
    previous = record.get("previous") or {}
    if service.kind == "static_site":
        # No rollback exists: the command opens a revert PR of the release's commits; a human merges it.
        return rst.revert_command(service.github_repo, service.release_branch, record["commit"], previous.get("commit"))
    if service.kind == "composer":
        commit = previous.get("commit") or "<previous commit>"
        return (
            f"gcloud builds triggers run {service.sync_trigger} --region={service.region} "
            f"--project={service.project} --sha={commit}"
        )
    snapshot_before = record.get("previous_snapshot") or {}
    commands = []
    for name in service.services:
        before = snapshot_before.get(f"cloud_run/{name}") or {}
        # The revisions that served before the release: after a manual traffic move the newest ready
        # revision may be one that serves nothing.
        traffic = {revision: percent for revision, percent in (before.get("traffic") or {}).items() if percent}
        split = ",".join(f"{revision}={percent}" for revision, percent in sorted(traffic.items()))
        split = split or f"{before.get('latest_ready') or '<previous revision>'}=100"
        commands.append(
            f"gcloud run services update-traffic {name} --region={service.region} --project={service.project} "
            f"--to-revisions={split}"
        )
    return " && ".join(commands)


def _json_file(path: str | None) -> dict:
    """The JSON object in a file; {} when no path is given."""
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise rr.RecordError(f"{path} does not hold a JSON object")
    return data


def cmd_begin(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Take a snapshot and write a deploying record before the deploy; write its id to --release-id-file.

    Tier full: on dev the record holds the images this build pushed (--images-file); on prod it holds
    the artifacts it promotes (--artifacts-file), and "previous" keeps the snapshot of what served
    before (the rollback target) with the previous release's artifacts.
    """
    started_at = rr.utc_now()
    token = GoogleApi(service.project, service.region).access_token
    dev = args.env == "dev"
    if dev:
        before = snapshot_cloud_run(service, runner) if service.kind == "cloud_run" else {}
    else:
        before = snapshot(service, runner, token)
    artifacts = _json_file(args.artifacts_file)
    if service.kind == "railway" and not dev and not artifacts:
        artifacts = {service.name: args.commit}
    if service.has_gate and not dev and not artifacts:
        raise rr.RecordError("tier full: the prod record needs --artifacts-file (what the build promotes)")
    pointer, _ = store.read("latest.json")
    previous: dict = {"release_id": None, "commit": None}
    if pointer:
        record, _ = store.read(rr.record_name(pointer["release_id"]))
        if service.kind == "static_site":
            # The comparison base is the newest release that went live: a failed publish changed nothing.
            record = last_published(store, record)
        if record:
            previous = {
                "release_id": record["release_id"],
                "commit": record["commit"],
                "deploy_finished": (record.get("times") or {}).get("deploy_finished"),
            }
            if service.has_gate and record.get("artifacts"):
                previous["artifacts"] = record["artifacts"]
    serving_commit = ((before.get(f"railway/{service.name}") or {}).get("commit")) if service.kind == "railway" else ""
    if serving_commit and serving_commit != previous["commit"]:
        # Railway keeps the old deployment when a deploy fails, so the newest record may name a commit that
        # never served. The commits since, the migration range and the rollback check start at what serves.
        previous["recorded_commit"] = previous["commit"]
        previous["commit"] = serving_commit
    if service.kind == "vercel" and not previous["commit"]:
        # The first release has no record before it: the commits since start at what the domains serve.
        previous["commit"] = (before.get(vercel_key(service)) or {}).get("commit") or None
    if not previous["commit"] and args.previous_commit:
        previous["commit"] = args.previous_commit
    if service.has_gate and not dev:
        # What served before this release: release_rollback.py's "previous" target.
        previous["workloads"] = before
    try:
        github = rr.github_token(service.env)
    except GuardError as error:
        print(f"WARNING: no GitHub token, the record has no commit list: {error}", file=sys.stderr)
        github = None
    record = rr.new_record(
        service.environment_named(args.env),
        kind="promote",
        commit=args.commit,
        images={str(key): str(value) for key, value in _json_file(args.images_file).items()},
        build=rr.build_info(service.env, args.build_id),
        previous=previous,
        started_at=started_at,
        token=github,
    )
    record["previous_snapshot"] = before
    if artifacts:
        record["artifacts"] = artifacts
    if service.kind == "railway" and not dev:
        # Whether the commits since the last release change the database schema: such a release never
        # rolls back by itself (forward-only).
        state = railway_migrations(service, previous.get("commit"), args.commit)
        if state is not None:
            record["migrations"] = state
            print(f"Migrations since {str(previous.get('commit'))[:9]}: {json.dumps(state)[:300]}", flush=True)
    rr.create_record(store, record, "deploy_started")
    Path(args.release_id_file).write_text(record["release_id"] + "\n", encoding="utf-8")
    print(f"Release {record['release_id']}: {store.location(rr.record_name(record['release_id']))}", flush=True)
    print(f"Previous: {previous.get('release_id') or 'unrecorded'} commit {previous.get('commit')}", flush=True)
    return 0


def dev_artifacts(service: LightService, record: dict, after: dict, runner: Runner) -> dict[str, str]:
    """What the dev stage holds now: the candidate revisions (cloud_run), the deployment Vercel built (vercel),
    or the candidate folder (composer)."""
    if service.kind == "vercel":
        wanted = str((record.get("images") or {}).get(service.name) or "")
        return vercel_deployment_artifacts(service, vercel_api(service), record["commit"], wanted)
    if service.kind == "railway":
        return railway_dev_artifacts(service, railway_api(service, "dev"), record)
    if service.kind == "cloud_run":
        return revision_artifacts(service, runner, candidate_revisions(service, after))
    return composer_artifacts(
        service, runner, GoogleApi(service.project, service.region).access_token, record["commit"]
    )


def cmd_finish(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Take a snapshot after the deploy, set the record status and point latest.json at it.

    Dev (tier full): also record the artifacts the dev stage holds; prod (tier full): free the prod lock.
    """
    release_id = (
        Path(args.release_id_file).read_text(encoding="utf-8").strip() if args.release_id_file else args.release_id
    )
    if args.env != "dev":
        # Prod: a late finish must never touch a record that a newer release or rollback replaced.
        rr.refuse_newer_latest(store, release_id)
    artifacts: dict[str, str] = {}
    problem = ""
    if args.env == "dev":
        after = snapshot_cloud_run(service, runner) if service.kind == "cloud_run" else {}
        if service.kind == "railway":
            try:
                after = snapshot_railway(service, railway_api(service, "dev"), "dev")
            except (rr.RecordError, GuardError) as error:
                print(f"WARNING: no dev snapshot: {str(error)[:200]}", file=sys.stderr, flush=True)
        try:
            artifacts = dev_artifacts(service, rr.read_record(store, release_id), after, runner)
        except (rr.RecordError, GuardError) as error:
            problem = str(error)[:300]
            print(f"WARNING: no artifacts for dev verify: {problem}", file=sys.stderr, flush=True)
    else:
        after = snapshot(service, runner, GoogleApi(service.project, service.region).access_token)

    def finish(record: dict) -> None:
        """Write the snapshot, the status and the deploy finish time on the record."""
        record["workloads"] = after
        rr.keep_finish_time(record, args.status)
        record["status"] = args.status
        if args.env == "dev":
            record["artifacts"] = artifacts
            if problem:
                record["artifacts_error"] = problem

    saved = rr.update_record(store, release_id, finish, args.status)
    try:
        rr.point_latest_unless_newer(store, saved)
    except rr.RecordError:
        if args.env != "dev" or not rr.newer_latest(store, release_id):
            # Only the refusal to hide a newer dev record is expected; any other failure surfaces.
            raise
        # Dev builds run side by side (Vercel builds several commits at once) and begin their records in the
        # order their waits end. An older one still finishes and verifies; latest.json keeps the newer record.
        print(f"Release {release_id}: {args.status}; latest.json keeps the newer dev record", flush=True)
        return 0
    print(f"Release {release_id}: {args.status}", flush=True)
    if service.has_gate and args.env == "prod":
        # The promote is over: a rollback may start now. A build that ends frees the lock too.
        try:
            rr.release_lock(store, (saved.get("build") or {}).get("id"))
        except (rr.RecordError, GuardError, OSError) as error:
            print(f"WARNING: could not free the prod lock; it ends with this build: {error}", file=sys.stderr)
    return 0


def _fail(
    service: LightService,
    store: rr.Store,
    record: dict,
    phase: str,
    regressions: list[dict],
    new_errors: dict | None = None,
) -> dict:
    """Set the failed verdict and the suggestion (the rollback command for a human).

    A window that judges several releases together and blames an older one suggests
    what ran before that older release, from its own record. A metric or check failure
    over several unjudged releases cannot tell which one regressed: it suggests what ran
    before the oldest of them (release_verify.apply_result does the same for GKE), and
    the suspects come from the commits of every release it removes.
    """
    base = record
    target = None
    commits = None
    if new_errors:
        target = rs.rollback_target(record, store, new_errors.get("problems") or [], new_errors.get("scope") or {})
    chain = list(((new_errors or {}).get("scope") or {}).get("releases") or [])
    if len(chain) > 1 and any(item.get("kind") != "new_error" for item in regressions):
        target = {"rollback_to": f"previous-of:{chain[-1]}"}
    if target:
        culprit = target["rollback_to"].split(":", 1)[1]
        base = rr.read_record(store, culprit)
        removed = chain[: chain.index(culprit) + 1] if culprit in chain else [record["release_id"]]
        if len(removed) > 1:
            commits = []
            for release_id in reversed(removed):
                commits += rr.read_record(store, release_id).get("commits") or []
    previous = base.get("previous") or {}
    suggestion = {
        "created_at": rr.utc_now(),
        "phase": phase,
        "summary": f"{len(regressions)} problem(s): " + "; ".join(item["text"] for item in regressions[:3]),
        "regressions": regressions,
        "suspected_prs": rv.suspected_prs(
            service, {**record, "commits": commits} if commits is not None else record, regressions, rr_token(service)
        ),
        "target": {
            "release_id": previous.get("release_id"),
            "commit": previous.get("commit"),
            **({"rollback_to": target["rollback_to"]} if target else {}),
        },
        "how": [rollback_command(service, base, target)],
        "note": (
            "Tier full: the watch-window build starts the rollback itself; a human can also start it."
            if service.auto_rollback
            else "Tier check_only: a human rolls back."
        ),
    }

    def change(current: dict) -> None:
        """Set the failed verdict and the suggestion, unless the record has failed already."""
        if (current.get("verdict") or {}).get("status") != "failed":
            current["verdict"] = {
                "status": "failed",
                "at": rr.utc_now(),
                "phase": phase,
                "reasons": [item["text"] for item in regressions],
            }
            current["rollback_suggestion"] = suggestion
        if phase.startswith("window_") and (current.get("checks") or {}).get(phase, {}).get("result") == "failed":
            rv.ro.note_checkpoint(current, current["checks"][phase], phase)

    return rr.update_record(store, record["release_id"], change, f"verify_{phase}", {"result": "failed"})


def rr_token(service: LightService) -> str | None:
    """The GitHub token of the service, or None when there is none."""
    try:
        return rr.github_token(service.env)
    except GuardError:
        return None


def on_failed(service: LightService, store: rr.Store, record: dict) -> None:
    """A failed prod release: tier full starts the rollback trigger (release_verify.start_rollback), else the alert."""
    if service.kind == "railway" and forward_state(service, record) is not None:
        forward_hold(service, store, {**record, "migrations": forward_state(service, record)})
        return
    if service.auto_rollback:
        rv.start_rollback(service, store, record)
    else:
        alert(service, store, record)


def suggested(service: LightService, record: dict) -> str:
    """The saved suggestion's rollback command, else the one that undoes this release."""
    how = (record.get("rollback_suggestion") or {}).get("how") or []
    return how[0] if how else rollback_command(service, record)


def alert(service: LightService, store: rr.Store, record: dict) -> bool:
    """The failed-release status line with the author ping; the fix prompt and the rollback command go to the log.

    Sent once per release (rv.alert_once): the deploy check and the watch both reach a failed verdict,
    and only a post that Slack did not take is sent again. Returns whether the line is in Slack.
    """
    if os.environ.get("RELEASE_SLACK_DISABLED", "").strip() == "1":
        print("Test run: no alert", flush=True)
        return False
    location = store.location(rr.record_name(record["release_id"]))
    if service.kind == "static_site":
        state, mentions, detail = static_alert(service, record, location, suggested(service, record))
        # A release that never went live: the old site still serves.
        words = "release failed · not published" if state == "warning" else None
    else:
        state = "down"
        mentions, detail = rv.rollback_alert(service, record, None, location, command=suggested(service, record))
        words = rv.failed_words(service, record)
    return rv.alert_once(service, store, record, state, mentions, detail, words=words)


def cmd_check(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Deploy checks right after the deploy (composer: after the DAG processor parsed the new files)."""
    if args.env == "dev":
        return cmd_dev_verify(args, service, store, runner)
    record = rr.read_record(store, args.release_id)
    if service.kind == "composer" and args.wait:
        print(f"Waiting {service.parse_wait_seconds} s for the DAG processor", flush=True)
        time.sleep(service.parse_wait_seconds)
    if service.kind == "static_site":
        checks = static_deploy_checks(service, store, record)
    else:
        checks, _ = deploy_checks(service, record, runner, GoogleApi(service.project, service.region).access_token)
    if record.get("status") == "deploy_failed":
        checks.insert(0, rv.Check("deploy", "failed", ["the deploy step failed (status deploy_failed)"]))
    for check in checks:
        print(f"   check {check.name}: {check.status}")
        for line in check.details[:12]:
            print(f"      {line}")
    failed = [check for check in checks if check.status == "failed"]
    if failed and args.failed_marker:
        # First: a record or Slack write that fails below must not turn a known failure green.
        Path(args.failed_marker).write_text("true\n", encoding="utf-8")
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
            posted = rr.post_status(
                "up",
                service.name,
                record=saved,
                words="test run",
                detail=f"{rv.TEST_RUN_LINE}. A test of {service.title} release-verify failed on purpose; no action needed.",
            )
        elif service.auto_rollback:
            # The watch-window build sees the failed verdict at once and starts the rollback trigger. A
            # forward-only release holds now, before another prod build can pass the gate.
            state = forward_state(service, saved) if service.kind == "railway" else None
            if state is not None:
                posted = forward_hold(service, store, {**saved, "migrations": state}, force=True)
            else:
                # One line for this moment: the down line here, or the "on hold" line of forward_hold above.
                posted = rv.alert_once(
                    service,
                    store,
                    saved,
                    "down",
                    rv.failure_mentions(saved, rv.load_people(), rv.likely_prs(saved)),
                    f"{service.title} release-verify {record['commit'][:9]} FAILED (deploy): "
                    f"{saved['rollback_suggestion']['summary'][:300]} An automatic rollback follows. Record {where}",
                    words=rv.failed_words(service, saved),
                )
        else:
            posted = alert(service, store, saved)
        if posted and args.alerted_marker:
            # Only a line Slack took: without it the build's notify-result step posts the failed deploy itself.
            Path(args.alerted_marker).write_text("true\n", encoding="utf-8")
        return rv.EXIT_FAILED
    minutes = " and ".join(f"+{value}" for value in service.checkpoints)
    detail = (
        f"{service.title} release-verify {record['commit'][:9]} deploy checks passed; window checks follow "
        f"at {minutes} min. Record {where}"
    )
    if service.has_dev:
        print(detail, flush=True)  # routine progress: the build log only
    else:
        rr.post_status("deploying", service.name, detail=detail, record=record)
    return rv.EXIT_OK


def dev_checks(service: LightService, record: dict, args: argparse.Namespace, runner: Runner) -> list[rv.Check]:
    """Dev verify of tier full: the dev stage holds what this build made, and it works."""
    checks = []
    if record.get("status") == "deploy_failed":
        checks.append(rv.Check("deploy", "failed", ["the dev deploy step failed (status deploy_failed)"]))
    artifacts = record.get("artifacts") or {}
    if not artifacts:
        checks.append(rv.Check("artifacts", "failed", [record.get("artifacts_error") or "the dev record has none"]))
    elif service.kind == "vercel":
        checks += vercel_dev_checks(service, record, vercel_api(service))
    elif service.kind == "railway":
        checks += railway_dev_checks(service, record, railway_api(service, "dev"))
    elif service.kind == "cloud_run":
        problems = []
        now = candidate_revisions(service, snapshot_cloud_run(service, runner))
        for name in service.services:
            revision, _, digest = str(artifacts.get(name) or "").partition("@")
            pushed = rr.digest_of((record.get("images") or {}).get(name))
            if not revision:
                problems.append(f"{name}: no candidate revision recorded")
                continue
            if not pushed or digest != pushed:
                problems.append(f"{name}: {revision} runs {digest or 'no digest'}, this build pushed {pushed}")
            if now.get(name) != revision:
                problems.append(
                    f"{name}: the {service.candidate_tag} tag moved to {now.get(name)} (a newer dev build?)"
                )
        checks.append(
            rv.Check(
                "candidate_revisions",
                "failed" if problems else "passed",
                [f"{len(service.services)} services: {', '.join(sorted(artifacts.values()))}", *problems],
            )
        )
        checks += [rv.check_http_public(check) for check in dev_smoke(service)]
    else:
        token = GoogleApi(service.project, service.region).access_token
        try:
            live = composer_artifacts(service, runner, token, record["commit"])
            same = live == artifacts
            detail = live["dags"] if same else f"the candidate folder changed: {live['dags']} (recorded {artifacts})"
        except (rr.RecordError, GuardError) as error:
            same, detail = False, f"the candidate folder cannot be read: {str(error)[:300]}"
        checks.append(rv.Check("candidate_folder", "passed" if same else "failed", [detail]))
        checks.append(check_parse(service, args.parse_result))
    if args.force_fail:
        checks.append(rv.Check("forced_failure", "failed", [f"forced failure (test run): {args.force_fail}"]))
    return checks


def cmd_dev_verify(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Dev verify (tier full): the dev checks, the verdict on the dev record and its commit index, the alert."""
    if not service.has_dev:
        raise rc.ConfigError(f"{service.name} is tier {service.tier}: it has no dev stage")
    record = rr.read_record(store, args.release_id)
    checks = dev_checks(service, record, args, runner)
    for check in checks:
        print(f"   check {check.name}: {check.status}")
        for line in check.details[:12]:
            print(f"      {line}")
    failed = [check for check in checks if check.status == "failed"]
    if failed and args.failed_marker:
        # First: a record or Slack write that fails below must not turn a known failure green.
        Path(args.failed_marker).write_text("true\n", encoding="utf-8")
    result = {
        "phase": "deploy",
        "at": rr.utc_now(),
        "checks": [asdict(check) for check in checks],
        "regressions": [
            {"kind": "check", "name": c.name, "text": f"{c.name}: {'; '.join(c.details[1:] or c.details)}"}
            for c in failed
        ],
        "result": "failed" if failed else "passed",
    }
    saved = rr.update_record(
        store,
        record["release_id"],
        lambda current: current.setdefault("checks", {}).__setitem__("deploy", result),
        "verify_deploy",
        {"result": result["result"]},
    )
    superseded = rv.newer_dev_build(service.dev, args.build_id) if failed else None
    saved = rv.finish_dev_verify(
        service,
        store,
        saved,
        result,
        build_url=args.build_url,
        test_run=bool(args.force_fail),
        token=rr_token(service),
        superseded_by=superseded,
    )
    if args.failed_marker and saved["verdict"]["status"] != "failed":
        Path(args.failed_marker).unlink(missing_ok=True)  # superseded: nothing to promote, not red
    return rv.EXIT_FAILED if saved["verdict"]["status"] == "failed" else rv.EXIT_OK


def evaluate_window(service: LightService, store: rr.Store, record: dict, runner: Runner, now: datetime) -> dict:
    """New error log lines (and composer: newly failing DAGs), traced to suspects.

    Kind vercel reads Vercel's request logs instead (evaluate_vercel_window); its result also carries
    "regressions" and "warnings" of its own (the 5xx share, the smoke checks).
    """
    if service.kind == "vercel":
        return evaluate_vercel_window(service, store, record, now)
    if service.kind == "railway":
        return evaluate_railway_window(service, store, record, now)
    if service.kind == "static_site":
        return evaluate_static_window(service, store, record, now)
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


# How long a checkpoint waits for another deploy build of the service.
DEPLOY_WAIT_SECONDS = 2700
# How many times a checkpoint runs again when a deploy build starts during its checks.
CHECKPOINT_ATTEMPTS = 3


def running_deploys(service: LightService, own_build: str | None) -> list[str]:
    """Other deploy builds of the service that run now."""
    names = " OR ".join(f'substitutions.TRIGGER_NAME="{name}"' for name in service.deploy_triggers)
    running = f'({names}) AND (status="PENDING" OR status="QUEUED" OR status="WORKING")'
    api = GoogleApi(service.project, service.region)
    builds = api.list_builds(running)
    if service.has_gate:
        # A full-tier prod build changes nothing before its release-record-begin step (it waits for the dev
        # build, its turn and the gate); a rollback build changes prod.
        builds = [build for build in builds if build.changing_cluster]
        builds += api.ongoing_rollbacks(service.rollback_build_tag)
    return [build.describe() for build in builds if build.id != own_build]


def replaced_by(service: LightService, store: rr.Store, record: dict) -> str | None:
    """The newer release, or the deploy build that runs now, that replaces this release; None if neither."""
    pointer, _ = store.read("latest.json")
    if pointer and pointer.get("release_id") != record["release_id"]:
        return f"release {pointer['release_id']}"
    running = running_deploys(service, (record.get("build") or {}).get("id"))
    return f"deploy build {running[0]}" if running else None


def wait_for_deploys(
    service: LightService,
    store: rr.Store,
    record: dict,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """Wait while another deploy build runs; return the newer release that replaced this one, else None."""
    deadline = clock() + DEPLOY_WAIT_SECONDS
    while True:
        pointer, _ = store.read("latest.json")
        if pointer and pointer.get("release_id") != record["release_id"]:
            return f"release {pointer['release_id']}"
        running = running_deploys(service, (record.get("build") or {}).get("id"))
        if not running:
            return None
        if clock() >= deadline:
            raise rr.RecordError(f"deploy build {running[0]} still runs after {DEPLOY_WAIT_SECONDS // 60} min")
        print(f"Waiting: deploy build {running[0]} runs", flush=True)
        sleep(60)


def finish_inconclusive(service: LightService, store: rr.Store, record: dict, key: str, why: str) -> None:
    """The last checkpoint could not run: the verdict is inconclusive (never left pending) and a human hears of it."""

    def done(current: dict) -> None:
        """Set the inconclusive verdict, unless the record has failed already."""
        if (current.get("verdict") or {}).get("status") != "failed":
            current["verdict"] = {
                "status": "inconclusive",
                "unchecked": True,
                "at": rr.utc_now(),
                "phase": key,
                "reasons": [why[:300]],
            }

    try:
        rr.update_record(store, record["release_id"], done, "verify_final", {"status": "inconclusive"})
    except (rr.RecordError, GuardError, OSError) as error:
        print(f"WARNING: could not save the inconclusive verdict: {error}", file=sys.stderr, flush=True)
    rr.post_status(
        "warning",
        service.name,
        rv.owner_now(record, store),
        f"{service.title} release-verify {record['commit'][:9]} inconclusive: the last checkpoint "
        f"could not run ({why[:200]}). Check the release by hand. "
        f"Record {store.location(rr.record_name(record['release_id']))}",
        record=record,
    )


def cmd_watch(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Run the window checks at each checkpoint; fail and alert on a clear new error, else pass at the end."""
    record = rr.read_record(store, args.release_id)
    if (record.get("verdict") or {}).get("status") == "failed":
        on_failed(service, store, record)
        return rv.EXIT_OK if rv.require_owner(store, rr.read_record(store, record["release_id"])) else rv.EXIT_FAILED
    finished = rr.parse_utc(record["times"].get("deploy_finished") or record["times"]["deploy_started"])
    checkpoints = sorted(int(value) for value in (args.checkpoints or " ".join(map(str, service.checkpoints))).split())
    checkpoints = rv.ro.restore_extensions(record, checkpoints)
    for index, minutes in enumerate(checkpoints):
        last = index == len(checkpoints) - 1
        current = rr.read_record(store, record["release_id"])
        receipt = ((current.get("followup") or {}).get("checkpoints") or {}).get(f"window_{minutes}m") or {}
        if (
            receipt.get("state") == "completed"
            and not getattr(args, "retry_completed", False)
            and not rv.ro.needs_final_decision(current, receipt, last)
        ):
            print(f"Checkpoint +{minutes} min already completed; preserved", flush=True)
            if (current.get("verdict") or {}).get("status") == "failed":
                # A manual check completed it with a failed verdict after this watch's first read: act on it.
                on_failed(service, store, current)
                return (
                    rv.EXIT_OK
                    if rv.require_owner(store, rr.read_record(store, record["release_id"]))
                    else rv.EXIT_FAILED
                )
            continue
        wait = (finished + timedelta(minutes=minutes) - datetime.now(rr.UTC)).total_seconds()
        if wait > 0:
            print(f"Waiting {wait / 60:.1f} min for the +{minutes} min checkpoint", flush=True)
            time.sleep(wait)
        try:
            rr.update_record(
                store,
                record["release_id"],
                lambda current, checkpoint=minutes: rv.ro.begin_checkpoint(current, f"window_{checkpoint}m"),
                "checkpoint_started",
            )
            new_errors = None
            for _ in range(CHECKPOINT_ATTEMPTS):
                if replaced := wait_for_deploys(service, store, record):
                    rr.update_record(
                        store,
                        record["release_id"],
                        lambda current, reason=replaced: rv.ro.supersede(current, reason),
                        "verify_superseded",
                    )
                    print(f"Superseded by {replaced}; the watch stops", flush=True)
                    return rv.EXIT_OK
                new_errors = evaluate_window(service, store, record, runner, datetime.now(rr.UTC))
                # A deploy build that started during the checks may have changed what they read. Only a
                # newer release ends the watch; a build that only runs makes the checkpoint run again.
                if replaced := replaced_by(service, store, record):
                    if replaced.startswith("release "):
                        rr.update_record(
                            store,
                            record["release_id"],
                            lambda current, reason=replaced: rv.ro.supersede(current, reason),
                            "verify_superseded",
                        )
                        print(f"Superseded by {replaced} during the checks; the watch stops", flush=True)
                        return rv.EXIT_OK
                    print(f"{replaced} started during the checks; the checkpoint runs again", flush=True)
                    new_errors = None
                    continue
                history = rv.builds_since_rollback(service, record)
                if history:
                    reason = f"another build changed prod during this observation: {history[0]}"
                    rv._record_checkpoint_error(store, record, minutes, reason, final=True)
                    rr.post_status(
                        "warning",
                        service.name,
                        rv.owner_now(record, store),
                        f"{service.title} release-verify unchecked: {reason}",
                        record=record,
                    )
                    return rv.EXIT_FAILED
                break
            if new_errors is None:
                raise rr.RecordError(f"deploy builds kept starting during {CHECKPOINT_ATTEMPTS} attempts")
        except (rr.RecordError, GuardError, subprocess.SubprocessError, OSError, ValueError, KeyError) as error:
            print(f"Checkpoint +{minutes} min could not run: {error}", file=sys.stderr, flush=True)
            rv._record_checkpoint_error(store, record, minutes, str(error), final=last)
            rr.post_status(
                "warning",
                service.name,
                rv.owner_now(record, store),
                f"{service.title} +{minutes} min checkpoint inconclusive: could not run: {str(error)[:200]}",
                record=record,
            )
            if last:
                return rv.EXIT_FAILED
            continue
        key = f"window_{minutes}m"
        regressions = [rv.new_error_regression(problem) for problem in new_errors["problems"] if problem["clear"]]
        warnings = [
            rv.new_error_regression(problem)
            for problem in new_errors["problems"]
            if not problem["clear"] and problem.get("suspects") and problem["suspects"][0].get("linked")
        ]
        # Kind vercel: the 5xx share and the smoke checks judge the release too. When the domains no longer
        # serve the release (judge False), nothing fails: every finding is a warning.
        regressions += list(new_errors.get("regressions") or [])
        warnings += list(new_errors.get("warnings") or [])
        if new_errors.get("judge") is False:
            warnings, regressions = warnings + regressions, []
        checked = window_checked(new_errors)
        result = {
            "phase": "window",
            "at": rr.utc_now(),
            "new_errors": new_errors,
            "warnings": warnings,
            "result": "failed" if regressions else "passed" if checked else "inconclusive",
        }
        fresh: list[dict] = []

        def store_result(
            current: dict, key: str = key, result: dict = result, complete: bool = not last and not regressions
        ) -> None:
            """Save the checkpoint result and collect the warnings not posted yet into fresh."""
            current.setdefault("checks", {})[key] = result
            if complete:
                rv.ro.note_checkpoint(current, result, key)
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
        owner_ok = rv.require_owner(store, saved)
        for problem in new_errors["problems"]:
            print(f"   {'FAILS' if problem['clear'] else 'warns'}  {problem['text'][:300]}")
        if regressions:
            saved = _fail(service, store, saved, key, regressions, new_errors)
            on_failed(service, store, saved)
            return rv.EXIT_OK if owner_ok else rv.EXIT_FAILED
        if (saved.get("verdict") or {}).get("status") == "failed":
            # A manual check failed the release while this watch slept or checked. That check only records
            # and alerts, so the watch acts on it, as release_verify does after apply_result.
            on_failed(service, store, saved)
            return rv.EXIT_OK if owner_ok else rv.EXIT_FAILED
        if fresh:
            rv.post_warnings(service, store, saved, fresh, command=rollback_command(service, saved))
        if last:
            status = "passed" if checked else "inconclusive"

            def done(current: dict, status: str = status) -> None:
                """Set the final verdict, unless the record has failed or a later checkpoint decided it."""
                verdict = current.get("verdict") or {}
                if verdict.get("status") != "failed" and not rv.ro.later_terminal(verdict, key):
                    current["verdict"] = {
                        "status": status,
                        "at": rr.utc_now(),
                        "phase": key,
                        "unchecked": status == "inconclusive",
                    }
                rv.ro.note_checkpoint(current, result, key)

            final = rr.update_record(store, record["release_id"], done, "verify_final", {"status": status})
            if (final.get("verdict") or {}).get("status") == "failed":
                # A manual check failed the release after this checkpoint's save; the final write kept it.
                on_failed(service, store, final)
                return rv.EXIT_OK if rv.require_owner(store, final) else rv.EXIT_FAILED
            rr.post_status(
                "up" if checked else "warning",
                service.name,
                [] if checked else rv.owner_now(final, store),
                f"{service.title} release-verify {record['commit'][:9]} {status}. "
                f"Record {store.location(rr.record_name(record['release_id']))}",
                record=final,
            )
    current = rr.read_record(store, record["release_id"])
    return (
        rv.EXIT_FAILED
        if (current.get("verdict") or {}).get("status") not in ("passed", "warning", "failed")
        or not rv.require_owner(store, current)
        else rv.EXIT_OK
    )


def window_checked(new_errors: dict) -> bool:
    """Whether a checkpoint's checks all ran: the logs part, or every part when the kind reports "complete"."""
    if "complete" in new_errors:
        return bool(new_errors["complete"])
    return new_errors["parts"].get("logs", {}).get("status") == "checked"


def window_sources(service: LightService) -> list[Path]:
    """The files the watch window build needs: the kit files and the runtime source files."""
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
    if service.site is not None and service.site.ignore_file and (REPO_ROOT / service.site.ignore_file).is_file():
        # The static window applies the publisher's ignore file when GitHub cannot list the page sets.
        sources.append(REPO_ROOT / service.site.ignore_file)
    return sources


def cmd_schedule_window(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Copy the sources and submit the watch window build; note its id on the record."""
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
    if service.auto_rollback:
        # Real releases only: a test run (--no-slack) never rolls back.
        substitutions += f",_AUTO_ROLLBACK={'' if args.no_slack else '1'}"
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
        lambda current: (
            current.setdefault("checks", {}).__setitem__(
                "window_build", {"id": build_id, "checkpoints_minutes": list(service.checkpoints)}
            ),
            rv.ro.merge_plan(current, list(service.checkpoints)),
        ),
        "window_scheduled",
        {"build_id": build_id},
    )
    print(f"Watch window build {build_id} submitted", flush=True)
    return rv.EXIT_OK


def cmd_turn(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Single flight of the deploy build: an older one that runs is waited for, a newer one supersedes this one.

    Prod (tier check_only): the commit must be on the release branch. Dev (tier full): the dev
    triggers' builds; a manual dev run of a branch is allowed (it never promotes). Exit 3:
    superseded (deploy nothing, not a failure).
    """
    api = GoogleApi(service.project, service.region)
    dev = args.env == "dev"
    if dev and not service.has_dev:
        raise rc.ConfigError(f"{service.name} is tier {service.tier}: it has no dev stage")
    turn = guard.Guard(
        api=api,
        build_id=args.build_id,
        commit=args.commit,
        peer_triggers=service.dev_triggers if dev else service.deploy_triggers,
        promoting_trigger=service.dev_triggers[0] if dev else "",
        wait_only=bool(getattr(args, "wait_only", False)),
    )
    try:
        own = turn.own_build()
        if not dev:
            token = api.repository_read_token(service.connection, service.connected_repository)
            turn.check_on_branch(service.github_repo, service.release_branch, token)
        turn.wait_for_turn(own, args.older_timeout_seconds)
    except guard.Superseded as reason:
        print(f"SUPERSEDED: {reason}; this build deploys nothing", flush=True)
        return guard.EXIT_SUPERSEDED
    return rv.EXIT_OK


# --------------------------------------------------------------------------- tier full: candidates and the promote


def _full(service: LightService) -> None:
    """Raise ConfigError unless the service is tier full."""
    if not service.has_gate:
        raise rc.ConfigError(f"{service.name} is tier {service.tier}; this command is for tier full")


def gsutil_rsync(runner: Runner, source: str, target: str, *, delete: bool = True) -> None:
    """gsutil -m rsync -r [-d] source target (the deploy steps of the composer kind)."""
    runner(["gsutil", "-m", "rsync", "-r", *(["-d"] if delete else []), source, target])


def cmd_stage(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Composer dev stage: copy the commit's DAG files to its candidate folder (the DAG processor does not read it)."""
    _full(service)
    if service.kind != "composer":
        raise rc.ConfigError("stage is for kind composer; cloud_run deploys a no-traffic revision in its dev build")
    target = candidate_uri(service, dags_uri(service, runner), args.commit)
    source = Path(args.source or (REPO_ROOT / service.dags_prefix))
    if not source.is_dir():
        raise rr.RecordError(f"no DAG folder {source}")
    gsutil_rsync(runner, str(source), target)
    print(f"Staged {source} at {target}", flush=True)
    return rv.EXIT_OK


def cmd_fetch_dags(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Copy the candidate DAGs and prod's DAGs into --out-dir (candidate/, baseline/) for parse-dags."""
    _full(service)
    dags = dags_uri(service, runner)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # Invalidate a previous attempt before downloading either folder. A partial
    # rsync must not authorize a parse of the leftover/empty local directories.
    (out / "fetch-complete").unlink(missing_ok=True)
    (out / "result.json").unlink(missing_ok=True)
    for name, source in (("candidate", candidate_uri(service, dags, args.commit)), ("baseline", dags)):
        (out / name).mkdir(parents=True, exist_ok=True)
        gsutil_rsync(runner, source, str(out / name))
    # The Airflow image runs as the user airflow; it writes the result here.
    for root, folders, files in os.walk(out):
        for name in [*folders, *files]:
            os.chmod(os.path.join(root, name), 0o777 if name in folders else 0o666)
    os.chmod(out, 0o777)
    (out / "fetch-complete").write_text(args.commit + "\n", encoding="utf-8")
    print(f"Candidate and prod DAGs in {out}", flush=True)
    return rv.EXIT_OK


def cmd_parse_dags(args: argparse.Namespace) -> int:
    """Parse the candidate and the prod DAGs, each in its own process (module names would collide), into --out.

    Runs in the Airflow image of the environment's version; it needs no service config and no gcloud.
    """
    if args.one:
        Path(args.out).write_text(json.dumps(parse_dag_folder(Path(args.one)), sort_keys=True), encoding="utf-8")
        return rv.EXIT_OK
    result: dict = {}
    for role, folder in (("candidate", args.candidate), ("baseline", args.baseline)):
        with tempfile.TemporaryDirectory() as home:
            out = Path(home) / "parse.json"
            env = {
                **os.environ,
                "AIRFLOW_HOME": home,
                "AIRFLOW__CORE__DAGS_FOLDER": str(Path(folder).resolve()),
                "AIRFLOW__CORE__LOAD_EXAMPLES": "False",
            }
            command = [sys.executable, str(Path(__file__).resolve()), "parse-dags", "--one", folder, "--out", str(out)]
            try:
                done = subprocess.run(
                    command, env=env, capture_output=True, text=True, timeout=args.timeout, check=False
                )
                result[role] = (
                    json.loads(out.read_text(encoding="utf-8"))
                    if done.returncode == 0
                    else {"error": (done.stderr or done.stdout).strip()[-1500:]}
                )
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                result[role] = {"error": str(error)}
        if result[role].get("error"):
            result["error"] = f"{role}: {result[role]['error'][-500:]}"
        print(f"{role}: {json.dumps({k: v for k, v in result[role].items() if k != 'errors'})}", flush=True)
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return rv.EXIT_OK


def _prod_guard(args: argparse.Namespace, service: LightService, api: GoogleApi) -> guard.Guard:
    """Single flight of the prod builds of tier full: a newer one supersedes, an older one or a rollback is waited for."""
    return guard.Guard(
        api=api,
        build_id=args.build_id,
        commit=args.commit,
        poll_seconds=args.poll_seconds,
        peer_triggers=service.deploy_triggers,
        rollback_tag=service.rollback_build_tag,
    )


def _verified_artifacts(service: LightService, commit: str, dry_run_dir: Path | None) -> dict[str, str]:
    """The artifacts that dev verify recorded for the commit; raise GuardError when there are none."""
    index, _ = open_store(service, dry_run_dir, env="dev").read(rg.dev_commit_index(commit))
    recorded = (index or {}).get("artifacts") or {}
    if not recorded:
        raise GuardError(f"dev verify recorded no artifacts for {commit[:9]}; the dev build of this commit must pass")
    return recorded


def cmd_preflight(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Prod build of tier full, first step: single flight, on the release branch, the dev build of the commit
    succeeded; then read what dev verify recorded and write the artifacts as they are now (ARTIFACTS_FILE)."""
    _full(service)
    api = GoogleApi(service.project, service.region)
    turn = _prod_guard(args, service, api)
    try:
        own = turn.own_build()
        print(f"Prod promote {own.describe()}", flush=True)
        turn.wait_for_turn(own, args.older_timeout_seconds)
        token = api.repository_read_token(service.connection, service.connected_repository)
        turn.check_on_branch(service.github_repo, service.release_branch, token)
        turn.wait_for_dev(
            own, service.dev_triggers, args.dev_timeout_seconds, service.release_branch, allow_overlap=True
        )
    except guard.Superseded as reason:
        print(f"SUPERSEDED: {reason}; this build deploys nothing", flush=True)
        return guard.EXIT_SUPERSEDED
    recorded = _verified_artifacts(service, args.commit, args.dry_run_dir)
    if service.kind == "vercel":
        vercel_hold_guard(service, store, vercel_api(service))
    live = live_artifacts(service, runner, api.access_token, args.commit, recorded)
    (Path(args.out_dir) / ARTIFACTS_FILE).write_text(
        json.dumps(live, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for name, value in sorted(live.items()):
        print(f"promote {name}: {value}", flush=True)
    return rv.EXIT_OK


def cmd_still_current(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Right before the promote: no newer prod build, no older one or rollback started, the artifacts did not change."""
    _full(service)
    api = GoogleApi(service.project, service.region)
    turn = _prod_guard(args, service, api)
    try:
        own = turn.own_build()
        if older := turn.check_superseded(own):
            raise GuardError(
                f"an older prod build or a rollback started running after preflight: {older[0].describe()}"
            )
    except guard.Superseded as reason:
        print(f"SUPERSEDED: {reason}; this build deploys nothing", flush=True)
        return guard.EXIT_SUPERSEDED
    expected = _json_file(str(Path(args.out_dir) / ARTIFACTS_FILE))
    if service.kind == "vercel":
        vercel_hold_guard(service, store, vercel_api(service))
    live = live_artifacts(service, runner, api.access_token, args.commit, expected)
    if live != expected:
        raise GuardError(f"the artifacts changed after preflight: {live} (preflight: {expected})")
    print(f"Still current: {own.id} is the newest {own.trigger_name} build and promotes {sorted(live.values())}")
    return rv.EXIT_OK


def sync_verified_dags(service: LightService, artifact: str, destination: str, runner: Runner) -> None:
    """Check the expected candidate checksum before copying and the live checksum afterward."""
    source, _, expected = artifact.partition(" ")
    if not source.startswith("gs://") or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise rr.RecordError(f"no verified DAG artifact: {artifact!r}")
    token = GoogleApi(service.project, service.region).access_token
    if folder_manifest(source, token)[0] != expected:
        raise rr.RecordError("the candidate DAG manifest changed after verification; nothing copied")
    gsutil_rsync(runner, source, destination)
    if folder_manifest(destination, token)[0] != expected:
        raise rr.RecordError("the live DAG manifest differs from the verified artifact after the copy")


def promote_artifacts(
    service: LightService, artifacts: dict[str, str], runner: Runner, store: rr.Store | None = None
) -> list[str]:
    """Send prod to the artifacts: 100% traffic to each revision, the verified Vercel deployment to the domains, or
    the candidate folder to dags/. Returns failures."""
    failures = []
    if service.kind == "vercel":
        if store is None:
            raise rc.ConfigError("kind vercel promotes with the prod store (the hold guard reads it)")
        return vercel_promote(service, vercel_api(service), artifacts, store)
    if service.kind == "railway":
        commit = str(artifacts.get(service.name) or "")
        if not guard.COMMIT_PATTERN.match(commit):
            return [f"{railway_key(service)}: no commit to deploy ({commit!r})"]
        _, problems = railway_deploy(service, railway_api(service, "prod"), "prod", commit)
        return [f"{railway_key(service)}: {problem}" for problem in problems]
    if service.kind == "cloud_run":
        for name in service.services:
            revision = str(artifacts.get(name) or "").split("@", 1)[0]
            try:
                if not revision:
                    raise rr.RecordError("no revision")
                runner(
                    [
                        "gcloud",
                        "run",
                        "services",
                        "update-traffic",
                        name,
                        f"--region={service.region}",
                        f"--project={service.project}",
                        f"--to-revisions={revision}=100",
                    ]
                )
                print(f"{name}: 100% of the traffic to {revision}", flush=True)
            except rr.RecordError as error:
                failures.append(f"cloud_run/{name}: {str(error)[:300]}")
        return failures
    try:
        target = dags_uri(service, runner)
        sync_verified_dags(service, str(artifacts.get("dags") or ""), target, runner)
        print(f"Synced the verified DAG artifact to {target}", flush=True)
    except (rr.RecordError, GuardError) as error:
        failures.append(f"composer/dags: {str(error)[:300]}")
    return failures


def cmd_apply(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """The prod deploy of a gated tier (full: the promote; check_rollback: railway's deploy): exit 1 when it failed."""
    if not service.has_gate:
        raise rc.ConfigError(f"{service.name} is tier {service.tier}; apply is for the gated tiers")
    failures = promote_artifacts(service, _json_file(args.artifacts_file), runner, store)
    for failure in failures:
        print(f"FAILED: {failure}", file=sys.stderr, flush=True)
    return rv.EXIT_ERROR if failures else rv.EXIT_OK


# --------------------------------------------------------------------------- tier full: rollback


def rollback_plan(service: LightService, latest: dict, target: dict, runner: Runner) -> rb.Plan:
    """One step per Cloud Run service (traffic split), one for the Vercel domains (deployment) or one for the DAG
    folder (commit); problems block the apply."""
    steps: list[rb.Step] = []
    problems: list[str] = []
    notes: list[str] = []
    if service.kind == "vercel":
        steps, problems, notes = vercel_rollback_steps(service, vercel_api(service), target)
    elif service.kind == "railway":
        steps, problems, notes = railway_rollback_steps(service, railway_api(service, "prod"), latest, target)
    elif service.kind == "cloud_run":
        live = snapshot_cloud_run(service, runner)
        for name in service.services:
            key = f"cloud_run/{name}"
            want = serving_split((target.get("workloads") or {}).get(key) or {})
            have = split_text(serving_split(live.get(key) or {}))
            if not want:
                steps.append(rb.Step(key, name, "traffic", have, None, "skip"))
                problems.append(f"{key}: the target release recorded no serving revisions")
                continue
            steps.append(
                rb.Step(key, name, "traffic", have, split_text(want), "none" if have == split_text(want) else "set")
            )
            for revision in want:
                try:
                    if not describe_revision(service, runner, revision)["ready"]:
                        problems.append(f"{key}: revision {revision} is not Ready")
                except (rr.RecordError, ValueError) as error:
                    problems.append(f"{key}: revision {revision} cannot be read: {str(error)[:200]}")
    else:
        commit = target.get("commit")
        steps.append(
            rb.Step("composer/dags", "dags", "commit", latest.get("commit"), commit, "set" if commit else "skip")
        )
        if not commit or not re.fullmatch(r"[0-9a-f]{40}", commit):
            problems.append("composer/dags: the target commit is unknown")
        notes.append("the target commit's candidate folder goes to dags/; a missing folder is staged from GitHub first")
    notes.append(
        f"{service.release_branch} still has {str(latest.get('commit'))[:9]}. Automatic prod deploys hold after the "
        f"rollback until a revert or a commit with the trailer 'Fixes-release: {latest.get('release_id')}' merges."
    )
    return rb.Plan(
        from_release=latest.get("release_id"),
        from_commit=latest.get("commit"),
        target_release=target.get("release_id"),
        target_commit=target.get("commit"),
        target_images=target.get("images") or {},
        steps=steps,
        not_rolled_back=[],
        notes=notes,
        problems=problems,
        target_artifacts=dict(target.get("artifacts") or {}),
    )


ROLLBACK_ATTEMPTS = "vercel_rollback_attempts"


def vercel_serving_guard(service: LightService, api: rvc.VercelApi, latest: dict) -> None:
    """An automatic rollback (_EXPECT_LATEST) refuses when the domains serve what only a human can explain.

    A rollback in the Vercel dashboard writes no kit record, so latest.json still names the release.
    Accepted, each with the kit's own evidence: the failed release's deployment; the deployment that
    served before it when the release's promote failed (deploy_failed: a promote that moved only some
    domains); and the target of a rollback attempt the kit recorded on the release before it called
    Vercel (a retry after that call moved the domains). Any other deployment: refuse.
    """
    key = vercel_key(service)
    known = {str((latest.get("artifacts") or {}).get(service.name) or "")}
    if latest.get("status") == "deploy_failed":
        known.add(str((((latest.get("previous") or {}).get("workloads") or {}).get(key) or {}).get("deployment") or ""))
    known |= {str(item.get("deployment") or "") for item in latest.get(ROLLBACK_ATTEMPTS) or []}
    known.discard("")
    serving = rvc.aliases(api, service.vercel_domains)
    moved = {domain: value for domain, value in serving.items() if value not in known}
    if not moved:
        return
    detail = "; ".join(f"{domain} serves {value or 'nothing'}" for domain, value in moved.items())
    raise rr.RecordError(
        f"a domain serves a deployment that release {latest.get('release_id')} and the kit's rollback attempts "
        f"never set: {detail}. A human moved it; the automatic rollback changes nothing"
    )


def note_rollback_attempt(service: LightService, store: rr.Store, latest: dict, plan: rb.Plan, build_id: str) -> None:
    """Before the rollback call: record its target deployment on the rolled-back release (the guard's evidence)."""
    targets = [str(step.target) for step in plan.changes() if step.target]
    if not targets:
        return
    attempt = {"deployment": targets[0], "build": build_id, "at": rr.utc_now()}
    rr.update_record(
        store,
        latest["release_id"],
        lambda current: current.setdefault(ROLLBACK_ATTEMPTS, []).append(attempt),
        "vercel_rollback_attempt",
        attempt,
    )


def vercel_target(service: LightService, target: dict) -> str:
    """The deployment a Vercel rollback returns to: what served before the target release (its snapshot), else its own."""
    served = ((target.get("workloads") or {}).get(vercel_key(service)) or {}).get("deployment")
    return str(served or (target.get("artifacts") or {}).get(service.name) or "")


def vercel_rollback_steps(
    service: LightService, api: rvc.VercelApi, target: dict
) -> tuple[list[rb.Step], list[str], list[str]]:
    """(steps, problems, notes) of a Vercel rollback: the domains go back to the target deployment."""
    key = vercel_key(service)
    wanted = vercel_target(service, target)
    serving = rvc.aliases(api, service.vercel_domains)
    have = serving.get(service.vercel_domains[0]) or None
    problems: list[str] = []
    if not wanted:
        problems.append(f"{key}: the target release recorded no deployment")
    else:
        try:
            state = rvc.deployment_state(api.deployment(wanted))
        except (rr.RecordError, GuardError) as error:
            state = f"unreadable ({str(error)[:160]})"
        if state != rvc.READY:
            problems.append(f"{key}: deployment {wanted} is {state} (expired after 30 days?); pick another --to")
    # Any domain that does not serve the target makes the rollback call (it moves every production domain).
    action = "skip" if not wanted else "none" if all(value == wanted for value in serving.values()) else "set"
    notes = [
        "Vercel turns auto-assignment of the production domains off after a rollback (its own hold); "
        "the kit's next promote turns it on again"
    ]
    return [rb.Step(key, service.name, "deployment", have, wanted or None, action)], problems, notes


def resolve_target(store: rr.Store, target: str) -> tuple[dict, dict]:
    """release_rollback.resolve_target; a "previous" of a check_only record takes its before-snapshot.

    Tier full records keep what served before in previous.workloads; records of tier check_only
    (before the move to tier full) keep it in previous_snapshot.
    """
    latest, resolved = rb.resolve_target(store, target)
    if not resolved.get("workloads") and (target == "previous" or target.startswith(rb.PREVIOUS_OF)):
        holder = latest if target == "previous" else rr.read_record(store, target[len(rb.PREVIOUS_OF) :])
        resolved["workloads"] = holder.get("previous_snapshot") or {}
    return latest, resolved


def print_rollback_plan(service: LightService, plan: rb.Plan) -> None:
    """Print the plan: the target, each step, the notes and the problems."""
    print(f"Rollback of {service.name} prod ({service.kind}) from {plan.from_release} ({str(plan.from_commit)[:9]})")
    print(f"  to {plan.target_release or 'the unrecorded release before it'} ({str(plan.target_commit)[:9]})")
    print(f"Changes ({len(plan.changes())} of {len(plan.steps)}):")
    for step in plan.steps:
        print(step.line())
    for note in plan.notes:
        print(f"Note: {note}")
    for problem in plan.problems:
        print(f"BLOCKED: {problem}")


def cmd_rollback_plan(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner | None) -> int:
    """release_rollback.py plan of a light service: print what a rollback would change; 1 when it is blocked."""
    latest, target = resolve_target(store, args.to)
    plan = rollback_plan(service, latest, target, runner or rr.run_command)
    print_rollback_plan(service, plan)
    if args.json:
        Path(args.json).write_text(json.dumps(asdict(plan), indent=2, sort_keys=True), encoding="utf-8")
    if plan.problems:
        print("\nDry run: nothing changed. This rollback is blocked; pick another target with --to.")
        return 1
    print(
        f"\nDry run: nothing changed. Start the rollback with: {service.kit_command('release_rollback.py', 'run')} --reason '<why>'"
    )
    return 0


def stage_from_github(service: LightService, commit: str, target: str, runner: Runner) -> str:
    """Stage exact commit files and return their locally computed manifest, independent of mutable GCS contents."""
    token = rr.github_token(service.prod)
    request = urllib.request.Request(
        f"https://api.github.com/repos/{service.github_repo}/tarball/{commit}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    prefix = service.dags_prefix.strip("/") + "/"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "source.tar.gz"
        with urllib.request.urlopen(request, timeout=120) as response:
            archive.write_bytes(response.read())
        root = Path(tmp) / "src"
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                # <owner>-<repo>-<sha>/dags/...: only regular files under the DAG folder, no links, no "..".
                parts = member.name.split("/", 1)
                if len(parts) < 2 or not member.isfile() or not parts[1].startswith(prefix):
                    continue
                relative = Path(parts[1])
                if relative.is_absolute() or ".." in relative.parts:
                    continue
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is not None:
                    path.write_bytes(source.read())
        folder = root / prefix.rstrip("/")
        if not folder.is_dir():
            raise rr.RecordError(f"commit {commit[:9]} has no {prefix} folder")
        expected = local_dag_manifest(folder)
        gsutil_rsync(runner, str(folder), target)
    print(f"Staged {commit[:9]} from GitHub at {target}", flush=True)
    return expected


def apply_rollback(service: LightService, plan: rb.Plan, runner: Runner) -> list[str]:
    """Set every planned traffic split, point the Vercel domains back, or sync the target commit's DAGs to dags/;
    return the failures."""
    failures = []
    for step in plan.changes():
        try:
            if service.kind == "vercel":
                api = vercel_api(service)
                project_id = str(api.project(service.vercel_project)["id"])
                api.rollback(project_id, str(step.target))
                now = rvc.wait_for_aliases(api, service.vercel_domains, str(step.target), service.assign_wait_seconds)
                print(f"{step.workload}: {step.current} -> {step.target} ({now})", flush=True)
                continue
            if service.kind == "railway":
                failures += railway_rollback(
                    service, railway_api(service, "prod"), str(step.target), plan.target_commit
                )
                continue
            if service.kind == "cloud_run":
                runner(
                    [
                        "gcloud",
                        "run",
                        "services",
                        "update-traffic",
                        step.name,
                        f"--region={service.region}",
                        f"--project={service.project}",
                        f"--to-revisions={step.target}",
                    ]
                )
                print(f"{step.workload}: traffic {step.current} -> {step.target}", flush=True)
                continue
            dags = dags_uri(service, runner)
            artifact = plan.target_artifacts.get("dags") or ""
            source = artifact.partition(" ")[0] if artifact else candidate_uri(service, dags, str(step.target))
            expected = artifact.partition(" ")[2]
            if not source.startswith("gs://") or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
                source, expected = candidate_uri(service, dags, str(step.target)), ""
            token = GoogleApi(service.project, service.region).access_token
            if not expected or folder_manifest(source, token)[0] != expected:
                staged = stage_from_github(service, str(step.target), source, runner)
                if expected and staged != expected:
                    raise rr.RecordError("the exact target commit does not match the recorded DAG artifact")
                artifact = f"{source} {staged}"
                plan.target_artifacts["dags"] = artifact
            sync_verified_dags(service, artifact, dags, runner)
            print(f"composer/dags: synced {source} to {dags}", flush=True)
        except (rr.RecordError, GuardError, OSError, tarfile.TarError) as error:
            failures.append(f"{step.workload}: {str(error)[:300]}")
    return failures


def check_rollback(service: LightService, plan: rb.Plan, after: dict) -> rv.Check:
    """Cloud Run: each service serves its target split; Vercel: every domain points at the target deployment;
    Composer: the live DAG manifest is the target's."""
    problems = []
    if service.kind == "railway":
        now = after.get(railway_key(service)) or {}
        if now.get("commit") != plan.target_commit or now.get("status") not in rwy.SERVING:
            problems.append(
                f"{railway_key(service)}: production serves {now.get('deployment')} {now.get('status')} of "
                f"{str(now.get('commit'))[:9]}, not {str(plan.target_commit)[:9]}"
            )
    elif service.kind == "vercel":
        now = (after.get(vercel_key(service)) or {}).get("aliases") or {}
        for step in plan.steps:
            problems += [
                f"{domain} serves {value or 'nothing'}, not {step.target}"
                for domain, value in sorted(now.items())
                if step.target and value != step.target
            ]
    elif service.kind == "cloud_run":
        for step in plan.steps:
            have = split_text(serving_split(after.get(step.workload) or {}))
            if step.target and have != step.target:
                problems.append(f"{step.workload}: traffic is {have}, not {step.target}")
    else:
        expected = (plan.target_artifacts.get("dags") or "").partition(" ")[2]
        have = (after.get("composer/dags") or {}).get("manifest")
        if not expected or have != expected:
            problems.append(f"composer/dags: live manifest {have!r} differs from target {expected!r}")
    return rv.Check("rollback", "failed" if problems else "passed", [f"{len(plan.steps)} changes checked", *problems])


def removed_commits(store: rr.Store, latest: dict, target: dict) -> list[str]:
    """The commits a rollback removes, newest first: the newest record's, then each earlier record's back to (not
    including) the target release, by the records' previous.release_id. A window that judged several releases
    together rolls back past all of them, so each of their commits loses its passed Deployment Check. The walk
    goes all the way to the target (a release seen twice ends it)."""
    commits: list[str] = []
    record: dict | None = latest
    seen: set[str] = set()
    while record:
        release_id = str(record.get("release_id") or "")
        if not release_id or release_id == target.get("release_id") or release_id in seen:
            break
        seen.add(release_id)
        commit = str(record.get("commit") or "")
        if commit and commit != target.get("commit") and commit not in commits:
            commits.append(commit)
        earlier = (record.get("previous") or {}).get("release_id")
        if not earlier:
            break
        try:
            record = rr.read_record(store, str(earlier))
        except (rr.RecordError, GuardError, OSError) as error:
            print(f"WARNING: cannot read release {earlier}: {str(error)[:200]}", file=sys.stderr, flush=True)
            break
    return commits


def cmd_rollback_apply(
    args: argparse.Namespace,
    service: LightService,
    store: rr.Store,
    runner: Runner | None,
    *,
    alive: Callable[[str], bool] | None = None,
) -> int:
    """release_rollback.py apply of a light service (inside the rollback build), under the prod lock."""
    run = runner or long_runner
    return rb.locked_apply(args, service, store, lambda: _rollback_apply(args, service, store, run), alive=alive)


def _rollback_apply(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Guard, record, change, check and record: the same record and hold as a GKE rollback."""
    rb.assert_alone(service, args.build_id)
    latest, target = resolve_target(store, args.target)
    rb.check_expect_latest(args, latest)
    if service.kind == "vercel" and args.expect_latest and not args.dry_run:
        vercel_serving_guard(service, vercel_api(service), latest)
    plan = rollback_plan(service, latest, target, runner)
    print_rollback_plan(service, plan)
    if plan.problems:
        raise rr.RecordError(f"rollback blocked before any change: {plan.problems[0]}")
    if args.dry_run:
        print("\nDry run (_DRY_RUN=true): nothing changed.")
        return 0
    previous = (latest.get("workloads") or {}, latest.get("images") or {})
    record = rb.create_rollback_record(service, store, args, latest, target, previous, plan)
    if service.kind == "vercel":
        note_rollback_attempt(service, store, latest, plan, args.build_id)
    failures = apply_rollback(service, plan, runner)
    cleanup_failures = []
    if service.kind == "railway":
        after = snapshot_railway(service, railway_api(service, "prod"))
    elif service.kind == "vercel":
        after = snapshot_vercel(service, vercel_api(service))
        # Every commit this rollback removes (the releases since the target, or the target of an earlier
        # rollback) must not go live again through its old success status, by a redeploy or a deploy hook.
        for commit in removed_commits(store, latest, target):
            try:
                post_commit_status(
                    service, commit, "failure", f"rolled back: release {latest.get('release_id')} failed"
                )
            except (rr.RecordError, GuardError) as error:
                print(
                    f"WARNING: the commit status of {commit[:9]} stays success: {str(error)[:200]}",
                    file=sys.stderr,
                    flush=True,
                )
    elif service.kind == "cloud_run":
        failed_candidates = {
            name: str(artifact).partition("@")[0]
            for name, artifact in (latest.get("artifacts") or {}).items()
            if name in service.services
        }
        cleanup_failures = remove_candidate_tags(service, failed_candidates)
        after = snapshot_cloud_run(service, runner)
    else:
        try:
            manifest, count = folder_manifest(
                dags_uri(service, runner), GoogleApi(service.project, service.region).access_token
            )
        except (rr.RecordError, GuardError) as error:
            manifest, count = "", 0
            failures.append(f"composer/dags: could not verify restored files: {error}")
        after = {
            "composer/dags": {
                "kind": "composer",
                "commit": target.get("commit"),
                "manifest": manifest,
                "objects": count,
            }
        }
    if plan.target_artifacts:
        target["artifacts"] = dict(plan.target_artifacts)
    check = check_rollback(service, plan, after)
    if check.status == "failed":
        failures += check.details[1:]
    return rb.finish_rollback(
        service, store, args, record, latest, target, after, [check], failures, cleanup_failures=cleanup_failures
    )


def cmd_rollback_command(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Print the command that undoes a release."""
    record = rr.read_record(store, args.release_id)
    print(rollback_command(service, record))
    return rv.EXIT_OK


def long_runner(command: list[str], timeout: float = 1800) -> str:
    """rr.run_command with the time a deploy step needs (a gsutil rsync, a traffic move)."""
    return rr.run_command(command, timeout=timeout)


# Commands that change or copy something: their gcloud and gsutil calls may take minutes.
LONG_COMMANDS = ("stage", "fetch-dags", "apply")


def cmd_own(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Acknowledge the deploying chat using the same atomic ownership protocol as GKE."""
    return rv.cmd_own(args, service, None, store, None)


def main(argv: list[str] | None = None) -> int:
    """Parse the command line, load the service and run the command; return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--service", help="service config name (release/services/<name>.json)")
    parser.add_argument("--dry-run-dir", type=Path, help="read the real store, write records only under this directory")
    commands = parser.add_subparsers(dest="command", required=True)

    def release_id_args(sub: argparse.ArgumentParser) -> None:
        """Add the --release-id and --release-id-file options to a subcommand."""
        sub.add_argument("--release-id", default="latest")
        sub.add_argument("--release-id-file")

    def env_arg(sub: argparse.ArgumentParser) -> None:
        """Add --env: prod, or dev (the dev stage of tier full)."""
        sub.add_argument("--env", default="prod", choices=["prod", "dev"])

    def prod_build_args(sub: argparse.ArgumentParser) -> None:
        """Add the options of the prod build's guard steps."""
        sub.add_argument("--build-id", required=True)
        sub.add_argument("--commit", required=True)
        sub.add_argument("--out-dir", default="/workspace")
        sub.add_argument("--poll-seconds", type=float, default=15.0)

    own = commands.add_parser("own", help="acknowledge the deploying agent's +15/+60 responsibility")
    env_arg(own)
    release_id_args(own)
    own.add_argument("--thread-url", required=True)
    own.add_argument("--pr-url", required=True)
    own.add_argument("--takeover", action="store_true")
    own.add_argument("--reason", default="")

    begin = commands.add_parser("begin", help="snapshot and write a deploying record (before the deploy)")
    env_arg(begin)
    begin.add_argument("--commit", required=True)
    begin.add_argument("--build-id")
    begin.add_argument("--release-id-file", required=True)
    begin.add_argument("--previous-commit", default="", help="the commit that ran before, when no record exists yet")
    begin.add_argument("--images-file", help="tier full, dev: JSON {service: image@digest} this build pushed")
    begin.add_argument("--artifacts-file", help="tier full, prod: the artifacts preflight wrote")
    finish = commands.add_parser("finish", help="snapshot after the deploy and set the status")
    env_arg(finish)
    release_id_args(finish)
    finish.add_argument("--status", required=True, choices=["deployed", "deploy_failed"])
    finish.add_argument("--attempts", type=int, default=1, help="retry a failed finish; the last failure alerts")
    check = commands.add_parser("check", help="deploy checks (prod) or dev verify (--env dev)")
    env_arg(check)
    release_id_args(check)
    check.add_argument("--wait", action="store_true", help="composer: wait for the DAG processor first")
    check.add_argument("--failed-marker")
    check.add_argument("--alerted-marker", default="", help="prod: written when Slack took the failed release's line")
    check.add_argument("--force-fail", default="", help="test runs: the alert says TEST RUN")
    check.add_argument("--parse-result", default="", help="composer dev verify: the parse-dags result")
    check.add_argument("--build-id", default="", help="dev verify: the dev build, to tell a superseded verify")
    check.add_argument("--build-url", default="", help="dev verify: the dev build's link for Slack")
    watch = commands.add_parser("watch", help="the window checks at each checkpoint")
    release_id_args(watch)
    watch.add_argument("--checkpoints", default="")
    watch.add_argument("--retry-completed", action="store_true")
    schedule = commands.add_parser("schedule-window", help="submit the watch-window build")
    release_id_args(schedule)
    schedule.add_argument("--source-dir", default="/workspace/release-verify-src")
    schedule.add_argument("--no-slack", action="store_true")
    turn = commands.add_parser("turn", help="single flight of the deploy build (exit 3: superseded)")
    env_arg(turn)
    turn.add_argument("--build-id", required=True)
    turn.add_argument("--commit", required=True)
    turn.add_argument("--older-timeout-seconds", type=float, default=1800)
    turn.add_argument("--wait-only", action="store_true", help="wait for older builds; a newer one never supersedes")
    command = commands.add_parser("rollback-command", help="print the command that undoes a release")
    release_id_args(command)
    stage = commands.add_parser("stage", help="tier full, composer: copy dags/ to the commit's candidate folder")
    stage.add_argument("--commit", required=True)
    stage.add_argument("--source", default="", help="default: the repository's DAG folder")
    fetch = commands.add_parser("fetch-dags", help="tier full, composer: candidate and prod DAGs for parse-dags")
    fetch.add_argument("--commit", required=True)
    fetch.add_argument("--out-dir", required=True)
    cleanup = commands.add_parser("cleanup-candidates", help="remove this build's failed Cloud Run candidate tags")
    cleanup.add_argument("--revisions-file", required=True)
    parse = commands.add_parser("parse-dags", help="tier full, composer: parse DAG folders (in the Airflow image)")
    parse.add_argument("--candidate", default="")
    parse.add_argument("--baseline", default="")
    parse.add_argument("--one", default="", help=argparse.SUPPRESS)
    parse.add_argument("--out", required=True)
    parse.add_argument("--timeout", type=float, default=1200)
    preflight = commands.add_parser("preflight", help="tier full, prod: single flight, branch, dev, artifacts")
    prod_build_args(preflight)
    preflight.add_argument("--older-timeout-seconds", type=float, default=3600)
    preflight.add_argument("--dev-timeout-seconds", type=float, default=1200)
    still = commands.add_parser("still-current", help="tier full, prod: recheck single flight and the artifacts")
    prod_build_args(still)
    apply = commands.add_parser("apply", help="tier full, prod: promote the artifacts")
    apply.add_argument("--artifacts-file", required=True)
    wait = commands.add_parser("vercel-wait", help="kind vercel, dev: wait for Vercel's deployment (exit 5: skipped)")
    wait.add_argument("--commit", required=True)
    wait.add_argument("--out-dir", default="/workspace")
    status = commands.add_parser("vercel-status", help="kind vercel: set the Deployment Check's commit status")
    status.add_argument("--commit", required=True)
    source = status.add_mutually_exclusive_group(required=True)
    source.add_argument("--state", choices=rvc.STATUS_STATES)
    source.add_argument("--from-dev-index", action="store_true", help="after the dev build: from the gate's decision")
    source.add_argument("--from-skip-file", help="after the prod build: from release_gate.py's skip reason file")
    status.add_argument("--description", default="")
    status.add_argument("--target-url", default="")
    deploy = commands.add_parser("railway-deploy", help="kind railway, dev: deploy the commit to the dev environment")
    env_arg(deploy)
    deploy.add_argument("--commit", required=True)
    deploy.add_argument("--out-dir", default="/workspace")
    publish = commands.add_parser("static-wait", help="kind static_site: wait for the publisher's check run (exit 5)")
    publish.add_argument("--commit", required=True)
    publish.add_argument("--out-dir", default="/workspace")

    args = parser.parse_args(argv)
    if args.command in (
        "vercel-wait",
        "vercel-status",
        "railway-deploy",
        "static-wait",
    ) and not guard.COMMIT_PATTERN.match(args.commit):
        print(f"release light: --commit must be a full 40-character sha, got {args.commit!r}", file=sys.stderr)
        return rv.EXIT_ERROR
    if args.command == "parse-dags":
        if not args.one and not (args.candidate and args.baseline):
            parser.error("parse-dags needs --candidate and --baseline")
        return cmd_parse_dags(args)
    if getattr(args, "release_id_file", None) and args.command != "begin":
        args.release_id = Path(args.release_id_file).read_text(encoding="utf-8").strip()
    handlers = {
        "own": cmd_own,
        "begin": cmd_begin,
        "finish": cmd_finish,
        "check": cmd_check,
        "watch": cmd_watch,
        "schedule-window": cmd_schedule_window,
        "turn": cmd_turn,
        "rollback-command": cmd_rollback_command,
        "stage": cmd_stage,
        "fetch-dags": cmd_fetch_dags,
        "cleanup-candidates": cmd_cleanup_candidates,
        "preflight": cmd_preflight,
        "still-current": cmd_still_current,
        "apply": cmd_apply,
        "vercel-wait": cmd_vercel_wait,
        "vercel-status": cmd_vercel_status,
        "railway-deploy": cmd_railway_deploy,
        "static-wait": cmd_static_wait,
    }
    try:
        service = load_light(args.service)
        env = getattr(args, "env", "prod")
        if env == "dev" and not service.has_dev:
            raise rc.ConfigError(f"{service.name} is tier {service.tier}: it has no dev stage")
        store = open_store(service, args.dry_run_dir, env=env)
        runner = long_runner if args.command in LONG_COMMANDS else rr.run_command
        if args.command == "finish":
            return rr.finish_with_retries(
                lambda: cmd_finish(args, service, store, runner),
                attempts=args.attempts,
                service=service.name,
                env=args.env,
                release_id=args.release_id,
                recovery=rr.finish_recovery("light", service.name, args.env, args.release_id, args.status),
            )
        return handlers[args.command](args, service, store, runner)
    except guard.Superseded as reason:
        print(f"SUPERSEDED: {reason}; this build deploys nothing", flush=True)
        return guard.EXIT_SUPERSEDED
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
