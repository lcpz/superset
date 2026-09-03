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
"""Tests for ``scripts/devin_report.py``.

The script is not installed as a package, so it is loaded via importlib from
its filesystem path. The units pinned here cover the degraded paths added when
the Devin API is partially unavailable: when the session list cannot be
fetched no status must be inferred from its absence (``dispatch-missed`` /
``stalled`` become ``... (session data unavailable)``), and health must stay
``unknown`` rather than ``healthy`` when the automation record is unreadable.
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "devin_report.py"
_spec = importlib.util.spec_from_file_location("devin_report", _SCRIPT_PATH)
assert _spec is not None, f"Could not load {_SCRIPT_PATH}"
assert _spec.loader is not None, f"No loader on spec for {_SCRIPT_PATH}"
devin_report = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = devin_report
_spec.loader.exec_module(devin_report)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _row(**kwargs: object) -> object:
    defaults: dict[str, object] = {
        "number": 1,
        "title": "t",
        "url": "https://github.com/o/r/issues/1",
        "labels": [],
        "closed": False,
        "status": "",
        "ready_at": None,
    }
    defaults.update(kwargs)
    return devin_report.IssueRow(**defaults)


# --- _classify_ready ------------------------------------------------------


def test_ready_overdue_is_dispatch_missed_when_sessions_known() -> None:
    row = _row(labels=["devin:ready"], ready_at=NOW - timedelta(hours=1))
    assert devin_report._classify_ready(row, NOW, True) == "dispatch-missed"


def test_ready_overdue_is_unavailable_when_sessions_unknown() -> None:
    row = _row(labels=["devin:ready"], ready_at=NOW - timedelta(hours=1))
    assert (
        devin_report._classify_ready(row, NOW, False)
        == "ready (session data unavailable)"
    )


def test_ready_recent_is_queued() -> None:
    row = _row(labels=["devin:ready"], ready_at=NOW - timedelta(minutes=1))
    assert devin_report._classify_ready(row, NOW, True) == "queued"


# --- _classify_in_progress ------------------------------------------------


def test_in_progress_stale_is_stalled_when_sessions_known() -> None:
    row = _row(labels=["devin:in-progress"], ready_at=NOW - timedelta(hours=7))
    assert devin_report._classify_in_progress(row, set(), NOW, True) == "stalled"


def test_in_progress_stale_is_unavailable_when_sessions_unknown() -> None:
    row = _row(labels=["devin:in-progress"], ready_at=NOW - timedelta(hours=7))
    assert (
        devin_report._classify_in_progress(row, set(), NOW, False)
        == "in-progress (session data unavailable)"
    )


def test_in_progress_working_session_beats_unknown_flag() -> None:
    row = _row(labels=["devin:in-progress"], ready_at=NOW - timedelta(hours=7))
    assert (
        devin_report._classify_in_progress(row, {"working"}, NOW, False)
        == "in-progress"
    )


# --- automation_health ----------------------------------------------------


def test_health_unknown_when_devin_disabled() -> None:
    health, _ = devin_report.automation_health(None, [], False)
    assert health == "unknown (DEVIN_API_KEY not configured)"


def test_health_disabled_requires_the_record() -> None:
    health, _ = devin_report.automation_health({"enabled": False}, [], True)
    assert health == "DISABLED"


def test_health_healthy_requires_the_record() -> None:
    health, _ = devin_report.automation_health({"enabled": True}, [], True)
    assert health == "healthy"


def test_health_unknown_when_record_unreadable() -> None:
    health, notes = devin_report.automation_health(None, [], True)
    assert health == "unknown (automation record not readable)"
    assert any("could not be verified" in note for note in notes)


def test_health_degraded_even_without_record() -> None:
    row = _row(status="dispatch-missed")
    health, _ = devin_report.automation_health(None, [row], True)
    assert health == "DEGRADED"


# --- main() fallback ------------------------------------------------------


class _StubGitHub:
    repo = "o/r"

    def issues(self, since: datetime) -> list[dict[str, object]]:
        return []


class _StubDevin:
    def __init__(self, enabled: bool, sessions_exc: Exception | None) -> None:
        self.enabled = enabled
        self._sessions_exc = sessions_exc

    def sessions(self, automation_id: str | None) -> list[dict[str, object]]:
        if self._sessions_exc is not None:
            raise self._sessions_exc
        return []

    def automation(self, automation_id: str) -> dict[str, object] | None:
        return {"enabled": True}


@pytest.mark.parametrize(
    "exc, expect_permission_hint",
    [
        (RuntimeError("GET https://api.devin.ai/... -> 403: nope"), True),
        (RuntimeError("GET https://api.devin.ai/... -> 500: boom"), False),
        (urllib.error.URLError("connection refused"), False),
    ],
)
def test_main_degrades_on_session_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exc: Exception,
    expect_permission_hint: bool,
) -> None:
    monkeypatch.setattr(devin_report, "GitHubClient", lambda *a, **k: _StubGitHub())
    monkeypatch.setattr(
        devin_report,
        "DevinClient",
        lambda *a, **k: _StubDevin(enabled=True, sessions_exc=exc),
    )
    assert devin_report.main([]) == 0
    out = capsys.readouterr()
    assert "unknown (Devin API error)" in out.out
    hinted = "ViewOrgSessions" in out.out
    assert hinted is expect_permission_hint


def test_main_publishes_board_when_devin_healthy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(devin_report, "GitHubClient", lambda *a, **k: _StubGitHub())
    monkeypatch.setattr(
        devin_report,
        "DevinClient",
        lambda *a, **k: _StubDevin(enabled=True, sessions_exc=None),
    )
    assert devin_report.main([]) == 0
    out = capsys.readouterr()
    assert "healthy" in out.out
    assert "Devin API error" not in out.out
