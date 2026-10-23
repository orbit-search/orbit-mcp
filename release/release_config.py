#!/usr/bin/env python3
"""The service config of the release kit: what one service deploys, where, and how it is checked.

Every repository that vendors the kit (orbit-infrastructure/release-kit) keeps one
JSON file per service in release/services/<service>.json. The kit's scripts read
it; nothing service-specific is written in the kit's code. A repository with one
service needs no --service flag; a repository with more names the service with
--service or RELEASE_SERVICE.

Tiers:
- full: a merge deploys dev, dev verify gates, prod promotes the same digest, a
  failed release rolls back on its own.
- check_rollback: no dev stage; the prod build of a merge deploys, then the release
  check runs and a failed release rolls back on its own.
- check_only: the release check runs and reports; a human starts the rollback.

Stdlib only; it runs in the Cloud Build kubectl and gcloud images (Python 3.10).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent
SERVICES_DIR = KIT_DIR / "services"
TIERS = ("full", "check_rollback", "check_only")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")


class ConfigError(Exception):
    """The service config is missing or invalid."""


@dataclass(frozen=True)
class Deployment:
    """One Deployment that the deploy build moves to the release image."""

    name: str
    container: str
    selector: str
    # Env values a rollback sets together with the image, in one patch (one rollout). For a value
    # that the deploy turns on only after its rollout, e.g. orbit-chatgpt-app's
    # OAUTH_SESSION_ISSUANCE_ENABLED: the release rolled back to ran with it on.
    rollback_env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Image:
    """The image the service builds: pull repository and its Artifact Registry coordinates."""

    repository: str  # pull prefix, for example us-central1-docker.pkg.dev/orbit-search/smp/smp
    registry_location: str  # Artifact Registry location; "us" for gcr.io
    registry_repository: str  # Artifact Registry repository; "gcr.io" for gcr.io
    package: str  # package (image) name in that repository


@dataclass(frozen=True)
class Environment:
    """The Deployments of one environment and the facts of its service that the scripts need."""

    name: str
    service: str
    title: str
    tier: str
    project: str
    region: str
    cluster: str
    namespace: str
    image: Image
    deployments: tuple[Deployment, ...]
    smoke: tuple[dict, ...]
    github_repo: str
    connection: str
    connected_repository: str
    release_branch: str
    # The builds that deploy this environment: the trigger first, then retired names whose builds still count.
    deploy_triggers: tuple[str, ...]

    @property
    def image_repository(self) -> str:
        """The pull repository of the image that this environment runs."""
        return self.image.repository

    @property
    def main_deployment(self) -> str:
        """The name of the first Deployment, the main one of the environment."""
        return self.deployments[0].name

    @property
    def deploy_trigger(self) -> str:
        """The trigger that deploys this environment now (the first of deploy_triggers)."""
        return self.deploy_triggers[0]

    @property
    def kube_context(self) -> str:
        """The kubectl context of the cluster on a laptop (gcloud container clusters get-credentials)."""
        return f"gke_{self.project}_{self.region}_{self.cluster}"

    def running_deploys_filter(self) -> str:
        """Cloud Build filter for the running builds of this environment's deploy triggers."""
        names = " OR ".join(f'substitutions.TRIGGER_NAME="{name}"' for name in self.deploy_triggers)
        return f'({names}) AND (status="PENDING" OR status="QUEUED" OR status="WORKING")'


@dataclass(frozen=True)
class Metric:
    """One Datadog metric that the watch window compares with the baseline.

    kind "rate": numerator / denominator (sums); kind "value": a value with its samples (p95).
    A query with `by {tag}` is judged per group (per route, per task type).
    """

    name: str
    kind: str
    scope: str
    ratio: float
    absolute: float
    min_samples: int
    numerator: str = ""
    denominator: str = ""
    value: str = ""
    samples: str = ""
    value_aggregator: str = "avg"
    # The value is in milliseconds (Datadog distributions); the kit compares seconds.
    value_ms: bool = False
    # Value metrics: exceeded only above this many times the highest baseline hour of the same group.
    spread: bool = False
    # The samples of this metric count as "judged" for the final verdict.
    judged: bool = False
    unit: str = ""
    # The queries can be grouped by the Datadog `version` tag (DD_VERSION is the commit): an exceeded
    # rate is then compared with the other versions that served in the same window.
    versioned: bool = False


