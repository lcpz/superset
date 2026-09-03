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
import importlib.util
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[3] / "scripts" / "devin_observability.py"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("devin_observability", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


obs = _load()


def _pr(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "number": 42,
        "title": "feat: thing",
        "url": "https://github.com/lcpz/superset/pull/42",
        "author": "devin-ai-integration[bot]",
        "branch": "devin/42-thing",
        "state": "open",
        "draft": False,
        "created_at": obs._iso(NOW - timedelta(days=1)),
        "updated_at": obs._iso(NOW),
        "merged_at": None,
        "head_sha": "abc123",
        "last_commit_at": obs._iso(NOW - timedelta(hours=5)),
        "failed_at": None,
        "last_devin_comment_at": None,
        "checks": "success",
        "failed_checks": [],
        "approved": False,
        "changes_requested": False,
        "last_human_review_at": None,
        "review_threads": 0,
        "unresolved_threads": 0,
        "oldest_unresolved_at": None,
    }
    base.update(overrides)
    return obs.PullRow(**base)


def _snapshot(pulls: list[Any], sessions: list[Any] | None = None) -> Any:
    return obs.Snapshot(
        collected_at=obs._iso(NOW),
        repo="lcpz/superset",
        devin_api_enabled=bool(sessions),
        pulls=pulls,
        check_runs=[],
        sessions=sessions or [],
        automations=[],
        findings=[],
    )


def test_summarise_checks_ignores_informational_and_flags_failures() -> None:
    runs = [
        {"name": "python-lint", "status": "completed", "conclusion": "success"},
        {"name": "actions-timeline", "status": "queued", "conclusion": None},
    ]
    assert obs._summarise_checks(runs) == ("success", [])
    runs.append({"name": "docs", "status": "completed", "conclusion": "failure"})
    assert obs._summarise_checks(runs) == ("failure", ["docs"])
    runs.append({"name": "e2e", "status": "in_progress", "conclusion": None})
    assert obs._summarise_checks(runs) == ("pending", [])
    assert obs._summarise_checks([]) == ("none", [])


def test_parse_epoch_timestamp_and_iso_round_trip() -> None:
    expected = datetime(2026, 9, 3, 11, 49, 11, tzinfo=timezone.utc)
    parsed = obs._parse_ts(1788436151)

    assert parsed == expected
    assert obs._parse_ts(obs._iso(parsed)) == expected


def test_session_row_normalizes_epoch_timestamps() -> None:
    row = obs._session_row(
        "lcpz/superset",
        {
            "session_id": "session-1",
            "created_at": 1788436151,
            "updated_at": 1788436152,
        },
    )

    assert row.created_at == "2026-09-03T11:49:11+00:00"
    assert row.updated_at == "2026-09-03T11:49:12+00:00"


def test_snapshot_from_json_normalizes_epoch_timestamps() -> None:
    snapshot = obs._snapshot_from_json(
        {
            "collected_at": "2026-09-03T12:00:00+00:00",
            "repo": "lcpz/superset",
            "devin_api_enabled": True,
            "pulls": [],
            "check_runs": [],
            "sessions": [
                {
                    "session_id": "session-1",
                    "title": None,
                    "status": None,
                    "status_detail": None,
                    "origin": None,
                    "automation_id": None,
                    "created_at": 1788436151,
                    "updated_at": 1788436152,
                    "acus_consumed": 0.0,
                    "url": None,
                    "tags": [],
                    "pr_numbers": [14, 15],
                    "category": None,
                }
            ],
            "automations": [],
            "findings": [],
        }
    )

    assert snapshot.sessions[0].created_at == "2026-09-03T11:49:11+00:00"
    assert snapshot.sessions[0].updated_at == "2026-09-03T11:49:12+00:00"
    assert snapshot.sessions[0].pr_numbers == [14, 15]


def test_dispatch_markers_are_parsed_from_hidden_comments() -> None:
    marker = {"key": "k1", "kind": "ci-failed-unattended", "session_id": "s"}
    comments = [
        {"body": "plain comment", "html_url": "u0", "created_at": "t0"},
        {
            "body": f"<!-- {obs.DISPATCH_MARKER} {obs.json.dumps(marker)} -->\ntext",
            "html_url": "u1",
            "created_at": "t1",
        },
        {"body": f"<!-- {obs.DISPATCH_MARKER} not-json -->", "html_url": "u2"},
    ]
    parsed = obs._dispatch_markers(comments)
    assert len(parsed) == 1
    assert parsed[0]["key"] == "k1"
    assert parsed[0]["comment_url"] == "u1"


def test_failed_ci_without_session_is_a_finding_once() -> None:
    pr = _pr(checks="failure", failed_checks=["python-lint"])
    findings = obs.derive_findings(_snapshot([pr]), NOW)
    assert [f.kind for f in findings] == ["ci-failed-unattended"]
    assert findings[0].pr_number == 42
    assert not findings[0].dispatched

    pr.dispatches = [{"key": findings[0].key, "session_id": "s1"}]
    again = obs.derive_findings(_snapshot([pr]), NOW)
    assert again[0].dispatched


def test_recent_commit_or_active_session_suppresses_ci_finding() -> None:
    fresh = _pr(checks="failure", last_commit_at=obs._iso(NOW - timedelta(minutes=10)))
    assert obs.derive_findings(_snapshot([fresh]), NOW) == []

    stale = _pr(checks="failure")
    session = obs.SessionRow(
        session_id="s1",
        title="Fix CI on PR #42",
        status="running",
        status_detail=None,
        origin="automation",
        automation_id="auto-1",
        created_at=obs._iso(NOW),
        updated_at=obs._iso(NOW),
        acus_consumed=1.0,
        url="https://app.devin.ai/sessions/s1",
        tags=[],
        pr_numbers=[42],
        category="fix",
    )
    assert obs.derive_findings(_snapshot([stale], [session]), NOW) == []


def test_review_findings_require_no_follow_up_commit() -> None:
    review_at = obs._iso(NOW - timedelta(hours=4))
    pr = _pr(
        unresolved_threads=2,
        oldest_unresolved_at=review_at,
        changes_requested=True,
        last_human_review_at=review_at,
        last_commit_at=obs._iso(NOW - timedelta(hours=6)),
    )
    kinds = sorted(f.kind for f in obs.derive_findings(_snapshot([pr]), NOW))
    assert kinds == ["changes-requested-unaddressed", "review-unaddressed"]

    pr.last_commit_at = obs._iso(NOW - timedelta(hours=3))
    assert obs.derive_findings(_snapshot([pr]), NOW) == []

    closed = _pr(state="merged", unresolved_threads=1, oldest_unresolved_at=review_at)
    assert obs.derive_findings(_snapshot([closed]), NOW) == []


def test_failed_check_age_not_commit_age() -> None:
    recent_failure = _pr(
        checks="failure",
        failed_checks=["python-lint"],
        failed_at=obs._iso(NOW - timedelta(minutes=10)),
    )
    assert obs.derive_findings(_snapshot([recent_failure]), NOW) == []

    old_failure = _pr(
        checks="failure",
        failed_checks=["python-lint"],
        failed_at=obs._iso(NOW - timedelta(hours=5)),
    )
    assert [f.kind for f in obs.derive_findings(_snapshot([old_failure]), NOW)] == [
        "ci-failed-unattended"
    ]


def test_session_state_controls_suppression() -> None:
    pr = _pr(checks="failure")
    for status, expected in [
        ("working", []),
        ("failed", ["ci-failed-unattended"]),
        ("finished", []),
    ]:
        session = obs.SessionRow(
            session_id="s1",
            title="Fix CI on PR #42",
            status=status,
            status_detail=None,
            origin="automation",
            automation_id="auto-1",
            created_at=obs._iso(NOW),
            updated_at=obs._iso(NOW),
            acus_consumed=1.0,
            url="https://app.devin.ai/sessions/s1",
            tags=[],
            pr_numbers=[42],
            category="fix",
        )
        assert [
            f.kind for f in obs.derive_findings(_snapshot([pr], [session]), NOW)
        ] == expected


def test_remediation_classification() -> None:
    assert obs.remediation(_pr(state="merged")) == "merged"
    assert obs.remediation(_pr(state="closed")) == "closed"
    assert obs.remediation(_pr(checks="failure")) == "failed-ci"
    assert obs.remediation(_pr(approved=True)) == "ready-to-merge"
    assert obs.remediation(_pr(unresolved_threads=1)) == "awaiting-devin"
    assert obs.remediation(_pr()) == "awaiting-review"
    assert obs.remediation(_pr(checks="pending")) == "ci-pending"


def test_dispatch_creates_session_and_idempotency_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request(
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, url, body))
        if url.endswith("/sessions"):
            return {
                "session_id": "devin-xyz",
                "url": "https://app.devin.ai/sessions/xyz",
            }
        if method == "PATCH":
            return {"html_url": "https://github.com/c/1"}
        return {"id": 1, "html_url": "https://github.com/c/1"}

    monkeypatch.setattr(obs, "_request", fake_request)
    monkeypatch.setenv("DEVIN_CREATE_AS_USER_ID", "user-1")
    gh = obs.GitHubClient("ghtoken", "lcpz/superset")
    devin = obs.DevinClient("key", "org-1")
    finding = obs.Finding(
        key="ci-failed-unattended-42-deadbeef0000",
        kind="ci-failed-unattended",
        pr_number=42,
        pr_url="https://github.com/lcpz/superset/pull/42",
        branch="devin/42-thing",
        detail="failed checks: python-lint",
        since=obs._iso(NOW),
    )
    done = obs.Finding(**{**finding.__dict__, "key": "other", "dispatched": True})

    results = obs.dispatch(
        gh, devin, [done, finding], dry_run=False, limit=3, max_acu=10
    )

    assert [r["status"] for r in results] == ["dispatched"]
    assert results[0]["session_id"] == "devin-xyz"
    method, url, body = calls[1]
    assert (method, url) == ("POST", f"{obs.DEVIN_API}/v3/organizations/org-1/sessions")
    assert body is not None
    assert body["max_acu_limit"] == 10
    assert body["create_as_user_id"] == "user-1"
    assert obs.DISPATCH_TAG in body["tags"]
    assert "NEVER merge" in body["prompt"]
    comment_method, comment_url, comment_body = calls[0]
    assert (comment_method, comment_url) == (
        "POST",
        f"{obs.GITHUB_API}/repos/lcpz/superset/issues/42/comments",
    )
    assert comment_body is not None
    assert f"<!-- {obs.DISPATCH_MARKER}" in comment_body["body"]
    assert obs._dispatch_markers([comment_body])[0]["key"] == finding.key
    assert calls[2][0] == "PATCH"


