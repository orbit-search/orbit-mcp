# Deploys, release records, the release check and rollback

How a change reaches the Cloud Run services `orbit-mcp` and `orbit-profile-mcp`,
and how a release is recorded, checked and rolled back. The scripts are the
shared release kit in `release/` (a pinned copy of
`orbit-infrastructure/release-kit`); this repo's part is
`release/services/orbit-mcp.json` (kind `cloud_run`, tier `check_only`).

## A deploy

The push trigger `orbit-mcp-deploy` runs `cloudbuild.yaml` for every merge to `main`:

1. **Single flight** (`release/release_light.py turn`): an older deploy that still
   runs is waited for; a newer merge's build supersedes this one. The commit must
   be on `main`.
2. Each image is built once (`gcr.io/orbit-search/<service>:<commit>`, also
   `:latest`) and deployed **by digest**.
3. **Release record** (`gs://orbit-search-release-records/orbit-mcp/prod/`): the
   serving revision of each service before the deploy is the rollback target.
4. **Deploy check:** each service's newest revision is ready and serves all
   traffic, and both `/health` answer `{"status": "ok"}`.
5. **Watch window** at +15 and +60 min (`release/services/orbit-mcp.release-verify.yaml`):
   new warning and error lines of both services in Cloud Logging, compared with
   the 24 h before the release, each traced to a suspected commit.

A failed release posts one Slack alert: what failed, the suspected PRs with their
authors mentioned, a fix prompt and the rollback command. Nothing rolls back by itself.

## Rollback (a human runs it)

```bash
python3 release/release_light.py rollback-command     # prints the command for the newest release
gcloud run services update-traffic orbit-mcp --region=us-central1 --project=orbit-search --to-revisions=<previous revision>=100
```

## Setup (once)

The repository moves to the 2nd-gen Cloud Build connection (the release check
reads GitHub through it) and the deploy trigger to `us-central1` with the
`cloud-build@` identity:

```bash
gcloud builds repositories create orbit-search-orbit-mcp --remote-uri=https://github.com/orbit-search/orbit-mcp.git --connection=orbit-search --region=us-central1 --project=orbit-search
gcloud builds triggers create github --name=orbit-mcp-deploy --region=us-central1 --project=orbit-search --repository=projects/orbit-search/locations/us-central1/connections/orbit-search/repositories/orbit-search-orbit-mcp --branch-pattern='^main$' --build-config=cloudbuild.yaml --service-account=projects/orbit-search/serviceAccounts/cloud-build@orbit-search.iam.gserviceaccount.com
gcloud builds triggers delete rmgpgab-orbit-mcp-us-central1-orbit-search-orbit-mcp--mavfj --project=orbit-search
```