@dataclass(frozen=True)
class Signals:
    """Where the new-error checks look."""

    log_containers: tuple[str, ...] = ()
    # Pods (or runs) that must write a new log line before it counts; 1 for a single-pod service.
    log_min_spread: int = 2
    # Datadog event query of the service's monitors, for example "source:alert service:twtwapi".
    monitors: str = ""
    # Datadog APM services whose Error Tracking issues count.
    error_tracking: tuple[str, ...] = ()
    datadog_env: str = "prod"
    # Failure series: {"failures": "sum:...{...} by {a,b}.as_count()", "all": "sum:... by {a}.as_count()",
    # "fields": ["a", "b"], "min_new": 10}
    failure_series: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Code:
    """Which files are runtime code, tests, deploy files; used to rank suspects and judge runtime changes."""

    source_prefixes: tuple[str, ...]
    extensions: tuple[str, ...]
    test_markers: tuple[str, ...] = (".test.", ".spec.", "__tests__/", "/tests/", "_test.go", "test_")
    non_runtime_prefixes: tuple[str, ...] = (".github/", ".claude/", ".codex/", "docs/")
    deploy_prefixes: tuple[str, ...] = ("k8/", "Dockerfile", "cloudbuild.")

    def is_test(self, path: str) -> bool:
        """Whether a path is a test file: it contains a test marker or its name starts with one."""
        name = path.rsplit("/", 1)[-1]
        return any(marker in path for marker in self.test_markers if "/" in marker or "." in marker) or any(
            name.startswith(marker) for marker in self.test_markers if marker.endswith("_")
        )

    def is_source(self, path: str) -> bool:
        """Whether a path is runtime code: under a source prefix, with a code extension, and not a test."""
        return path.startswith(self.source_prefixes) and path.endswith(self.extensions) and not self.is_test(path)

    def is_deploy(self, path: str) -> bool:
        """Whether a path is a deploy file (under a deploy prefix)."""
        return path.startswith(self.deploy_prefixes)


@dataclass(frozen=True)
class Service:
    name: str
    title: str
    tier: str
    github_repo: str
    connection: str
    connected_repository: str
    release_branch: str
    project: str
    region: str
    store: str
    docs: str
    image: Image
    environments: dict[str, Environment]
    rollback_trigger: str
    checkpoints: tuple[int, ...]
    min_judged: int
    metrics: tuple[Metric, ...]
    signals: Signals
    code: Code
    dashboard: str = ""
    path: Path | None = None

    @property
    def prod(self) -> Environment:
        """The prod environment."""
        return self.environments["prod"]

    @property
    def dev(self) -> Environment | None:
        """The dev environment, or None when the service has none."""
        return self.environments.get("dev")

    @property
    def has_dev(self) -> bool:
        """Whether the service has a dev environment."""
        return "dev" in self.environments

    @property
    def auto_rollback(self) -> bool:
        """Whether a failed release rolls back by itself (the full and check_rollback tiers)."""
        return self.tier in ("full", "check_rollback")

    # Deploys pass the freeze and hold gate (release_gate.py); the light kinds have none.
    has_gate = True

    @property
    def rollback_build_tag(self) -> str:
        """Builds with this tag change prod images (the rollback config); a promote waits for them."""
        return f"{self.name}-rollback"

    @property
    def verify_build_tag(self) -> str:
        """The tag of the release verify builds of this service."""
        return f"{self.name}-release-verify"

    @property
    def rollback_config(self) -> str:
        """The repository path of the rollback build config of this service."""
        return f"release/services/{self.name}.rollback.yaml"

    @property
    def window_config(self) -> str:
        """The repository path of the release verify build config of this service."""
        return f"release/services/{self.name}.release-verify.yaml"

    def kit_command(self, script: str, sub: str = "") -> str:
        """How a human runs a kit script's subcommand for this service (the Slack hints and fix prompts)."""
        return kit_command(self.name, script, sub)