def test_dry_run_and_disabled_client_never_call_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network call attempted")

    monkeypatch.setattr(obs, "_request", boom)
    gh = obs.GitHubClient("t", "lcpz/superset")
    disabled = obs.DevinClient("", "")
    finding = obs.Finding("k", "ci-failed-unattended", 1, "u", "devin/1", "d", None)

    assert obs.dispatch(gh, disabled, [finding], dry_run=True, limit=1, max_acu=5) == [
        {"key": "k", "kind": "ci-failed-unattended", "pr": 1, "status": "dry-run"}
    ]
    assert disabled.sessions("lcpz/superset", NOW) == []
    assert disabled.automations() == []
    with pytest.raises(RuntimeError, match="not configured"):
        disabled.create_session("p", "t", [], 1)


def test_deleted_review_author_is_counted_as_human(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGitHub:
        def __init__(self) -> None:
            self.comments_calls = 0

        def check_runs(self, sha: str) -> list[dict[str, Any]]:
            return []

        def reviews(self, number: int) -> list[dict[str, Any]]:
            return []

        def review_threads(self, number: int) -> list[dict[str, Any]]:
            return [
                {
                    "isResolved": False,
                    "isOutdated": False,
                    "comments": {"nodes": [{"author": None, "createdAt": "t"}]},
                }
            ]

        def commits(self, number: int) -> list[dict[str, Any]]:
            return []

        def issue_comments(self, number: int) -> list[dict[str, Any]]:
            self.comments_calls += 1
            return [
                {
                    "body": f"<!-- {obs.DISPATCH_MARKER} "
                    f"{obs.json.dumps({'key': 'marker'})} -->",
                    "created_at": "2026-09-02T00:00:00+00:00",
                    "html_url": "https://github.com/c/1",
                    "user": {"login": "devin-ai-integration[bot]"},
                }
            ]

    github = FakeGitHub()
    row, _ = obs.collect_pull(
        github,
        {
            "number": 42,
            "title": "feat",
            "html_url": "https://github.com/lcpz/superset/pull/42",
            "user": {"login": "human"},
            "head": {"ref": "devin/42-feat", "sha": "abc"},
            "state": "open",
            "draft": False,
            "created_at": "2026-09-01T00:00:00+00:00",
            "updated_at": "2026-09-01T00:00:00+00:00",
            "merged_at": None,
        },
    )
    assert row.unresolved_threads == 1
    assert row.last_devin_comment_at is None
    assert github.comments_calls == 1


def test_dispatch_failed_session_clears_marker_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request(
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, url, body))
        if method == "POST" and url.endswith("/comments"):
            return {"id": 1}
        if method == "PATCH":
            return {}
        raise RuntimeError("create failed")

    monkeypatch.setattr(obs, "_request", fake_request)
    gh = obs.GitHubClient("t", "lcpz/superset")
    devin = obs.DevinClient("key", "org-1")
    finding = obs.Finding("k", "ci-failed-unattended", 1, "u", "devin/1", "d", None)
    results = obs.dispatch(gh, devin, [finding], dry_run=False, limit=1, max_acu=5)
    assert results[0]["status"] == "error"
    assert [call[0] for call in calls] == ["POST", "POST", "PATCH"]
    assert obs.DISPATCH_MARKER not in (calls[2][2] or {}).get("body", "")


