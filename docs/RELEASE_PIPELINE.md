# Deploys, release records, the release check and rollback

How a change reaches the Cloud Run services `orbit-mcp` and `orbit-profile-mcp`,
and how a release is recorded, checked and rolled back. The scripts are the
shared release kit in `release/` (a pinned copy of
`orbit-infrastructure/release-kit`); this repo's part is
`release/services/orbit-mcp.json` (kind `cloud_run`, tier `full`).

## A merge deploys

### Dev stage (`cloudbuild.dev.yaml`, push trigger `orbit-mcp-deploy-dev`)

1. Each image is built once (`gcr.io/orbit-search/<service>:<commit>`). The
   build keeps the pushed digests.
2. **Single flight** (`release_light.py turn --env dev`): an older dev build
   that still runs is waited for; a newer merge's build supersedes this one.
3. **Dev record** (`gs://orbit-search-release-records/orbit-mcp/dev/`).
4. **Candidates:** `gcloud run deploy ... --no-traffic --tag candidate` makes a
   new revision of each prod service. It gets no traffic.
5. **Dev verify** (`release_light.py check --env dev`): each candidate revision
   is Ready, runs the digest this build pushed and still has the tag, and
   `/health` answers `{"status": "ok"}` on the tag URLs
   (`https://candidate---orbit-mcp-k7iyipqilq-uc.a.run.app/health`, the same for
   `orbit-profile-mcp`). The verdict and the revisions go into the dev record
   and its commit index. A failure pings the author with a fix prompt, and the
   commit is not promoted.
6. **Promote request** (`release_gate.py request-promote`): when dev verify
   passed, the build starts the prod build for the same commit, unless the
   gate says freeze, hold or a newer verified commit.

The dev stage is a revision of the prod service. It reads the prod config,
secrets and data, and a request to a tag URL is a prod request. The service template and `--remove-env-vars` change at this step, before dev
verify. Existing IAM is preserved; only the traffic waits for the promote.
The HTTP entrypoints register routes and listen without starting background
jobs or calling the Orbit API; `/health` returns a constant JSON response.
This source audit supports health-only probes. It does not authorize arbitrary
MCP calls against production data during candidate validation.

### Prod promote (`cloudbuild.prod.yaml`, manual trigger `orbit-mcp-prod`)

It builds and deploys nothing. It moves the traffic to the revisions that dev
verify checked.

1. **Preflight** (`release_light.py preflight`): single flight among prod builds
   (a rollback that runs is waited for), the commit is on `main`, the dev build
   of the commit succeeded, and the revisions dev verify recorded are read live.
2. **Gate** (`release_gate.py check-prod --artifacts-file`): dev verify of this
   commit passed for exactly these revisions and digests, no freeze, no hold
   after a rollback, no newer verified commit. The build takes the prod lock.
3. **Still current**, then the **release record**
   (`gs://orbit-search-release-records/orbit-mcp/prod/`): what served before is
   the rollback target.
4. **Promote:** `gcloud run services update-traffic --to-revisions=<revision>=100`
   for each service.
5. **Deploy check:** each service sends 100% to its promoted revision, and both
   `/health` answer.
6. **Watch window** at +15 and +60 min (`release/services/orbit-mcp.release-verify.yaml`):
   new warning and error lines of both services in Cloud Logging, compared with
   the 24 h before the release, each traced to a suspected commit.

A failed release rolls back on its own: the watch-window build starts the
trigger `orbit-mcp-rollback` with `_EXPECT_LATEST` (it refuses when prod moved
on), and Slack pings the suspected PR's author with a fix prompt. After a
rollback, prod promotes hold until a revert of the held PRs or a commit with the
trailer `Fixes-release: <release id>` merges.

## Commands

```bash
python3 release/release_gate.py status
python3 release/release_gate.py freeze --reason "<why>"        # stop automatic promotes
python3 release/release_gate.py unfreeze --reason "<why>"
python3 release/release_gate.py resume --reason "<why>"        # end the hold after a rollback
python3 release/release_gate.py promote [--commit <sha>]       # break glass: promote a verified commit
python3 release/release_rollback.py plan                       # dry run: the traffic split it would set
python3 release/release_rollback.py run --reason "<why>"       # traffic back to the previous release
```

Never cancel a dev, prod, rollback or release-verify build. To keep a merge out
of prod, freeze.

## Setup (once)

```bash
gcloud builds triggers create github --name=orbit-mcp-deploy-dev --region=us-central1 --project=orbit-search --repository=projects/orbit-search/locations/us-central1/connections/orbit-search/repositories/orbit-search-orbit-mcp --branch-pattern='^main$' --build-config=cloudbuild.dev.yaml --service-account=projects/orbit-search/serviceAccounts/cloud-build@orbit-search.iam.gserviceaccount.com
gcloud builds triggers create manual --name=orbit-mcp-prod --region=us-central1 --project=orbit-search --repository=projects/orbit-search/locations/us-central1/connections/orbit-search/repositories/orbit-search-orbit-mcp --branch=main --build-config=cloudbuild.prod.yaml --service-account=projects/orbit-search/serviceAccounts/cloud-build@orbit-search.iam.gserviceaccount.com
gcloud builds triggers create manual --name=orbit-mcp-rollback --region=us-central1 --project=orbit-search --repository=projects/orbit-search/locations/us-central1/connections/orbit-search/repositories/orbit-search-orbit-mcp --branch=main --build-config=release/services/orbit-mcp.rollback.yaml --service-account=projects/orbit-search/serviceAccounts/cloud-build@orbit-search.iam.gserviceaccount.com
gcloud builds triggers delete orbit-mcp-deploy --region=us-central1 --project=orbit-search --quiet
```

The build identity `cloud-build@` already has `roles/run.admin` (deploy
revisions, move traffic) and `roles/iam.serviceAccountUser` (deploy as the
services' runtime identity).
