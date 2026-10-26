#!/usr/bin/env python3
"""Static sites in the release kit: kind "static_site", tier check_only.

A hosted publisher (Mintlify for orbit-search/docs) builds and publishes the
site on every merge to the release branch. It has no staged build to verify
first, no API to promote one, and (below its Enterprise plan) no rollback the
kit could call. So the kit only watches:

1. The merge build waits for the publisher's GitHub check run of the commit
   (Mintlify: "Mintlify Deployment"). Success: the commit is live. Failure or
   no run in time: nothing went live; the author is pinged.
2. The release record (the kit's store layout, gs://orbit-search-release-records/<service>/prod/).
3. Page checks of the live site at +0, +15 and +60 min, read-only GETs:
   the sitemap parses and lost no page this release did not remove; every
   sitemap page answers 200; the required paths (llms.txt) answer 200; each
   page the release changed answers 200 and carries its frontmatter title; the
   internal links on those pages resolve. External links are not checked.
   Pages are fetched with a cache-busting query, so a CDN copy older than the
   publish does not hide what the publisher serves. At +0 a changed page
   without its new title is a warning (the publisher may still be warming up);
   at +15 and +60 it fails the release.
4. A failed check pings the PR author in Slack with the exact revert command
   and a fix prompt. Nothing is reverted by itself: a bot must not author
   commits, and the publisher has no rollback API.

Part of the release kit (orbit-infrastructure/release-kit). Stdlib only (Python 3.10).
"""

from __future__ import annotations

import base64
import fnmatch
import html
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_config as rc  # noqa: E402
import release_record as rr  # noqa: E402

USER_AGENT = "orbit-release-kit"
# The Secret Manager secret the builds put in GITHUB_TOKEN: reads the repository, its commits and check runs.
DEFAULT_GITHUB_SECRET = "release-kit-github-token"
# Check run conclusions: published, nothing published, failed.
PUBLISHED = frozenset({"success"})
NOTHING = frozenset({"skipped", "neutral"})
EXIT_NOTHING_PUBLISHED = 5
# A page body is read up to this size (Mintlify pages are 100-300 KB).
PAGE_READ_BYTES = 2_000_000
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
HREF_RE = re.compile(r"""\shref\s*=\s*["']([^"'#][^"']*)["']""", re.IGNORECASE)
FRONT_TITLE_RE = re.compile(r"^title:\s*(.+?)\s*$", re.MULTILINE)
ASSET_RE = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|ico|css|js|mjs|map|woff2?|ttf|json|xml|pdf|zip)$", re.IGNORECASE)


@dataclass(frozen=True)
class Site:
    url: str
    publisher_check: str
    publisher_wait_minutes: int = 10
    sitemap_path: str = "/sitemap.xml"
    required_paths: tuple[str, ...] = ()
    page_extensions: tuple[str, ...] = (".mdx",)
    # Repository folders that are never pages (snippets, scripts, the kit).
    not_pages: tuple[str, ...] = ("snippets/", "scripts/", "release/", ".github/", ".agents/", ".claude/")
    github_token_secret: str = DEFAULT_GITHUB_SECRET
    max_links: int = 300
    # The publisher's ignore file in the repository (gitignore-like): files it lists are never pages.
    ignore_file: str = ".mintignore"


def parse_site(data: dict, where: str) -> Site:
    """The static_site block of a service config; raise ConfigError when it is invalid."""
    url = str(rc._require(data, "url", f"{where} static_site")).rstrip("/")
    if not re.fullmatch(r"https://[a-z0-9.-]+", url):
        raise rc.ConfigError(f"{where}: static_site.url must be https://<host> with no path")
    paths = tuple(str(path) for path in data.get("required_paths") or ())
    sitemap = str(data.get("sitemap_path") or "/sitemap.xml")
    if not all(path.startswith("/") for path in (*paths, sitemap)):
        raise rc.ConfigError(f"{where}: static_site paths must start with /")
    return Site(
        url=url,
        publisher_check=str(rc._require(data, "publisher_check", f"{where} static_site")),
        publisher_wait_minutes=int(data.get("publisher_wait_minutes", 10)),
        sitemap_path=sitemap,
        required_paths=paths,
        page_extensions=tuple(data.get("page_extensions") or (".mdx",)),
        not_pages=tuple(data.get("not_pages") or Site.not_pages),
        github_token_secret=str(data.get("github_token_secret") or DEFAULT_GITHUB_SECRET),
        max_links=int(data.get("max_links", 300)),
        ignore_file=str(data.get("ignore_file", ".mintignore")),
    )