def test_one_session_per_pr_includes_all_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request(
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, url, body))
        if method == "POST" and url.endswith("/sessions"):
            return {"session_id": "s1", "url": "https://app.devin.ai/sessions/s1"}
        if method == "POST":
            return {"id": 7}
        return {}

    monkeypatch.setattr(obs, "_request", fake_request)
    gh = obs.GitHubClient("t", "lcpz/superset")
    devin = obs.DevinClient("key", "org-1")
    findings = [
        obs.Finding(
            "review",
            "review-unaddressed",
            42,
            "u",
            "devin/42",
            "unresolved threads",
            None,
        ),
        obs.Finding(
            "changes",
            "changes-requested-unaddressed",
            42,
            "u",
            "devin/42",
            "changes requested",
            None,
        ),
    ]
    results = obs.dispatch(gh, devin, findings, dry_run=False, limit=3, max_acu=5)
    assert len(results) == 1
    assert results[0]["status"] == "dispatched"
    session_body = calls[1][2]
    assert session_body is not None
    assert "review-unaddressed" in session_body["prompt"]
    assert "changes-requested-unaddressed" in session_body["prompt"]


def test_provisional_dispatch_marker_expires_but_completed_marker_does_not() -> None:
    pr = _pr(checks="failure", failed_checks=["python-lint"])
    finding = obs.derive_findings(_snapshot([pr]), NOW)[0]

    pr.dispatches = [
        {
            "key": finding.key,
            "created_at": obs._iso(NOW - timedelta(minutes=10)),
        }
    ]
    assert obs.derive_findings(_snapshot([pr]), NOW)[0].dispatched

    pr.dispatches[0]["created_at"] = obs._iso(NOW - timedelta(hours=5))
    assert not obs.derive_findings(_snapshot([pr]), NOW)[0].dispatched

    pr.dispatches[0] = {"key": finding.key, "session_id": "session-1"}
    assert obs.derive_findings(_snapshot([pr]), NOW)[0].dispatched


