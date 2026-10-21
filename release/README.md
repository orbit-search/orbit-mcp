# Release kit

Release safety for one Orbit service: merge deploys, dev verify, digest promotion,
release records, the release check, automatic rollback, the freeze and the hold.
It is the deep-search and orbit-api release pipeline with everything
service-specific moved into one config file.

This directory is a copy of `orbit-infrastructure/release-kit/release/`.
`KIT.json` pins every file; `python3 release/release_kit.py check` fails when a
copy changes. Change the kit in orbit-infrastructure and sync it to every repo.
The repo owns only `release/services/<service>.json` and the build files that
`release_kit.py render` writes from it.

## Tiers

| Tier | Deploy | Check | Failed release |
|---|---|---|---|
| `full` | A merge deploys dev; dev verify gates; the dev build starts the prod build, which promotes the dev digest. | Smoke checks after the deploy, watch window at +15 and +60 min. | Rolls back on its own; promotes hold. |
| `check_rollback` | The prod build of a merge deploys (no dev stage), after single flight and the freeze and hold gate. | Same. | Rolls back on its own; deploys hold. |
| `check_only` | Same as `check_rollback`. | Same. | Slack alert with the author ping and a fix prompt; a human rolls back. |

## Files

| File | What it does |
|---|---|
| `release_config.py` | Loads and validates `services/<service>.json`. |
| `promote_release_guard.py` | Single flight (`turn`), and for the full tier `preflight` (on the release branch, dev build passed, the dev digest) and `still-current`. |
| `release_record.py` | Records in `gs://orbit-search-release-records/<service>/<env>/`: `begin` before the first cluster change, `finish` after the deploy, `show`, `backfill`. |
| `release_verify.py` | `check --phase deploy` (smoke checks), `schedule-window`, `watch` (metrics and new errors at each checkpoint), the automatic rollback and the Slack alerts. |
| `release_signals.py` | New monitor alerts, new Error Tracking issues, new error log lines (Cloud Logging) and new failure series, each traced to a suspected commit. |
| `release_gate.py` | `request-promote`, `check-prod`, `check-direct`, `status`, `freeze`, `unfreeze`, `resume`, `promote`. |
| `release_rollback.py` | `plan`, `run` (starts the `<service>-rollback` trigger), `apply` (inside the rollback build). |
| `release_light.py` | Check-only services that are not GKE Deployments: kind `cloud_run` (revisions, traffic back to the previous revisions) and kind `composer` (DAG import errors, newly failing DAGs, the sync trigger at the previous commit). Records, the window and the alert work as for GKE; a human runs the printed rollback command. |
| `release_kit.py` | `check` and `render`. |
| `release_people.json` | GitHub login to Slack id, for the author ping. |

## The service config

```json
{
  "service": "slipi-management-portal",
  "title": "SMP",
  "tier": "full",
  "github_repo": "orbit-search/slipi-management-portal",
  "connected_repository": "orbit-search-slipi-management-portal",
  "release_branch": "main",
  "docs": "docs/RELEASE_PIPELINE.md",
  "image": {"repository": "us-central1-docker.pkg.dev/orbit-search/slipi-management-portal/slipi-management-portal",
            "registry_location": "us-central1", "registry_repository": "slipi-management-portal",
            "package": "slipi-management-portal"},
  "triggers": {"dev": ["slipi-management-portal-dev"], "prod": ["slipi-management-portal-prod"],
               "rollback": "slipi-management-portal-rollback"},
  "environments": {
    "prod": {"cluster": "prod", "namespace": "smp",
             "deployments": [{"name": "prod-smp-deployment", "container": "smp", "selector": "app=prod-smp-deployment"}],
             "smoke": [{"type": "http_pod", "name": "readiness", "deployment": "prod-smp-deployment",
                        "port": 3000, "path": "/api/readyz"}]},
    "dev": {"cluster": "dev", "namespace": "smp", "deployments": [...], "smoke": [...]}
  },
  "verify": {"checkpoints": [15, 60], "metrics": [], "signals": {"log_containers": ["smp"]}},
  "code": {"source_prefixes": ["app/", "lib/", "components/"], "extensions": [".ts", ".tsx", ".js"]}
}
```

- `triggers`: the first name of each list is the trigger; later names are retired
  triggers whose builds still count for single flight.
- Smoke check types: `http_pod` (a GET on every live pod through the Kubernetes API
  pod proxy: `port`, `path`, `expect_status`, `contains`, `expect_json`, `all_pods`),
  `http_public` (`url`, same expectations), `pods_ready` (`deployment`, `min_share`).
- `verify.metrics`: Datadog metrics compared with the baseline. `kind` `rate`
  (`numerator`, `denominator`) or `value` (`value`, `samples`, `value_ms`); a query
  `by {tag}` is judged per group. `ratio` and `absolute` must both be exceeded, with
  `min_samples` in both windows; `spread` also needs the value above 1.5x the highest
  baseline hour; `judged` metrics decide "passed" against `verify.min_judged`.
- `verify.signals`: `log_containers` (Cloud Logging new error lines), `monitors` (a
  Datadog event query), `error_tracking` (APM services), `datadog_env`,
  `failure_series` (`failures` and `all` count queries grouped by `fields`, `min_new`).
- `code`: which paths are runtime source (`source_prefixes`, `extensions`), tests,
  non-runtime and deploy files. A release that changes no runtime code never rolls
  back on a metric; suspects rank by the source they change.

## Rules carried over from deep-search

- A metric limit fails a release only when the release changed runtime code and the
  value is above the baseline's hourly spread (deep-search #2315).
- A new error fails a release only with a clear suspect: a score of 4 or more and 2
  above the next PR. A commit that adds the log line itself is never clear.
- The watch window judges releases that a newer release replaced before their own
  window ended together with the newer one.
- The automatic rollback claims the record with a generation-conditional write and
  passes `_EXPECT_LATEST`, so it refuses when prod moved on.
- After a rollback, deploys hold until a revert of the held PRs, a commit with the
  trailer `Fixes-release: <release id>`, or `release_gate.py resume`.
- Test runs say `TEST RUN, nothing is wrong` first and never roll back.
