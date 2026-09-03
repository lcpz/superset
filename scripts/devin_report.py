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
Devin automation status board.

Joins GitHub (issues, labels, PRs, check runs) with the Devin v3 API (sessions,
automation) and prints a markdown report answering:

* is the automation active and healthy?
* which issues completed, failed, or remain blocked?
* did the agent finish, and did the remediation pass (CI green / merged)?
* portfolio progress, controlled replays, observed ACU consumption
* which evidence (URLs) supports each status

Environment:
    GITHUB_TOKEN        GitHub token with read access to the repository
    GITHUB_REPOSITORY   owner/repo (defaults to lcpz/superset)
    DEVIN_API_KEY       Devin v3 service-user key (ViewOrgSessions)
    DEVIN_ORG_ID        Devin organization id (org-...)
    DEVIN_AUTOMATION_ID Devin automation id (auto-...), optional

Usage:
    python scripts/devin_report.py [--output report.md] [--json state.json]

See .github/devin-observability.md for status definitions.
"""

from __future__ import annotations

import argparse
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
DEVIN_API = "https://api.devin.ai"

LABEL_READY = "devin:ready"
LABEL_IN_PROGRESS = "devin:in-progress"
LABEL_DONE = "devin:done"
LABEL_BLOCKED_PREFIX = "blocked-"
SESSION_TAG = "issue-automation"

STALE_IN_PROGRESS = timedelta(hours=6)
DISPATCH_GRACE = timedelta(minutes=15)
LOOKBACK = timedelta(days=int(os.environ.get("DEVIN_REPORT_LOOKBACK_DAYS", "90")))
AUTOMATION_AUTHORS = {"devin-ai-integration[bot]"}

JsonDict = dict[str, Any]


def _get_json(url: str, headers: dict[str, str]) -> Any:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url}")
    request = urllib.request.Request(url, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} -> {exc.code}: {body[:300]}") from exc


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitHubClient:
    """Minimal GitHub REST client using urllib only."""

    def __init__(self, token: str, repo: str) -> None:
        self.repo = repo
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def _paginate(self, path: str, params: dict[str, str] | None = None) -> list[Any]:
        params = {"per_page": "100", **(params or {})}
        page = 1
        results: list[Any] = []
        while True:
            query = urllib.parse.urlencode({**params, "page": str(page)})
            data = _get_json(f"{GITHUB_API}{path}?{query}", self.headers)
            if not data:
                break
            results.extend(data)
            if len(data) < 100:
                break
            page += 1
        return results

    def issues(self, since: datetime) -> list[JsonDict]:
        """Issues (not PRs) updated since ``since``; bounds the per-issue lookups."""
        issues = self._paginate(
            f"/repos/{self.repo}/issues",
            {"state": "all", "since": since.strftime("%Y-%m-%dT%H:%M:%SZ")},
        )
        return [issue for issue in issues if "pull_request" not in issue]

    def issue_events(self, number: int) -> list[JsonDict]:
        return self._paginate(f"/repos/{self.repo}/issues/{number}/events")

    def issue_comments(self, number: int) -> list[JsonDict]:
        return self._paginate(f"/repos/{self.repo}/issues/{number}/comments")

    def pull(self, number: int) -> JsonDict:
        return _get_json(f"{GITHUB_API}/repos/{self.repo}/pulls/{number}", self.headers)

    def check_runs(self, sha: str) -> list[JsonDict]:
        data = _get_json(
            f"{GITHUB_API}/repos/{self.repo}/commits/{sha}/check-runs?per_page=100",
            self.headers,
        )
        return data.get("check_runs", [])

    def reviews(self, number: int) -> list[JsonDict]:
        return self._paginate(f"/repos/{self.repo}/pulls/{number}/reviews")


class DevinClient:
    """Minimal Devin v3 client; all methods degrade to empty results if unset."""

    def __init__(self, api_key: str, org_id: str) -> None:
        self.enabled = bool(api_key and org_id)
        self.org_id = org_id
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def sessions(self, automation_id: str | None) -> list[JsonDict]:
        if not self.enabled:
            return []
        params: dict[str, Any] = {"first": "200"}
        if automation_id:
            params["automation_ids"] = automation_id
        else:
            params["tags"] = SESSION_TAG
        results: list[JsonDict] = []
        after: str | None = None
        while True:
            if after:
                params["after"] = after
            query = urllib.parse.urlencode(params, doseq=True)
            data = _get_json(
                f"{DEVIN_API}/v3/organizations/{self.org_id}/sessions?{query}",
                self.headers,
            )
            results.extend(data.get("items", []))
            if not data.get("has_next_page"):
                return results
            after = data.get("end_cursor")

    def automation(self, automation_id: str) -> JsonDict | None:
        if not (self.enabled and automation_id):
            return None
        return _get_json(
            f"{DEVIN_API}/v3/organizations/{self.org_id}/automations/{automation_id}",
            self.headers,
        )


@dataclass
class PullState:
    url: str
    number: int
    state: str  # open | merged | closed
    checks: str  # success | failure | pending | none
    approved: bool
    evidence: list[str] = field(default_factory=list)


@dataclass
class IssueRow:
    number: int
    title: str
    url: str
    labels: list[str]
    closed: bool
    status: str
    ready_at: datetime | None
    sessions: list[JsonDict] = field(default_factory=list)
    replays: int = 0
    acus: float = 0.0
    pull: PullState | None = None
    evidence: list[str] = field(default_factory=list)


def _ready_events(events: list[JsonDict]) -> list[datetime]:
    return sorted(
        ts
        for event in events
        if event.get("event") == "labeled"
        and (event.get("label") or {}).get("name") == LABEL_READY
        and (ts := _parse_ts(event.get("created_at"))) is not None
    )


def _md_cell(text: str) -> str:
    """Neutralise Markdown/HTML in untrusted text placed inside a table cell."""
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r"([\\`*_\[\]<>|~#!])", r"\\\1", text)
    return text[:120]


def _pr_numbers_from_text(repo: str, text: str) -> set[int]:
    numbers: set[int] = set()
    for match in re.finditer(
        rf"https://github\.com/{re.escape(repo)}/pull/(\d+)", text or ""
    ):
        numbers.add(int(match.group(1)))
    return numbers


def _pull_state(gh: GitHubClient, number: int) -> PullState:
    pull = gh.pull(number)
    state = "merged" if pull.get("merged_at") else pull.get("state", "open")
    runs = gh.check_runs(pull["head"]["sha"])
    if not runs:
        checks = "none"
    elif any(r.get("status") != "completed" for r in runs):
        checks = "pending"
    elif all(r.get("conclusion") in {"success", "skipped", "neutral"} for r in runs):
        checks = "success"
    else:
        checks = "failure"
    latest: dict[str, str] = {}
    for review in gh.reviews(number):
        if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[(review.get("user") or {}).get("login", "")] = review["state"]
    approved = "APPROVED" in latest.values() and "CHANGES_REQUESTED" not in (
        latest.values()
    )
    evidence = [pull["html_url"], f"{pull['html_url']}/checks"]
    return PullState(
        url=pull["html_url"],
        number=number,
        state=state,
        checks=checks,
        approved=approved,
        evidence=evidence,
    )


def _classify(row: IssueRow, now: datetime, sessions_known: bool = True) -> str:
    labels = set(row.labels)
    session_states = {s.get("status") for s in row.sessions}
    if any(label.startswith(LABEL_BLOCKED_PREFIX) for label in labels):
        return "blocked"
    if row.pull and row.pull.state == "merged":
        return "merged"
    if LABEL_DONE in labels:
        if row.pull and row.pull.checks == "failure":
            return "failed-ci"
        return "done"
    if LABEL_IN_PROGRESS in labels:
        return _classify_in_progress(row, session_states, now)
    if LABEL_READY in labels:
        return _classify_ready(row, now, sessions_known)
    if row.closed:
        return "closed"
    return "unlabeled"


def _classify_ready(row: IssueRow, now: datetime, sessions_known: bool) -> str:
    if row.sessions:
        return "finished-no-label"
    if not sessions_known:
        return "ready (session data unavailable)"
    if row.ready_at and now - row.ready_at > DISPATCH_GRACE:
        return "dispatch-missed"
    return "queued"


def _classify_in_progress(
    row: IssueRow, session_states: set[Any], now: datetime
) -> str:
    if session_states & {"working", "resumed"}:
        return "in-progress"
    stale = row.ready_at is not None and now - row.ready_at > STALE_IN_PROGRESS
    if session_states & {"blocked", "expired"} or stale:
        return "stalled"
    return "in-progress"


def build_rows(
    gh: GitHubClient,
    sessions: list[JsonDict],
    now: datetime,
    sessions_known: bool = True,
) -> list[IssueRow]:
    """Join issues with sessions; ``sessions_known`` is False when the Devin
    session list could not be fetched, so no status is inferred from its absence."""
    rows: list[IssueRow] = []
    for issue in gh.issues(since=now - LOOKBACK):
        number = issue["number"]
        ready = _ready_events(gh.issue_events(number))
        if not ready:
            continue
        row = IssueRow(
            number=number,
            title=issue["title"],
            url=issue["html_url"],
            labels=[label["name"] for label in issue.get("labels", [])],
            closed=issue.get("state") == "closed",
            status="",
            ready_at=ready[-1] if ready else None,
            replays=max(len(ready) - 1, 0),
        )
        pr_numbers: set[int] = set()
        for comment in gh.issue_comments(number):
            if (comment.get("user") or {}).get("login") in AUTOMATION_AUTHORS:
                pr_numbers |= _pr_numbers_from_text(gh.repo, comment.get("body", ""))
        row.sessions = [
            s for s in sessions if _session_matches(gh.repo, s, number, pr_numbers)
        ]
        for session in row.sessions:
            for pr in session.get("pull_requests") or []:
                pr_numbers |= _pr_numbers_from_text(gh.repo, pr.get("pr_url", ""))
        row.acus = round(
            sum(float(s.get("acus_consumed") or 0) for s in row.sessions), 2
        )
        row.evidence.append(row.url)
        row.evidence.extend(s["url"] for s in row.sessions if s.get("url"))
        if pr_numbers:
            row.pull = _pull_state(gh, max(pr_numbers))
            row.evidence.extend(row.pull.evidence)

        row.status = _classify(row, now, sessions_known)
        rows.append(row)
    return rows


def _session_matches(
    repo: str, session: JsonDict, issue_number: int, pr_numbers: set[int]
) -> bool:
    """A session belongs to an issue if its title names it or it opened its PR."""
    title = session.get("title") or ""
    if re.search(rf"(?<!\w)#{issue_number}(?!\d)", title):
        return True
    if re.search(rf"issues/{issue_number}(?!\d)", title):
        return True
    for pr in session.get("pull_requests") or []:
        if _pr_numbers_from_text(repo, pr.get("pr_url", "")) & pr_numbers:
            return True
    return False


def automation_health(
    automation: JsonDict | None, rows: list[IssueRow], devin_enabled: bool
) -> tuple[str, list[str]]:
    notes: list[str] = []
    if not devin_enabled:
        return "unknown (DEVIN_API_KEY not configured)", notes
    if automation is None:
        return "unknown (automation id not configured)", notes
    if not automation.get("enabled"):
        return "DISABLED", notes
    if last := automation.get("last_invocation") or {}:
        fired = datetime.fromtimestamp(last["fired_at"], tz=timezone.utc)
        notes.append(f"last invocation: {last['status']} at {fired.isoformat()}")
    missed = [r for r in rows if r.status == "dispatch-missed"]
    if missed:
        notes.append(
            "dispatch missed for: " + ", ".join(f"#{r.number}" for r in missed)
        )
        return "DEGRADED", notes
    return "healthy", notes


def render(
    repo: str,
    rows: list[IssueRow],
    health: str,
    health_notes: list[str],
    sessions: list[JsonDict],
    now: datetime,
) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    total_acus = round(sum(float(s.get("acus_consumed") or 0) for s in sessions), 2)
    terminal = sum(counts.get(s, 0) for s in ("done", "merged", "failed-ci"))
    progress = f"{terminal}/{len(rows)}" if rows else "0/0"

    lines = [
        "<!-- devin-status-board -->",
        f"## Devin status board — `{repo}`",
        f"_Generated {now.strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
        f"**Automation health:** {health}",
    ]
    lines.extend(f"- {note}" for note in health_notes)
    lines += [
        "",
        "### Portfolio",
        f"- Issues tracked: {len(rows)} · terminal (PR open/merged/failed-ci): "
        f"{progress}",
        "- By status: "
        + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"),
        f"- Controlled replays (re-labels): {sum(r.replays for r in rows)}",
        f"- Sessions observed: {len(sessions)} · ACUs consumed: {total_acus}",
        "",
        "### Issues",
        "| Issue | Status | Sessions | Replays | ACUs | PR | CI | Approved | "
        "Evidence |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in sorted(rows, key=lambda r: r.number):
        pr_cell = (
            f"[#{row.pull.number}]({row.pull.url}) ({row.pull.state})"
            if row.pull
            else "—"
        )
        ci_cell = row.pull.checks if row.pull else "—"
        approved_cell = ("yes" if row.pull.approved else "no") if row.pull else "—"
        evidence = " ".join(f"[{i + 1}]({u})" for i, u in enumerate(row.evidence))
        lines.append(
            f"| [#{row.number}]({row.url}) {_md_cell(row.title)} | **{row.status}** | "
            f"{len(row.sessions)} | {row.replays} | {row.acus} | {pr_cell} | "
            f"{ci_cell} | {approved_cell} | {evidence} |"
        )
    lines += [
        "",
        "Status definitions and replay procedure: `.github/devin-observability.md`.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="write markdown here (default: stdout)")
    parser.add_argument("--json", dest="json_path", help="also dump raw rows as JSON")
    args = parser.parse_args(argv)

    repo = os.environ.get("GITHUB_REPOSITORY", "lcpz/superset")
    gh = GitHubClient(os.environ.get("GITHUB_TOKEN", ""), repo)
    devin = DevinClient(
        os.environ.get("DEVIN_API_KEY", ""), os.environ.get("DEVIN_ORG_ID", "")
    )
    automation_id = os.environ.get("DEVIN_AUTOMATION_ID", "")
    now = datetime.now(tz=timezone.utc)

    devin_error = ""
    sessions: list[JsonDict] = []
    sessions_known = devin.enabled
    automation: JsonDict | None = None
    try:
        sessions = devin.sessions(automation_id or None)
    except (RuntimeError, urllib.error.URLError) as exc:
        # Report from GitHub evidence only; the health line explains the gap.
        print(f"::warning::Devin API unavailable: {exc}", file=sys.stderr)
        devin_error = str(exc)
        sessions_known = False
    else:
        try:
            automation = devin.automation(automation_id)
        except (RuntimeError, urllib.error.URLError) as exc:
            print(f"::warning::Devin automation lookup failed: {exc}", file=sys.stderr)
            devin_error = str(exc)
    rows = build_rows(gh, sessions, now, sessions_known)
    health, notes = automation_health(automation, rows, sessions_known)
    if devin_error:
        health = "unknown (Devin API error)"
        notes.append(
            f"Devin API: {_md_cell(devin_error.split(' -> ')[-1])} "
            "(check the service user's ViewOrgSessions permission)"
        )
    report = render(repo, rows, health, notes, sessions, now)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(report)
    else:
        sys.stdout.write(report)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump([asdict(r) for r in rows], handle, default=str, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