def test_recent_devin_progress_comment_suppresses_finding() -> None:
    recent = _pr(
        checks="failure",
        failed_checks=["python-lint"],
        last_devin_comment_at=obs._iso(NOW - timedelta(hours=1)),
    )
    assert obs.derive_findings(_snapshot([recent]), NOW) == []

    old = _pr(
        checks="failure",
        failed_checks=["python-lint"],
        last_devin_comment_at=obs._iso(NOW - timedelta(hours=6)),
    )
    assert [f.kind for f in obs.derive_findings(_snapshot([old]), NOW)] == [
        "ci-failed-unattended"
    ]


def test_old_snapshot_defaults_new_pull_activity_fields() -> None:
    pull = asdict(_pr())
    pull.pop("failed_at")
    pull.pop("last_devin_comment_at")
    snapshot = obs._snapshot_from_json(
        {
            "collected_at": obs._iso(NOW),
            "repo": "lcpz/superset",
            "devin_api_enabled": False,
            "pulls": [pull],
            "check_runs": [],
            "sessions": [],
            "automations": [],
            "findings": [],
        }
    )
    assert snapshot.pulls[0].failed_at is None
    assert snapshot.pulls[0].last_devin_comment_at is None


def test_pull_request_insert_and_schema_support_activity_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.executed: list[tuple[str, Any]] = []

        def __enter__(self) -> "FakeCursor":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def execute(self, query: str, params: Any = None) -> None:
            self.executed.append((query, params))

    class FakeConnection:
        def __init__(self, cursor: FakeCursor) -> None:
            self.cursor_instance = cursor

        def __enter__(self) -> "FakeConnection":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def cursor(self) -> FakeCursor:
            return self.cursor_instance

        def close(self) -> None:
            return None

    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    psycopg2 = ModuleType("psycopg2")
    psycopg2.connect = lambda database_url: connection  # type: ignore[attr-defined]
    extras = ModuleType("psycopg2.extras")
    extras.execute_values = lambda *args: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg2", psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", extras)

    obs.load_snapshot("postgresql://unused", _snapshot([_pr()]), "test")

    assert "ADD COLUMN IF NOT EXISTS last_devin_comment_at TIMESTAMPTZ" in obs.DDL
    pull_insert = next(
        query
        for query, _ in cursor.executed
        if "INSERT INTO devin_obs.pull_requests" in query
    )
    assert "last_devin_comment_at" in pull_insert.split("VALUES", 1)[0]