Get = Callable[[urllib.request.Request], tuple[int, bytes]]


def http_get(request: urllib.request.Request) -> tuple[int, bytes]:
    """GET a page (up to PAGE_READ_BYTES); HTTP errors are statuses, not exceptions."""
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(PAGE_READ_BYTES)
    except urllib.error.HTTPError as error:
        return error.code, error.read(PAGE_READ_BYTES)


def fetch(get: Get, url: str, bust: str = "") -> tuple[int, str]:
    """(status, body text) of a GET; bust adds a query the CDN has not cached. A network error is status 0."""
    target = url
    if bust:
        target += ("&" if "?" in url else "?") + urllib.parse.urlencode({"release-check": bust})
    request = urllib.request.Request(target, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    for attempt in range(2):
        try:
            status, body = get(request)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            if attempt:
                return 0, f"error: {error}"
            continue
        if status in (502, 503, 504, 520, 521, 522, 524) and not attempt:
            continue
        return status, body.decode("utf-8", "replace")
    return 0, "no answer"


# --------------------------------------------------------------------------- the publisher's check run


def check_run(repo: str, commit: str, name: str, token: str) -> dict | None:
    """The newest check run of that name on the commit, or None."""
    query = urllib.parse.urlencode({"check_name": name, "filter": "latest", "per_page": "20"})
    payload = rr.github_json(f"repos/{repo}/commits/{commit}/check-runs?{query}", token)
    runs = [run for run in (payload.get("check_runs") or []) if run.get("name") == name]  # type: ignore[union-attr]
    return max(runs, key=lambda run: str(run.get("started_at") or "")) if runs else None


def wait_for_publish(
    repo: str,
    commit: str,
    site: Site,
    token: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    poll_seconds: float = 15,
) -> tuple[str, dict | None]:
    """("published" | "nothing" | "failed" | "missing", the check run) once the run completed or the time ran out."""
    deadline = clock() + site.publisher_wait_minutes * 60
    run = None
    while True:
        run = check_run(repo, commit, site.publisher_check, token) or run
        if run and run.get("status") == "completed":
            conclusion = str(run.get("conclusion") or "")
            return ("published" if conclusion in PUBLISHED else "nothing" if conclusion in NOTHING else "failed"), run
        if clock() >= deadline:
            return ("failed" if run else "missing"), run
        print(f"Waiting for {site.publisher_check} on {commit[:9]} ({(run or {}).get('status') or 'no run yet'})")
        sleep(poll_seconds)


def run_line(run: dict | None) -> str:
    """One line about a check run: conclusion, title, link."""
    if not run:
        return "no check run"
    output = run.get("output") or {}
    parts = [f"{run.get('name')}: {run.get('status')}/{run.get('conclusion') or '-'}"]
    if output.get("title"):
        parts.append(str(output["title"])[:160])
    if run.get("html_url"):
        parts.append(str(run["html_url"]))
    return "; ".join(parts)


# --------------------------------------------------------------------------- pages


def sitemap_urls(text: str) -> list[str]:
    """The page URLs of a sitemap (sorted, unique)."""
    return sorted({html.unescape(match) for match in LOC_RE.findall(text)})


def ignore_patterns(text: str | None) -> tuple[str, ...]:
    """The patterns of a gitignore-like file (comments and blank lines dropped; negations are not supported)."""
    return tuple(
        line.strip() for line in (text or "").splitlines() if line.strip() and not line.strip().startswith(("#", "!"))
    )


def ignored(path: str, patterns: tuple[str, ...]) -> bool:
    """Whether a repository path matches an ignore pattern: a folder (drafts/), a file, or a glob (*.draft.mdx)."""
    for pattern in patterns:
        anchored = pattern.lstrip("/")
        if anchored.endswith("/"):
            if path.startswith(anchored) or f"/{anchored}" in f"/{path}":
                return True
        elif "/" in anchored:
            if fnmatch.fnmatch(path, anchored):
                return True
        elif fnmatch.fnmatch(path.rsplit("/", 1)[-1], anchored) or fnmatch.fnmatch(path, anchored):
            return True
    return False


def route_of(path: str, site: Site) -> str | None:
    """The site route of a repository file (api/search/search.mdx -> /api/search/search), or None for a non-page."""
    if path.startswith(site.not_pages) or "/" not in path and path.startswith("."):
        return None
    for extension in site.page_extensions:
        if path.endswith(extension):
            route = "/" + path[: -len(extension)]
            if route == "/index" or route.endswith("/index"):
                route = route[: -len("index")].rstrip("/") or "/"
            return route
    return None


def front_title(text: str) -> str:
    """The title of a page's frontmatter, without quotes ("" when there is none)."""
    if not text.startswith("---"):
        return ""
    head = text.split("---", 2)[1] if text.count("---") >= 2 else ""
    match = FRONT_TITLE_RE.search(head)
    return match.group(1).strip().strip("\"'") if match else ""


def page_title(body: str) -> str:
    """The <title> of an HTML page, unescaped."""
    match = TITLE_RE.search(body)
    return html.unescape(" ".join(match.group(1).split())) if match else ""


def internal_links(body: str, page: str) -> list[str]:
    """The links of a page to other pages of the same site (no fragments, queries or assets), absolute.

    Relative links resolve against the page's own URL, as a browser resolves them.
    """
    host = urllib.parse.urlsplit(page).netloc
    out = set()
    for href in HREF_RE.findall(body):
        href = html.unescape(href.strip())
        if href.startswith(("mailto:", "tel:", "javascript:", "data:")) or href.startswith("//"):
            continue
        absolute = urllib.parse.urljoin(page, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.netloc != host or parts.scheme != "https" or ASSET_RE.search(parts.path):
            continue
        if parts.path.startswith(("/_next/", "/mintlify-assets/", "/cdn-cgi/")):
            continue
        out.add(f"https://{host}{parts.path.rstrip('/') or '/'}")
    return sorted(out)


@dataclass
class Changes:
    """What a release changed in the site's pages: routes with their titles, removed routes, and whether it is known."""

    pages: dict[str, str] = field(default_factory=dict)  # route -> frontmatter title ("" when unknown)
    # Routes that were pages at the base commit and are none at the head (deleted, renamed, newly ignored).
    removed: list[str] = field(default_factory=list)
    # False when the base or head page sets could not be read: the sitemap count is not judged.
    count_known: bool = True
    known: bool = True
    note: str = ""


def file_at(repo: str, commit: str, path: str, token: str) -> str:
    """A file of the repository at a commit ("" when it does not exist there)."""
    try:
        payload = rr.github_json(f"repos/{repo}/contents/{urllib.parse.quote(path)}?ref={commit}", token)
    except Exception as error:  # noqa: BLE001 - a missing file is a 404 GuardError
        if "404" in str(error):
            return ""
        raise
    return base64.b64decode(str(payload.get("content") or "")).decode("utf-8", "replace")  # type: ignore[union-attr]


def page_routes(repo: str, commit: str, site: Site, token: str) -> set[str]:
    """The page routes of the repository at a commit: page files that the ignore file of that commit keeps."""
    patterns = ignore_patterns(file_at(repo, commit, site.ignore_file, token)) if site.ignore_file else ()
    payload = rr.github_json(f"repos/{repo}/git/trees/{commit}?recursive=1", token)
    if payload.get("truncated"):  # type: ignore[union-attr]
        raise rr.RecordError(f"the tree of {commit[:9]} is too large to list")
    routes = set()
    for item in payload.get("tree") or []:  # type: ignore[union-attr]
        path = str(item.get("path") or "")
        route = route_of(path, site) if item.get("type") == "blob" else None
        if route and not ignored(path, patterns):
            routes.add(route)
    return routes


def changed_pages(
    repo: str, base: str | None, head: str, site: Site, token: str | None, read: Callable[[str], str | None]
) -> Changes:
    """The page routes base..head added or changed (with titles read through read(path)), and the removed routes.

    Removed: the page set at base minus the page set at head (each with its own ignore file), so deleted,
    renamed and newly ignored pages count, and files that were never pages do not.
    """
    if not base or not token:
        return Changes(known=False, count_known=False, note="the previous commit or a GitHub token is missing")
    if base == head:
        return Changes()
    try:
        payload = rr.github_json(f"repos/{repo}/compare/{base}...{head}?per_page=300", token)
    except Exception as error:  # noqa: BLE001 - GuardError, HTTP and network errors all mean unknown
        return Changes(known=False, count_known=False, note=f"the compare failed: {str(error)[:160]}")
    files = payload.get("files") or []  # type: ignore[union-attr]
    changes = Changes(known=len(files) < 300, note="300 or more changed files" if len(files) >= 300 else "")
    try:
        before, after = page_routes(repo, base, site, token), page_routes(repo, head, site, token)
        changes.removed = sorted(before - after)
    except Exception as error:  # noqa: BLE001 - GuardError, HTTP and network errors
        changes.count_known = False
        after = None
        changes.note = (
            changes.note + "; " if changes.note else ""
        ) + f"the page sets cannot be read: {str(error)[:120]}"
    # The head's ignore rules, from the build's own tree: they still apply when the page sets cannot be read.
    patterns = ignore_patterns(read(site.ignore_file)) if site.ignore_file else ()
    for item in files:
        path = str(item.get("filename") or "")
        route = route_of(path, site)
        if route is None or item.get("status") == "removed" or ignored(path, patterns):
            continue
        if after is not None and route not in after:
            continue
        changes.pages[route] = front_title(read(path) or "")
    return changes


@dataclass
class PageResult:
    """The page checks of one checkpoint: failures (each fails the release), warnings, facts for the record."""

    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    complete: bool = True


def check_pages(
    site: Site,
    commit: str,
    changes: Changes,
    previous_count: int | None,
    *,
    strict_content: bool,
    get: Get = http_get,
) -> PageResult:
    """The read-only checks of the live site (see the module docstring)."""
    result = PageResult()
    # A fresh query per run: the CDN may cache a query string, and each checkpoint must read the publisher.
    bust = f"{commit[:12]}-{int(time.time())}"
    status, text = fetch(get, site.url + site.sitemap_path, bust)
    urls = sitemap_urls(text) if status == 200 else []
    result.facts["sitemap_urls"] = len(urls)
    if status != 200 or not urls:
        result.failures.append(f"{site.sitemap_path}: HTTP {status}, {len(urls)} pages")
        result.complete = status != 0
    elif previous_count is not None and len(urls) < previous_count - len(changes.removed):
        line = (
            f"{site.sitemap_path} lists {len(urls)} pages; the previous release listed {previous_count} and this "
            f"release removed {len(changes.removed)}"
        )
        if changes.count_known:
            result.failures.append(line)
        else:
            # The removals are unknown: the count is not judged, and the checkpoint is not complete.
            result.warnings.append(f"{line}; the removals are unknown, so the count is not judged")
            result.complete = False
    for path in site.required_paths:
        code, _ = fetch(get, site.url + path, bust)
        if code != 200:
            result.failures.append(f"{path}: HTTP {code}")
    bodies: dict[str, str] = {}
    bad = []
    for url in urls:
        code, body = fetch(get, url, bust)
        if code != 200:
            bad.append(f"{urllib.parse.urlsplit(url).path}: HTTP {code}")
        else:
            bodies[url] = body
    result.facts["pages_checked"] = len(urls)
    if bad:
        result.failures.append(f"{len(bad)} sitemap pages fail: " + "; ".join(bad[:8]))
    in_sitemap = {urllib.parse.urlsplit(url).path.rstrip("/") or "/": url for url in urls}
    links: set[str] = set()
    for route, title in sorted(changes.pages.items()):
        url = in_sitemap.get(route)
        if url is None:
            # Not in the navigation (a hidden page, or a new page that did not publish): fetch it directly.
            url = site.url + (route if route != "/" else "/")
            code, direct = fetch(get, url, bust)
            if code != 200:
                line = f"{route}: changed, not in the sitemap, and HTTP {code}"
                (result.failures if strict_content else result.warnings).append(line)
                continue
            result.warnings.append(f"{route}: changed and live, not in the sitemap (a hidden page)")
            bodies[url] = direct
        body = bodies.get(url)
        if body is None:
            continue  # already a failure above
        if title and title not in page_title(body):
            line = f"{route}: the live page title is {page_title(body)[:120]!r}, the commit's title is {title!r}"
            (result.failures if strict_content else result.warnings).append(line)
        links.update(internal_links(body, url))
    links -= set(urls)
    links -= {site.url + path for path in site.required_paths}
    result.facts["links_checked"] = min(len(links), site.max_links)
    if len(links) > site.max_links:
        result.warnings.append(f"{len(links)} internal links; checked the first {site.max_links}")
    broken = []
    for url in sorted(links)[: site.max_links]:
        code, _ = fetch(get, url, bust)
        if code >= 400 or code == 0:
            broken.append(f"{urllib.parse.urlsplit(url).path}: HTTP {code}")
    if broken:
        result.failures.append(f"{len(broken)} internal links on changed pages fail: " + "; ".join(broken[:8]))
    if not changes.known:
        # The changed pages' titles and links went unchecked (a new page may not be in the sitemap yet):
        # the checkpoint is not complete, so the final verdict stays inconclusive and a later release judges it.
        result.warnings.append(f"the changed pages are unknown ({changes.note}); only the sitemap pages were checked")
        result.complete = False
    return result


def revert_command(repo: str, branch: str, commit: str, previous: str | None) -> str:
    """The commands that open a revert PR of a release (the release's commits after previous, else one commit)."""
    short = commit[:9]
    what = f"{previous}..{commit}" if previous and previous != commit else commit
    return (
        f"git fetch origin {branch} && git switch -c revert-{short} origin/{branch} && "
        f"git revert --no-edit {what} && git push -u origin HEAD && "
        f"gh pr create --repo {repo} --base {branch} --fill"
    )


# --------------------------------------------------------------------------- rendered builds

SECRETS = """\
availableSecrets:
    secretManager:
        - versionName: projects/$PROJECT_ID/secrets/slack-webhook-workspace-orbit/versions/latest
          env: "WEBHOOK_ORBIT"
        # The kit's GitHub token (release_record.github_token reads GITHUB_TOKEN): reads the repository,
        # its commits and the publisher's check runs. Read-only.
        - versionName: projects/$PROJECT_ID/secrets/{github_secret}/versions/latest
          env: "GITHUB_TOKEN"
"""

RELEASE_TEMPLATE = (
    """\
# GENERATED by release/release_kit.py render from release/services/{service}.json. Do not edit.
# Release check of {service} (kind static_site, tier check_only) on every merge
# to {branch} ({docs}); the push trigger {trigger} runs it. The publisher
# builds and publishes the site itself; this build changes nothing. It waits
# for the check run "{publisher_check}" of the commit, writes the release
# record, checks the live site {url} (pages, sitemap, links; read-only) and
# submits the watch window (+15 and +60 min). A failed check pings the PR
# author in Slack with the exact revert command and a fix prompt; nothing is
# reverted by itself.
steps:
    # Order, not single flight: this build waits for older merge builds, so records follow the commit
    # order. A newer build does not skip this one: the publisher published this commit on its own, and a
    # newer commit may publish nothing, so every published commit keeps its checks.
    - name: "gcr.io/cloud-builders/gcloud"
      id: "static-turn"
      entrypoint: "bash"
      args:
          - "-c"
          - |
              python3 release/release_light.py --service {service} turn --wait-only --build-id "$BUILD_ID" \\
                --commit "$COMMIT_SHA" --older-timeout-seconds 7200
              TURN_STATUS=$$?
              if [ "$${{TURN_STATUS}}" -ne 0 ]; then echo "single flight failed; the release is unchecked" | tee -a /workspace/release_verify_unchecked >&2; touch /workspace/nothing_published; fi
              exit 0
      timeout: "7500s"

    - name: "gcr.io/cloud-builders/gcloud"
      id: "static-wait"
      entrypoint: "bash"
      secretEnv: ["GITHUB_TOKEN"]
      args:
          - "-c"
          - |
              if [ -f /workspace/nothing_published ]; then exit 0; fi
              python3 release/release_light.py --service {service} static-wait --commit "$COMMIT_SHA" --out-dir /workspace
              WAIT_STATUS=$$?
              if [ "$${{WAIT_STATUS}}" -eq 5 ]; then touch /workspace/nothing_published; exit 0; fi
              if [ "$${{WAIT_STATUS}}" -ne 0 ]; then touch /workspace/deploy_failed; fi
              exit 0
      timeout: "{wait_seconds}s"

    - name: "gcr.io/cloud-builders/gcloud"
      id: "release-record-begin"
      entrypoint: "bash"
      secretEnv: ["GITHUB_TOKEN"]
      args:
          - "-c"
          - |
              if [ -f /workspace/nothing_published ]; then exit 0; fi
              python3 release/release_light.py --service {service} begin --commit "$COMMIT_SHA" \\
                --build-id "$BUILD_ID" --release-id-file /workspace/release_id \\
                || {{ echo "release record not written; the release is unchecked" | tee -a /workspace/release_verify_unchecked >&2; rm -f /workspace/release_id; }}

    - name: "gcr.io/cloud-builders/gcloud"
      id: "release-record-finish"
      entrypoint: "bash"
      secretEnv: ["GITHUB_TOKEN"]
      args:
          - "-c"
          - |
              if [ -f /workspace/nothing_published ] || [ ! -s /workspace/release_id ]; then exit 0; fi
              STATUS=deployed
              if [ -f /workspace/deploy_failed ]; then STATUS=deploy_failed; fi
              python3 release/release_light.py --service {service} finish \\
                --release-id-file /workspace/release_id --status "$${{STATUS}}" \\
                || {{ echo "release record not finished" | tee -a /workspace/release_verify_unchecked >&2; touch /workspace/release_record_failed; }}

    # The page checks at +0 (a failure pings the author), then the watch window at +15 and +60 min.
    - name: "gcr.io/cloud-builders/gcloud"
      id: "release-verify-deploy"
      entrypoint: "bash"
      secretEnv: ["WEBHOOK_ORBIT", "GITHUB_TOKEN"]
      args:
          - "-c"
          - |
              if [ -f /workspace/nothing_published ] || [ -f /workspace/release_record_failed ] || [ ! -s /workspace/release_id ]; then exit 0; fi
              python3 release/release_light.py --service {service} check \\
                --release-id-file /workspace/release_id --failed-marker /workspace/release_verify_failed
              VERIFY_STATUS=$$?
              if [ "$${{VERIFY_STATUS}}" -ne 0 ] && [ "$${{VERIFY_STATUS}}" -ne 2 ]; then
                echo "release-verify could not run (exit $${{VERIFY_STATUS}}); release is unchecked" | tee -a /workspace/release_verify_unchecked >&2
              fi
              if [ -f /workspace/deploy_failed ]; then exit 0; fi
              python3 release/release_light.py --service {service} schedule-window --release-id-file /workspace/release_id \\
                || echo "schedule-window failed; the +15/+60 checks are unchecked" | tee -a /workspace/release_verify_unchecked >&2
              exit 0

    - name: "gcr.io/cloud-builders/gcloud"
      id: "release-verify-result"
      entrypoint: "bash"
      args:
          - "-c"
          - |
              if [ -f /workspace/release_verify_unchecked ]; then
                echo "release UNCHECKED: $$(cat /workspace/release_verify_unchecked)" >&2
                exit 1
              fi
              if [ -f /workspace/nothing_published ]; then echo "{publisher_check} published nothing for this commit"; exit 0; fi
              if [ -f /workspace/deploy_failed ] || [ -f /workspace/release_verify_failed ]; then
                echo "the publish or the page checks failed; see the release record and Slack" >&2
                exit 1
              fi

# Up to {wait_minutes} minutes for the publisher, then the page checks.
timeout: "{build_seconds}s"
options:
    logging: CLOUD_LOGGING_ONLY

"""
    + SECRETS
)

WINDOW_TEMPLATE = (
    """\
# GENERATED by release/release_kit.py render from release/services/{service}.json. Do not edit.
# Watch window of one {service} release (kind static_site, tier check_only):
# release/release_light.py watch. The merge build submits it with the kit and
# the source tree as its source (release_light.py schedule-window). It runs the
# read-only page checks of {url} at each checkpoint, writes the release record
# and Slack lines, and changes nothing. A failed check pings the PR author with
# the revert command; nothing is reverted by itself.
steps:
    - name: "gcr.io/cloud-builders/gcloud"
      id: "release-verify-window"
      entrypoint: "bash"
      args:
          - "-c"
          - |
              set -euo pipefail
              python3 release/release_light.py --service "${{_SERVICE}}" watch \\
                --release-id "${{_RELEASE_ID}}" \\
                --checkpoints "${{_CHECKPOINTS}}"
      env:
          - "RELEASE_SLACK_DISABLED=${{_SLACK_DISABLED}}"
      secretEnv: ["WEBHOOK_ORBIT", "GITHUB_TOKEN"]
      timeout: "13800s"

substitutions:
    _SERVICE: {service}
    _RELEASE_ID: latest
    _CHECKPOINTS: "{checkpoints}"
    # "1" for test runs: print the Slack lines instead of posting them.
    _SLACK_DISABLED: ""

tags: ["{service}-release-verify"]
serviceAccount: "projects/{project}/serviceAccounts/{build_service_account}"
timeout: "14400s"
options:
    logging: CLOUD_LOGGING_ONLY

"""
    + SECRETS
)


def rendered(service: object) -> dict[str, str]:
    """The generated builds of a kind static_site service: the merge build and the watch window."""
    site: Site = service.site  # type: ignore[attr-defined]
    values = {
        "service": service.name,  # type: ignore[attr-defined]
        "project": service.project,  # type: ignore[attr-defined]
        "build_service_account": service.build_service_account,  # type: ignore[attr-defined]
        "docs": service.docs,  # type: ignore[attr-defined]
        "branch": service.release_branch,  # type: ignore[attr-defined]
        "trigger": service.deploy_triggers[0],  # type: ignore[attr-defined]
        "checkpoints": " ".join(str(value) for value in service.checkpoints),  # type: ignore[attr-defined]
        "publisher_check": site.publisher_check,
        "url": site.url,
        "github_secret": site.github_token_secret,
        "wait_minutes": site.publisher_wait_minutes,
        "wait_seconds": (site.publisher_wait_minutes + 5) * 60,
        "build_seconds": (site.publisher_wait_minutes + 160) * 60,
    }
    return {
        service.release_build_config: RELEASE_TEMPLATE.format(**values),  # type: ignore[attr-defined]
        service.window_config: WINDOW_TEMPLATE.format(**values),  # type: ignore[attr-defined]
    }