# Scripts whose --service belongs to each subcommand; the others take it before the subcommand.
SUBCOMMAND_SERVICE_FLAG = ("release_record.py", "release_rollback.py", "release_verify.py")


def kit_command(service: str, script: str, sub: str = "") -> str:
    """`python3 release/<script> <sub>` with --service where that script's parser takes it."""
    flag = "" if single_service() else f" --service {service}"
    if script in SUBCOMMAND_SERVICE_FLAG:
        return f"python3 release/{script} {sub}{flag}".rstrip()
    return f"python3 release/{script}{flag} {sub}".rstrip()


def _require(data: dict, key: str, where: str) -> object:
    """Return data[key]; raise ConfigError when the key is missing or empty."""
    if key not in data or data[key] in (None, ""):
        raise ConfigError(f"{where}: missing {key!r}")
    return data[key]


def _names(value: object, where: str) -> tuple[str, ...]:
    """A name or a non-empty list of names as a tuple; raise ConfigError on anything else."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{where}: expected a non-empty list of names")
    return tuple(value)


SMOKE_TYPES = ("http_pod", "http_public", "pods_ready")


def validate_smoke(check: object, deployments: set[str], where: str) -> None:
    """A smoke check names a known type, its required fields, and a Deployment of its environment."""
    if not isinstance(check, dict):
        raise ConfigError(f"{where}: each check is an object")
    kind = check.get("type")
    if kind not in SMOKE_TYPES:
        raise ConfigError(f"{where}: type {kind!r} is not one of {SMOKE_TYPES}")
    if kind in ("http_pod", "pods_ready") and check.get("deployment") not in deployments:
        raise ConfigError(
            f"{where}: {kind} names deployment {check.get('deployment')!r}, not one of {sorted(deployments)}"
        )
    if kind == "http_pod":
        if not isinstance(check.get("port"), int) or not 0 < check["port"] < 65536:
            raise ConfigError(f"{where}: http_pod needs an integer port")
        if not str(check.get("path") or "").startswith("/"):
            raise ConfigError(f"{where}: http_pod needs a path that starts with /")
    if kind == "http_public" and not str(check.get("url") or "").startswith("https://"):
        raise ConfigError(f"{where}: http_public needs an https:// url")
    expect = check.get("expect_status")
    if expect is not None and not (
        isinstance(expect, int)
        or (isinstance(expect, list) and expect and all(isinstance(code, int) for code in expect))
    ):
        raise ConfigError(f"{where}: expect_status is a status code or a list of them")
    if "min_share" in check and not 0 < float(check["min_share"]) <= 1:
        raise ConfigError(f"{where}: min_share is in (0, 1]")


def parse_service(data: dict, path: Path | None = None) -> Service:
    """Validate a service config and build the Service."""
    where = str(path or "service config")
    name = str(_require(data, "service", where))
    if not NAME_RE.match(name):
        raise ConfigError(f"{where}: service name {name!r} must match {NAME_RE.pattern}")
    tier = str(_require(data, "tier", where))
    if tier not in TIERS:
        raise ConfigError(f"{where}: tier {tier!r} is not one of {TIERS}")
    image_data = dict(_require(data, "image", where))  # type: ignore[arg-type]
    image = Image(
        repository=str(_require(image_data, "repository", f"{where} image")).rstrip("/"),
        registry_location=str(_require(image_data, "registry_location", f"{where} image")),
        registry_repository=str(_require(image_data, "registry_repository", f"{where} image")),
        package=str(_require(image_data, "package", f"{where} image")),
    )
    triggers = dict(_require(data, "triggers", where))  # type: ignore[arg-type]
    common = {
        "service": name,
        "title": str(data.get("title") or name),
        "tier": tier,
        "project": str(data.get("project") or "orbit-search"),
        "region": str(data.get("region") or "us-central1"),
        "github_repo": str(_require(data, "github_repo", where)),
        "connection": str(data.get("connection") or "orbit-search"),
        "connected_repository": str(_require(data, "connected_repository", where)),
        "release_branch": str(data.get("release_branch") or "main"),
        "image": image,
    }
    environments: dict[str, Environment] = {}
    for env_name, env_data in dict(_require(data, "environments", where)).items():  # type: ignore[arg-type]
        env_where = f"{where} environments.{env_name}"
        deployments = tuple(
            Deployment(
                name=str(_require(item, "name", env_where)),
                container=str(_require(item, "container", env_where)),
                selector=str(_require(item, "selector", env_where)),
                rollback_env=tuple(
                    sorted((str(key), str(value)) for key, value in (item.get("rollback_env") or {}).items())
                ),
            )
            for item in env_data.get("deployments") or []
        )
        if not deployments:
            raise ConfigError(f"{env_where}: no deployments")
        smoke = tuple(env_data.get("smoke") or ())
        names = {item.name for item in deployments}
        for check in smoke:
            validate_smoke(check, names, f"{env_where} smoke")
        environments[env_name] = Environment(
            name=env_name,
            cluster=str(env_data.get("cluster") or env_name),
            namespace=str(_require(env_data, "namespace", env_where)),
            deployments=deployments,
            smoke=smoke,
            deploy_triggers=_names(triggers.get(env_name), f"{where} triggers.{env_name}"),
            **common,  # type: ignore[arg-type]
        )
    if "prod" not in environments:
        raise ConfigError(f"{where}: no prod environment")
    if tier == "full" and "dev" not in environments:
        raise ConfigError(f"{where}: tier full needs a dev environment")
    if tier != "full" and "dev" in environments:
        raise ConfigError(f"{where}: only tier full has a dev environment")
    verify = dict(data.get("verify") or {})
    metrics = tuple(
        Metric(
            name=str(_require(item, "name", f"{where} metric")),
            kind=str(_require(item, "kind", f"{where} metric")),
            scope=str(item.get("scope") or name),
            ratio=float(_require(item, "ratio", f"{where} metric")),
            absolute=float(_require(item, "absolute", f"{where} metric")),
            min_samples=int(_require(item, "min_samples", f"{where} metric")),
            numerator=str(item.get("numerator") or ""),
            denominator=str(item.get("denominator") or ""),
            value=str(item.get("value") or ""),
            samples=str(item.get("samples") or ""),
            value_aggregator=str(item.get("value_aggregator") or "avg"),
            value_ms=bool(item.get("value_ms")),
            spread=bool(item.get("spread")),
            judged=bool(item.get("judged")),
            unit=str(item.get("unit") or ("rate" if item.get("kind") == "rate" else "s")),
            versioned=bool(item.get("versioned")),
        )
        for item in verify.get("metrics") or []
    )
    for metric in metrics:
        if metric.versioned and metric.kind != "rate":
            raise ConfigError(f"{where}: metric {metric.name} is versioned, but only rate metrics can be")
        if metric.kind == "rate" and not (metric.numerator and metric.denominator):
            raise ConfigError(f"{where}: rate metric {metric.name} needs numerator and denominator")
        if metric.kind == "value" and not (metric.value and metric.samples):
            raise ConfigError(f"{where}: value metric {metric.name} needs value and samples")
        if metric.kind not in ("rate", "value"):
            raise ConfigError(f"{where}: metric {metric.name} kind {metric.kind!r} is not rate or value")
    signals_data = dict(verify.get("signals") or {})
    signals = Signals(
        log_containers=tuple(signals_data.get("log_containers") or ()),
        log_min_spread=int(signals_data.get("log_min_spread") or 2),
        monitors=str(signals_data.get("monitors") or ""),
        error_tracking=tuple(signals_data.get("error_tracking") or ()),
        datadog_env=str(signals_data.get("datadog_env") or "prod"),
        failure_series=dict(signals_data.get("failure_series") or {}),
    )
    code_data = dict(_require(data, "code", where))  # type: ignore[arg-type]
    defaults = Code(source_prefixes=(), extensions=())
    code = Code(
        source_prefixes=_names(code_data.get("source_prefixes"), f"{where} code.source_prefixes"),
        extensions=_names(code_data.get("extensions"), f"{where} code.extensions"),
        test_markers=tuple(code_data.get("test_markers") or defaults.test_markers),
        non_runtime_prefixes=tuple(code_data.get("non_runtime_prefixes") or defaults.non_runtime_prefixes),
        deploy_prefixes=tuple(code_data.get("deploy_prefixes") or defaults.deploy_prefixes),
    )
    return Service(
        name=name,
        title=common["title"],  # type: ignore[arg-type]
        tier=tier,
        github_repo=common["github_repo"],  # type: ignore[arg-type]
        connection=common["connection"],  # type: ignore[arg-type]
        connected_repository=common["connected_repository"],  # type: ignore[arg-type]
        release_branch=common["release_branch"],  # type: ignore[arg-type]
        project=common["project"],  # type: ignore[arg-type]
        region=common["region"],  # type: ignore[arg-type]
        store=str(data.get("store") or f"gs://orbit-search-release-records/{name}"),
        docs=str(data.get("docs") or "docs/RELEASE_PIPELINE.md"),
        image=image,
        environments=environments,
        rollback_trigger=str(triggers.get("rollback") or f"{name}-rollback"),
        checkpoints=tuple(int(value) for value in verify.get("checkpoints") or (15, 60)),
        min_judged=int(verify.get("min_judged") or 0),
        metrics=metrics,
        signals=signals,
        code=code,
        dashboard=str(data.get("dashboard") or ""),
        path=path,
    )


def service_files() -> list[Path]:
    """The service config files in SERVICES_DIR, sorted; empty when the directory is missing."""
    return sorted(SERVICES_DIR.glob("*.json")) if SERVICES_DIR.is_dir() else []


def single_service() -> bool:
    """Whether the repository has exactly one service config."""
    return len(service_files()) == 1


def config_path(name: str | None = None) -> Path:
    """The config file of the service named by --service or RELEASE_SERVICE, else the repository's only one."""
    name = name or os.environ.get("RELEASE_SERVICE", "").strip() or None
    files = service_files()
    if name:
        path = SERVICES_DIR / f"{name}.json"
        if not path.exists():
            raise ConfigError(f"no service config {path}")
        return path
    if len(files) == 1:
        return files[0]
    raise ConfigError(
        f"{len(files)} service configs in {SERVICES_DIR}; name one with --service "
        f"({', '.join(item.stem for item in files) or 'none'})"
    )


