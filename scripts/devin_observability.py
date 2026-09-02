# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""
Devin observability collector, gap detector and session dispatcher.

Complements ``devin_report.py`` (issue-centric markdown board) with a
PR-centric, database-backed view meant to be explored in Superset:

* ``collect``   pull ``devin/*`` PRs, check runs, reviews, unresolved review
                threads and Devin sessions/automations; write a JSON snapshot
                and, when ``--database-url``/``DEVIN_OBS_DATABASE_URL`` is
                set, upsert everything into Postgres schema ``devin_obs``.
* ``findings``  derive actionable *gaps* that the event-driven automations
                cannot see (dropped/rate-limited events): failed CI nobody is
                working on, unresolved human review threads, ``changes
                requested`` with no follow-up commit.
* ``dispatch``  for each finding not yet dispatched, create a Devin session
                via ``POST /v3/organizations/{org}/sessions`` and leave an
                idempotency marker comment on the PR so the next run (and the
                collector) see the dispatch.

Environment:
    GITHUB_TOKEN            GitHub token (read PRs/checks; write comments for dispatch)
    GITHUB_REPOSITORY       owner/repo (defaults to lcpz/superset)
    DEVIN_API_KEY           Devin v3 key (ViewOrgSessions + UseDevinSessions)
    DEVIN_ORG_ID            Devin organization id (org-...)
    DEVIN_OBS_DATABASE_URL  postgresql://user:pass@host:5432/db (optional)

Usage:
    python scripts/devin_observability.py collect  [--json snapshot.json]
    python scripts/devin_observability.py findings [--json findings.json]
    python scripts/devin_observability.py dispatch [--dry-run] [--max 3]
    python scripts/devin_observability.py load snapshot.json   # JSON -> Postgres

See .github/devin-observability.md for the data model and finding semantics.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"
DEVIN_API = "https://api.devin.ai"

DEVIN_BRANCH_PREFIX = "devin/"
BOT_LOGINS = {"devin-ai-integration[bot]", "github-actions[bot]"}
DISPATCH_TAG = "devin-obs"
DISPATCH_MARKER = "devin-obs:dispatch"
UNATTENDED_GRACE = timedelta(hours=int(os.environ.get("DEVIN_OBS_GRACE_HOURS", "2")))
# Informational checks (e.g. the post-run timeline summary) that never gate a PR.
IGNORED_CHECKS = set(
    filter(
        None,
        os.environ.get("DEVIN_OBS_IGNORE_CHECKS", "actions-timeline").split(","),
    )
)
LOOKBACK = timedelta(days=int(os.environ.get("DEVIN_OBS_LOOKBACK_DAYS", "60")))
ACTIVE_SESSION_STATES = {"new", "claimed", "running", "resuming", "working"}
FAILED_SESSION_STATES = {"expired", "failed", "cancelled"}

JsonDict = dict[str, Any]


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _request(
    method: str, url: str, headers: dict[str, str], body: JsonDict | None = None
) -> Any:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url}")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        headers={**headers, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail[:300]}") from exc


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #
class GitHubClient:
    def __init__(self, token: str, repo: str) -> None:
        self.repo = repo
        self.owner, self.name = repo.split("/", 1)
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def _paginate(self, path: str, params: dict[str, str] | None = None) -> list[Any]:
        params = {"per_page": "100", **(params or {})}
        page, results = 1, []
        while True:
            query = urllib.parse.urlencode({**params, "page": str(page)})
            data = _request("GET", f"{GITHUB_API}{path}?{query}", self.headers)
            if not data:
                break
            results.extend(data)
            if len(data) < 100:
                break
            page += 1
        return results

    def pulls(self, since: datetime) -> list[JsonDict]:
        pulls = self._paginate(
            f"/repos/{self.repo}/pulls",
            {"state": "all", "sort": "updated", "direction": "desc"},
        )
        return [p for p in pulls if (_parse_ts(p["updated_at"]) or since) >= since]

    def check_runs(self, sha: str) -> list[JsonDict]:
        data = _request(
            "GET",
            f"{GITHUB_API}/repos/{self.repo}/commits/{sha}/check-runs?per_page=100",
            self.headers,
        )
        return list(data.get("check_runs", []))

    def reviews(self, number: int) -> list[JsonDict]:
        return self._paginate(f"/repos/{self.repo}/pulls/{number}/reviews")

    def issue_comments(self, number: int) -> list[JsonDict]:
        return self._paginate(f"/repos/{self.repo}/issues/{number}/comments")

    def commits(self, number: int) -> list[JsonDict]:
        return self._paginate(f"/repos/{self.repo}/pulls/{number}/commits")

    def review_threads(self, number: int) -> list[JsonDict]:
        """Review threads with resolution state (REST does not expose it)."""
        query = """
        query($owner:String!,$name:String!,$number:Int!,$after:String){
          repository(owner:$owner,name:$name){ pullRequest(number:$number){
            reviewThreads(first:100, after:$after){
              pageInfo{hasNextPage endCursor}
              nodes{ id isResolved isOutdated path
                comments(last:1){ nodes{ author{login} createdAt url } } } } } } }
        """
        threads: list[JsonDict] = []
        after: str | None = None
        while True:
            data = _request(
                "POST",
                GITHUB_GRAPHQL,
                self.headers,
                {
                    "query": query,
                    "variables": {
                        "owner": self.owner,
                        "name": self.name,
                        "number": number,
                        "after": after,
                    },
                },
            )
            if data.get("errors"):
                raise RuntimeError(f"GraphQL: {data['errors']}")
            conn = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            threads.extend(conn["nodes"])
            if not conn["pageInfo"]["hasNextPage"]:
                return threads
            after = conn["pageInfo"]["endCursor"]

    def comment(self, number: int, body: str) -> JsonDict:
        result: JsonDict = _request(
            "POST",
            f"{GITHUB_API}/repos/{self.repo}/issues/{number}/comments",
            self.headers,
            {"body": body},
        )
        return result

    def update_comment(self, comment_id: int, body: str) -> JsonDict:
        result: JsonDict = _request(
            "PATCH",
            f"{GITHUB_API}/repos/{self.repo}/issues/comments/{comment_id}",
            self.headers,
            {"body": body},
        )
        return result


class DevinClient:
    def __init__(self, api_key: str, org_id: str) -> None:
        self.enabled = bool(api_key and org_id)
        self.org_id = org_id
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def _url(self, path: str) -> str:
        return f"{DEVIN_API}/v3/organizations/{self.org_id}{path}"

    def sessions(self, repo: str, since: datetime) -> list[JsonDict]:
        if not self.enabled:
            return []
        params: dict[str, Any] = {
            "first": "200",
            "repo_names": repo,
            "created_after": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        results: list[JsonDict] = []
        after: str | None = None
        while True:
            if after:
                params["after"] = after
            query = urllib.parse.urlencode(params, doseq=True)
            data = _request("GET", self._url(f"/sessions?{query}"), self.headers)
            results.extend(data.get("items", []))
            if not data.get("has_next_page"):
                return results
            after = data.get("end_cursor")

    def automations(self) -> list[JsonDict]:
        if not self.enabled:
            return []
        data = _request("GET", self._url("/automations?first=100"), self.headers)
        return list(data.get("items", []))

    def create_session(
        self, prompt: str, title: str, tags: list[str], max_acu: int
    ) -> JsonDict:
        if not self.enabled:
            raise RuntimeError("DEVIN_API_KEY / DEVIN_ORG_ID not configured")
        body: JsonDict = {
            "prompt": prompt,
            "title": title,
            "tags": tags,
            "max_acu_limit": max_acu,
        }
        if user_id := os.environ.get("DEVIN_CREATE_AS_USER_ID"):
            body["create_as_user_id"] = user_id
        created: JsonDict = _request("POST", self._url("/sessions"), self.headers, body)
        return created


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class CheckRun:
    check_run_id: int
    pr_number: int
    head_sha: str
    name: str
    status: str
    conclusion: str | None
    started_at: str | None
    completed_at: str | None
    url: str | None


@dataclass
class PullRow:
    number: int
    title: str
    url: str
    author: str
    branch: str
    state: str  # open | merged | closed
    draft: bool
    created_at: str
    updated_at: str
    merged_at: str | None
    head_sha: str
    last_commit_at: str | None
    failed_at: str | None
    checks: str  # success | failure | pending | none
    failed_checks: list[str]
    approved: bool
    changes_requested: bool
    last_human_review_at: str | None
    review_threads: int
    unresolved_threads: int
    oldest_unresolved_at: str | None
    dispatches: list[JsonDict] = field(default_factory=list)


@dataclass
class SessionRow:
    session_id: str
    title: str | None
    status: str | None
    status_detail: str | None
    origin: str | None
    automation_id: str | None
    created_at: str | None
    updated_at: str | None
    acus_consumed: float
    url: str | None
    tags: list[str]
    pr_numbers: list[int]
    category: str | None


@dataclass
class AutomationRow:
    automation_id: str
    name: str
    enabled: bool
    last_status: str | None
    last_fired_at: str | None
    event_types: list[str]


@dataclass
class Finding:
    key: str
    kind: str  # ci-failed-unattended|review-unaddressed|changes-requested-unaddressed
    pr_number: int
    pr_url: str
    branch: str
    detail: str
    since: str | None
    dispatched: bool = False


@dataclass
class Snapshot:
    collected_at: str
    repo: str
    devin_api_enabled: bool
    pulls: list[PullRow]
    check_runs: list[CheckRun]
    sessions: list[SessionRow]
    automations: list[AutomationRow]
    findings: list[Finding]


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #
def _pr_numbers(repo: str, text: str) -> list[int]:
    return sorted(
        {
            int(m.group(1))
            for m in re.finditer(
                rf"https://github\.com/{re.escape(repo)}/pull/(\d+)", text or ""
            )
        }
    )


def _summarise_checks(runs: list[JsonDict]) -> tuple[str, list[str]]:
    runs = [r for r in runs if r.get("name") not in IGNORED_CHECKS]
    if not runs:
        return "none", []
    if any(r.get("status") != "completed" for r in runs):
        return "pending", []
    failed = [
        r["name"]
        for r in runs
        if r.get("conclusion") not in {"success", "skipped", "neutral"}
    ]
    return ("failure", failed) if failed else ("success", [])


def _dispatch_markers(comments: list[JsonDict]) -> list[JsonDict]:
    markers: list[JsonDict] = []
    for comment in comments:
        for match in re.finditer(
            rf"<!--\s*{DISPATCH_MARKER}\s+(\{{.*?\}})\s*-->", comment.get("body") or ""
        ):
            try:
                marker = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            marker["comment_url"] = comment.get("html_url")
            marker["created_at"] = comment.get("created_at")
            markers.append(marker)
    return markers


def collect_pull(gh: GitHubClient, pull: JsonDict) -> tuple[PullRow, list[CheckRun]]:
    number = pull["number"]
    sha = pull["head"]["sha"]
    runs = gh.check_runs(sha)
    checks, failed = _summarise_checks(runs)
    check_rows = [
        CheckRun(
            check_run_id=int(r["id"]),
            pr_number=number,
            head_sha=sha,
            name=r["name"],
            status=r.get("status", ""),
            conclusion=r.get("conclusion"),
            started_at=r.get("started_at"),
            completed_at=r.get("completed_at"),
            url=r.get("html_url"),
        )
        for r in runs
    ]
    latest: dict[str, tuple[str, str]] = {}
    for review in gh.reviews(number):
        login = (review.get("user") or {}).get("login", "")
        if login in BOT_LOGINS:
            continue
        if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[login] = (review["state"], review.get("submitted_at", ""))
    states = {s for s, _ in latest.values()}
    human_review_at = max((ts for _, ts in latest.values()), default=None)
    threads = gh.review_threads(number)
    unresolved = [
        t
        for t in threads
        if not t["isResolved"]
        and not t["isOutdated"]
        and ((t["comments"]["nodes"] or [{}])[0].get("author") or {}).get("login")
        not in BOT_LOGINS
    ]
    oldest = min(
        (
            t["comments"]["nodes"][0]["createdAt"]
            for t in unresolved
            if t["comments"]["nodes"]
        ),
        default=None,
    )
    commits = gh.commits(number)
    last_commit_at = max(
        (c["commit"]["committer"]["date"] for c in commits), default=None
    )
    failed_at_values = [
        completed_at
        for r in runs
        if r.get("status") == "completed"
        and r.get("name") not in IGNORED_CHECKS
        and r.get("conclusion") not in {"success", "skipped", "neutral"}
        and isinstance(completed_at := r.get("completed_at"), str)
    ]
    row = PullRow(
        number=number,
        title=pull["title"],
        url=pull["html_url"],
        author=(pull.get("user") or {}).get("login", ""),
        branch=pull["head"]["ref"],
        state="merged" if pull.get("merged_at") else pull.get("state", "open"),
        draft=bool(pull.get("draft")),
        created_at=pull["created_at"],
        updated_at=pull["updated_at"],
        merged_at=pull.get("merged_at"),
        head_sha=sha,
        last_commit_at=last_commit_at,
        failed_at=max(failed_at_values, default=None),
        checks=checks,
        failed_checks=failed,
        approved="APPROVED" in states and "CHANGES_REQUESTED" not in states,
        changes_requested="CHANGES_REQUESTED" in states,
        last_human_review_at=human_review_at or None,
        review_threads=len(threads),
        unresolved_threads=len(unresolved),
        oldest_unresolved_at=oldest,
        dispatches=_dispatch_markers(gh.issue_comments(number)),
    )
    return row, check_rows


def collect(gh: GitHubClient, devin: DevinClient, now: datetime) -> Snapshot:
    since = now - LOOKBACK
    pulls: list[PullRow] = []
    checks: list[CheckRun] = []
    for pull in gh.pulls(since):
        if not pull["head"]["ref"].startswith(DEVIN_BRANCH_PREFIX):
            continue
        row, runs = collect_pull(gh, pull)
        pulls.append(row)
        checks.extend(runs)

    sessions = [
        SessionRow(
            session_id=s["session_id"],
            title=s.get("title"),
            status=s.get("status"),
            status_detail=s.get("status_detail"),
            origin=s.get("origin"),
            automation_id=s.get("automation_id"),
            created_at=s.get("created_at"),
            updated_at=s.get("updated_at"),
            acus_consumed=float(s.get("acus_consumed") or 0),
            url=s.get("url"),
            tags=list(s.get("tags") or []),
            pr_numbers=sorted(
                {
                    n
                    for pr in s.get("pull_requests") or []
                    for n in _pr_numbers(gh.repo, pr.get("pr_url", ""))
                }
            ),
            category=s.get("category"),
        )
        for s in devin.sessions(gh.repo, since)
    ]
    automations = []
    for a in devin.automations():
        last = a.get("last_invocation") or {}
        fired = last.get("fired_at")
        automations.append(
            AutomationRow(
                automation_id=a["automation_id"],
                name=a.get("name", ""),
                enabled=bool(a.get("enabled")),
                last_status=last.get("status"),
                last_fired_at=_iso(datetime.fromtimestamp(fired, tz=timezone.utc))
                if isinstance(fired, (int, float))
                else fired,
                event_types=[t.get("event_type", "") for t in a.get("triggers") or []],
            )
        )
    snapshot = Snapshot(
        collected_at=_iso(now) or "",
        repo=gh.repo,
        devin_api_enabled=devin.enabled,
        pulls=pulls,
        check_runs=checks,
        sessions=sessions,
        automations=automations,
        findings=[],
    )
    snapshot.findings = derive_findings(snapshot, now)
    return snapshot


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #
def _finding_key(kind: str, pr: PullRow, anchor: str) -> str:
    digest = hashlib.sha1(f"{kind}:{pr.number}:{anchor}".encode()).hexdigest()[:12]  # noqa: S324
    return f"{kind}-{pr.number}-{digest}"


def _session_active_since(
    sessions: list[SessionRow], pr: PullRow, since: datetime
) -> bool:
    for s in sessions:
        if pr.number in s.pr_numbers or f"#{pr.number}" in (s.title or ""):
            updated = _parse_ts(s.updated_at)
            status = (s.status or "").lower()
            if status in ACTIVE_SESSION_STATES:
                return True
            if updated and updated >= since and status not in FAILED_SESSION_STATES:
                return True
    return False


def _add_finding(
    findings: list[Finding],
    pr: PullRow,
    kind: str,
    anchor: str,
    detail: str,
    since: str | None,
) -> None:
    key = _finding_key(kind, pr, anchor)
    findings.append(
        Finding(
            key=key,
            kind=kind,
            pr_number=pr.number,
            pr_url=pr.url,
            branch=pr.branch,
            detail=detail,
            since=since,
            dispatched=key in {d.get("key") for d in pr.dispatches},
        )
    )


def derive_findings(snapshot: Snapshot, now: datetime) -> list[Finding]:
    findings: list[Finding] = []
    for pr in snapshot.pulls:
        if pr.state != "open" or pr.draft:
            continue
        add = functools.partial(_add_finding, findings, pr)
        last_commit = _parse_ts(pr.last_commit_at) or _parse_ts(pr.created_at) or now
        quiet_since = now - UNATTENDED_GRACE
        failed_at = _parse_ts(pr.failed_at)
        ci_since = max(last_commit, failed_at) if failed_at else last_commit

        if pr.checks == "failure" and ci_since <= quiet_since:
            if not _session_active_since(snapshot.sessions, pr, ci_since):
                add(
                    "ci-failed-unattended",
                    pr.head_sha,
                    f"failed checks: {', '.join(pr.failed_checks[:5])}",
                    pr.failed_at or pr.last_commit_at,
                )
        oldest = _parse_ts(pr.oldest_unresolved_at)
        if (
            pr.unresolved_threads
            and oldest
            and oldest <= quiet_since
            and last_commit < oldest
            and not _session_active_since(snapshot.sessions, pr, oldest)
        ):
            add(
                "review-unaddressed",
                pr.oldest_unresolved_at or "",
                f"{pr.unresolved_threads} unresolved human review thread(s)",
                pr.oldest_unresolved_at,
            )
        review_at = _parse_ts(pr.last_human_review_at)
        if (
            pr.changes_requested
            and review_at
            and review_at <= quiet_since
            and last_commit < review_at
            and not _session_active_since(snapshot.sessions, pr, review_at)
        ):
            add(
                "changes-requested-unaddressed",
                pr.last_human_review_at or "",
                "changes requested with no follow-up commit",
                pr.last_human_review_at,
            )
    return findings


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def dispatch_prompt(
    finding: Finding, repo: str, related: list[Finding] | None = None
) -> str:
    signals = related or [finding]
    signal_text = "\n".join(
        f"- {item.kind}: {item.detail} (since {item.since})" for item in signals
    )
    return (
        f"You were started by the periodic Devin observability job for @{repo} "
        f"(finding `{finding.key}`, kind `{finding.kind}`).\n\n"
        f"Pull request: {finding.pr_url} (branch `{finding.branch}`).\n"
        f"Signals detected for this PR:\n{signal_text}\n\n"
        "Tasks:\n"
        "1. Re-check the PR's CURRENT state first; stop with a short PR comment if the "
        "signal is already resolved, the PR is merged/closed, or another Devin session "
        "is actively working on it.\n"
        "2. For failed CI: reproduce the failing check(s) locally, fix the root "
        "cause on the same branch, push, and watch CI until green. Do not weaken "
        "or skip tests.\n"
        "3. For unresolved review threads / changes requested: address every human "
        "comment with code or a reasoned reply, reply in-thread, and resolve threads "
        "whose request you fulfilled.\n"
        "4. NEVER merge, approve, enable auto-merge or push to `master`; "
        "humans merge.\n"
        "5. Finish with one PR comment summarising what you changed and what remains."
    )


def dispatch(
    gh: GitHubClient,
    devin: DevinClient,
    findings: list[Finding],
    *,
    dry_run: bool,
    limit: int,
    max_acu: int,
) -> list[JsonDict]:
    results: list[JsonDict] = []
    priority = {
        "ci-failed-unattended": 0,
        "changes-requested-unaddressed": 1,
        "review-unaddressed": 2,
    }
    by_pr: dict[int, list[Finding]] = {}
    for finding in findings:
        by_pr.setdefault(finding.pr_number, []).append(finding)
    ordered = sorted(
        (f for f in findings if not f.dispatched),
        key=lambda f: (priority.get(f.kind, len(priority)), f.pr_number),
    )
    dispatched_prs: set[int] = set()
    for finding in ordered:
        if len(dispatched_prs) >= limit or finding.pr_number in dispatched_prs:
            continue
        record: JsonDict = {
            "key": finding.key,
            "kind": finding.kind,
            "pr": finding.pr_number,
        }
        dispatched_prs.add(finding.pr_number)
        if dry_run:
            record["status"] = "dry-run"
        else:
            marker = json.dumps({"key": finding.key, "kind": finding.kind})
            comment = gh.comment(
                finding.pr_number,
                f"<!-- {DISPATCH_MARKER} {marker} -->\n"
                "Devin observability: "
                f"`{finding.kind}` detected; a session is being started.",
            )
            try:
                session = devin.create_session(
                    prompt=dispatch_prompt(finding, gh.repo, by_pr[finding.pr_number]),
                    title=f"[obs] {finding.kind} on PR #{finding.pr_number}",
                    tags=[
                        DISPATCH_TAG,
                        f"finding:{finding.kind}",
                        f"pr:{finding.pr_number}",
                    ],
                    max_acu=max_acu,
                )
            except Exception as exc:
                gh.update_comment(
                    int(comment["id"]),
                    "Devin observability: "
                    f"dispatch failed ({exc}); the next run will retry.",
                )
                record.update({"status": "error", "error": str(exc)})
                results.append(record)
                continue
            record.update(
                {
                    "status": "dispatched",
                    "session_id": session.get("session_id"),
                    "session_url": session.get("url"),
                }
            )
            marker = json.dumps(
                {k: record[k] for k in ("key", "kind", "session_id", "session_url")}
            )
            gh.update_comment(
                int(comment["id"]),
                f"<!-- {DISPATCH_MARKER} {marker} -->\n"
                f"Devin observability: `{finding.kind}` detected ({finding.detail}); "
                f"started a session to address it: {record['session_url']}",
            )
        results.append(record)
    return results


# --------------------------------------------------------------------------- #
# Postgres sink (optional dependency)
# --------------------------------------------------------------------------- #
DDL = """
CREATE SCHEMA IF NOT EXISTS devin_obs;
CREATE TABLE IF NOT EXISTS devin_obs.snapshots (
  snapshot_id  BIGSERIAL PRIMARY KEY,
  collected_at TIMESTAMPTZ NOT NULL,
  repo         TEXT NOT NULL,
  source       TEXT NOT NULL,
  devin_api    BOOLEAN NOT NULL,
  pulls        INT NOT NULL,
  open_pulls   INT NOT NULL,
  failing_ci   INT NOT NULL,
  sessions     INT NOT NULL,
  acus_total   NUMERIC NOT NULL,
  findings     INT NOT NULL
);
CREATE TABLE IF NOT EXISTS devin_obs.pull_requests (
  number INT PRIMARY KEY, title TEXT, url TEXT, author TEXT, branch TEXT,
  state TEXT, draft BOOLEAN, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ,
  merged_at TIMESTAMPTZ, head_sha TEXT, last_commit_at TIMESTAMPTZ,
  failed_at TIMESTAMPTZ, checks TEXT, failed_checks TEXT[],
  approved BOOLEAN, changes_requested BOOLEAN,
  last_human_review_at TIMESTAMPTZ, review_threads INT, unresolved_threads INT,
  oldest_unresolved_at TIMESTAMPTZ, remediation TEXT, last_seen_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS devin_obs.pull_request_history (
  number INT, collected_at TIMESTAMPTZ, state TEXT, checks TEXT,
  approved BOOLEAN, unresolved_threads INT, remediation TEXT,
  PRIMARY KEY (number, collected_at)
);
CREATE TABLE IF NOT EXISTS devin_obs.check_runs (
  check_run_id BIGINT PRIMARY KEY, pr_number INT, head_sha TEXT, name TEXT,
  status TEXT, conclusion TEXT, started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
  url TEXT
);
CREATE TABLE IF NOT EXISTS devin_obs.sessions (
  session_id TEXT PRIMARY KEY, title TEXT, status TEXT, status_detail TEXT,
  origin TEXT, automation_id TEXT, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ,
  acus_consumed NUMERIC, url TEXT, tags TEXT[], pr_numbers INT[], category TEXT,
  last_seen_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS devin_obs.automations (
  automation_id TEXT PRIMARY KEY, name TEXT, enabled BOOLEAN, last_status TEXT,
  last_fired_at TIMESTAMPTZ, event_types TEXT[], last_seen_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS devin_obs.findings (
  key TEXT PRIMARY KEY, kind TEXT, pr_number INT, pr_url TEXT, branch TEXT,
  detail TEXT, since TIMESTAMPTZ, first_seen_at TIMESTAMPTZ, last_seen_at TIMESTAMPTZ,
  resolved_at TIMESTAMPTZ, dispatched BOOLEAN
);
CREATE TABLE IF NOT EXISTS devin_obs.dispatches (
  key TEXT, session_id TEXT, session_url TEXT, kind TEXT, pr_number INT,
  created_at TIMESTAMPTZ, comment_url TEXT, PRIMARY KEY (key, session_id)
);
"""


def remediation(pr: PullRow) -> str:
    """Delivery state stronger than CI: green CI + human approval / merge."""
    if pr.state == "merged":
        return "merged"
    if pr.state == "closed":
        return "closed"
    if pr.checks == "failure":
        return "failed-ci"
    if pr.checks == "success" and pr.approved:
        return "ready-to-merge"
    if pr.changes_requested or pr.unresolved_threads:
        return "awaiting-devin"
    if pr.checks == "success":
        return "awaiting-review"
    return "ci-pending"


def load_snapshot(database_url: str, snapshot: Snapshot, source: str) -> None:
    import psycopg2  # noqa: PLC0415  (optional dependency, only for the DB sink)
    from psycopg2.extras import execute_values  # noqa: PLC0415

    now = snapshot.collected_at
    conn = psycopg2.connect(database_url)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(
                """INSERT INTO devin_obs.snapshots
                   (collected_at, repo, source, devin_api, pulls, open_pulls,
                    failing_ci,
                    sessions, acus_total, findings)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    now,
                    snapshot.repo,
                    source,
                    snapshot.devin_api_enabled,
                    len(snapshot.pulls),
                    sum(p.state == "open" for p in snapshot.pulls),
                    sum(
                        p.checks == "failure" and p.state == "open"
                        for p in snapshot.pulls
                    ),
                    len(snapshot.sessions),
                    round(sum(s.acus_consumed for s in snapshot.sessions), 2),
                    len(snapshot.findings),
                ),
            )
            for pr in snapshot.pulls:
                rem = remediation(pr)
                cur.execute(
                    """INSERT INTO devin_obs.pull_requests VALUES
                       (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (number) DO UPDATE SET
                         title=EXCLUDED.title, state=EXCLUDED.state,
                         draft=EXCLUDED.draft,
                         updated_at=EXCLUDED.updated_at, merged_at=EXCLUDED.merged_at,
                         head_sha=EXCLUDED.head_sha,
                         last_commit_at=EXCLUDED.last_commit_at,
                         failed_at=EXCLUDED.failed_at,
                         checks=EXCLUDED.checks, failed_checks=EXCLUDED.failed_checks,
                         approved=EXCLUDED.approved,
                         changes_requested=EXCLUDED.changes_requested,
                         last_human_review_at=EXCLUDED.last_human_review_at,
                         review_threads=EXCLUDED.review_threads,
                         unresolved_threads=EXCLUDED.unresolved_threads,
                         oldest_unresolved_at=EXCLUDED.oldest_unresolved_at,
                         remediation=EXCLUDED.remediation,
                         last_seen_at=EXCLUDED.last_seen_at""",
                    (
                        pr.number,
                        pr.title,
                        pr.url,
                        pr.author,
                        pr.branch,
                        pr.state,
                        pr.draft,
                        pr.created_at,
                        pr.updated_at,
                        pr.merged_at,
                        pr.head_sha,
                        pr.last_commit_at,
                        pr.failed_at,
                        pr.checks,
                        pr.failed_checks,
                        pr.approved,
                        pr.changes_requested,
                        pr.last_human_review_at,
                        pr.review_threads,
                        pr.unresolved_threads,
                        pr.oldest_unresolved_at,
                        rem,
                        now,
                    ),
                )
                cur.execute(
                    """INSERT INTO devin_obs.pull_request_history
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING""",
                    (
                        pr.number,
                        now,
                        pr.state,
                        pr.checks,
                        pr.approved,
                        pr.unresolved_threads,
                        rem,
                    ),
                )
                for d in pr.dispatches:
                    cur.execute(
                        """INSERT INTO devin_obs.dispatches
                           VALUES (%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT DO NOTHING""",
                        (
                            d.get("key"),
                            d.get("session_id") or "",
                            d.get("session_url"),
                            d.get("kind"),
                            pr.number,
                            d.get("created_at"),
                            d.get("comment_url"),
                        ),
                    )
            execute_values(
                cur,
                """INSERT INTO devin_obs.check_runs VALUES %s
                   ON CONFLICT (check_run_id) DO UPDATE SET status=EXCLUDED.status,
                     conclusion=EXCLUDED.conclusion,
                     completed_at=EXCLUDED.completed_at""",
                [
                    (
                        c.check_run_id,
                        c.pr_number,
                        c.head_sha,
                        c.name,
                        c.status,
                        c.conclusion,
                        c.started_at,
                        c.completed_at,
                        c.url,
                    )
                    for c in snapshot.check_runs
                ],
            )
            for s in snapshot.sessions:
                cur.execute(
                    """INSERT INTO devin_obs.sessions VALUES
                       (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (session_id) DO UPDATE SET title=EXCLUDED.title,
                         status=EXCLUDED.status, status_detail=EXCLUDED.status_detail,
                         updated_at=EXCLUDED.updated_at,
                         acus_consumed=EXCLUDED.acus_consumed,
                         tags=EXCLUDED.tags, pr_numbers=EXCLUDED.pr_numbers,
                         category=EXCLUDED.category,
                         last_seen_at=EXCLUDED.last_seen_at""",
                    (
                        s.session_id,
                        s.title,
                        s.status,
                        s.status_detail,
                        s.origin,
                        s.automation_id,
                        s.created_at,
                        s.updated_at,
                        s.acus_consumed,
                        s.url,
                        s.tags,
                        s.pr_numbers,
                        s.category,
                        now,
                    ),
                )
            for a in snapshot.automations:
                cur.execute(
                    """INSERT INTO devin_obs.automations VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (automation_id) DO UPDATE SET name=EXCLUDED.name,
                         enabled=EXCLUDED.enabled, last_status=EXCLUDED.last_status,
                         last_fired_at=EXCLUDED.last_fired_at,
                         event_types=EXCLUDED.event_types,
                         last_seen_at=EXCLUDED.last_seen_at""",
                    (
                        a.automation_id,
                        a.name,
                        a.enabled,
                        a.last_status,
                        a.last_fired_at,
                        a.event_types,
                        now,
                    ),
                )
            open_keys = [f.key for f in snapshot.findings]
            for f in snapshot.findings:
                cur.execute(
                    """INSERT INTO devin_obs.findings VALUES
                       (%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s)
                       ON CONFLICT (key) DO UPDATE SET detail=EXCLUDED.detail,
                         last_seen_at=EXCLUDED.last_seen_at, resolved_at=NULL,
                         dispatched=EXCLUDED.dispatched""",
                    (
                        f.key,
                        f.kind,
                        f.pr_number,
                        f.pr_url,
                        f.branch,
                        f.detail,
                        f.since,
                        now,
                        now,
                        f.dispatched,
                    ),
                )
            cur.execute(
                """UPDATE devin_obs.findings SET resolved_at=%s
                   WHERE resolved_at IS NULL AND NOT (key = ANY(%s))""",
                (now, open_keys),
            )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _snapshot_from_json(data: JsonDict) -> Snapshot:
    return Snapshot(
        collected_at=data["collected_at"],
        repo=data["repo"],
        devin_api_enabled=data["devin_api_enabled"],
        pulls=[PullRow(**p) for p in data["pulls"]],
        check_runs=[CheckRun(**c) for c in data["check_runs"]],
        sessions=[SessionRow(**s) for s in data["sessions"]],
        automations=[AutomationRow(**a) for a in data["automations"]],
        findings=[Finding(**f) for f in data["findings"]],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "findings"):
        p = sub.add_parser(name)
        p.add_argument("--json", help="write result to this file")
        p.add_argument(
            "--database-url", default=os.environ.get("DEVIN_OBS_DATABASE_URL")
        )
        p.add_argument("--source", default=os.environ.get("DEVIN_OBS_SOURCE", "cli"))
    p = sub.add_parser("dispatch")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max", type=int, default=3)
    p.add_argument("--max-acu", type=int, default=15)
    p.add_argument("--json", help="write dispatch records to this file")
    p = sub.add_parser("load")
    p.add_argument("snapshot")
    p.add_argument("--database-url", default=os.environ.get("DEVIN_OBS_DATABASE_URL"))
    p.add_argument("--source", default=os.environ.get("DEVIN_OBS_SOURCE", "artifact"))
    args = parser.parse_args(argv)

    repo = os.environ.get("GITHUB_REPOSITORY", "lcpz/superset")
    gh = GitHubClient(os.environ.get("GITHUB_TOKEN", ""), repo)
    devin = DevinClient(
        os.environ.get("DEVIN_API_KEY", ""), os.environ.get("DEVIN_ORG_ID", "")
    )
    now = datetime.now(timezone.utc)

    if args.command == "load":
        with open(args.snapshot, encoding="utf-8") as fh:
            snapshot = _snapshot_from_json(json.load(fh))
        if not args.database_url:
            parser.error("--database-url / DEVIN_OBS_DATABASE_URL required for load")
        load_snapshot(args.database_url, snapshot, args.source)
        print(
            f"loaded snapshot {snapshot.collected_at} into "
            f"{args.database_url.split('@')[-1]}"
        )
        return 0

    snapshot = collect(gh, devin, now)

    if args.command == "dispatch":
        records = dispatch(
            gh,
            devin,
            snapshot.findings,
            dry_run=args.dry_run,
            limit=args.max,
            max_acu=args.max_acu,
        )
        output = json.dumps(records, indent=2)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                fh.write(output)
        print(output)
        return 0

    payload: Any = (
        asdict(snapshot)
        if args.command == "collect"
        else [asdict(f) for f in snapshot.findings]
    )
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    if args.database_url and args.command == "collect":
        load_snapshot(args.database_url, snapshot, args.source)
    summary = {
        "collected_at": snapshot.collected_at,
        "devin_api": snapshot.devin_api_enabled,
        "pulls": len(snapshot.pulls),
        "open": sum(p.state == "open" for p in snapshot.pulls),
        "failing_ci": sum(
            p.checks == "failure" and p.state == "open" for p in snapshot.pulls
        ),
        "sessions": len(snapshot.sessions),
        "automations": len(snapshot.automations),
        "findings": [
            f"{f.kind}#{f.pr_number}{' (dispatched)' if f.dispatched else ''}"
            for f in snapshot.findings
        ],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