def config_kind(name: str | None = None) -> str:
    """The deploy kind of a service config: "gke" (the default), "cloud_run" or "composer"."""
    path = config_path(name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConfigError(f"{path}: {error}") from None
    return str(data.get("kind") or "gke")


def load_any_service(name: str | None = None) -> object:
    """The service of any kind: a GKE Service, or a release_light.LightService (kinds cloud_run, composer).

    The release gate, the rollback commands and release_kit.py take both; the light kinds give the
    same names (prod, dev, rollback_trigger, kit_command, ...), so one code path serves them.
    """
    if config_kind(name) == "gke":
        return load_service(name)
    import release_light  # noqa: PLC0415 - release_light imports this module

    return release_light.load_light(config_path(name).stem)


def load_service(name: str | None = None) -> Service:
    """The service named by --service or RELEASE_SERVICE, else the repository's only one."""
    name = name or os.environ.get("RELEASE_SERVICE", "").strip() or None
    files = service_files()
    if name:
        path = SERVICES_DIR / f"{name}.json"
        if not path.exists():
            raise ConfigError(f"no service config {path}")
    elif len(files) == 1:
        path = files[0]
    else:
        raise ConfigError(
            f"{len(files)} service configs in {SERVICES_DIR}; name one with --service "
            f"({', '.join(item.stem for item in files) or 'none'})"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConfigError(f"{path}: {error}") from None
    if data.get("kind", "gke") != "gke":
        raise ConfigError(f"{path}: kind {data.get('kind')!r} is a release_light.py service")
    service = parse_service(data, path)
    if service.name != path.stem:
        raise ConfigError(f"{path}: the file name must be the service name {service.name!r}")
    return service
