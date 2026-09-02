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
"""Tests for the dataset-migration evidence bundle and its digest."""

from __future__ import annotations

import hashlib
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from superset.utils import json

MOD = "superset.versioning.evidence"

DATASET_UUID = UUID("11111111-2222-4333-8444-555555555555")
CHART_UUID = UUID("22222222-2222-4333-8444-555555555555")
SINCE = datetime(2026, 8, 1)
UNTIL = datetime(2026, 9, 1)

RETENTION = {
    "version_history_days": 30,
    "pruning_enabled": True,
    "history_begins_at": "2026-08-03T00:00:00",
}


def _dataset() -> Any:
    return SimpleNamespace(
        id=1,
        uuid=DATASET_UUID,
        table_name="orders",
        schema="public",
        database_id=3,
    )


def _chart() -> SimpleNamespace:
    return SimpleNamespace(id=10, uuid=CHART_UUID, slice_name="Orders by day")


def _version(n: int, issued_at: datetime) -> dict[str, Any]:
    return {
        "version_uuid": UUID(int=n),
        "version_number": n,
        "transaction_id": 100 + n,
        "operation_type": "update",
        "issued_at": issued_at,
        "changed_by": None,
        "changes": [],
    }


VERSIONS = [
    _version(0, datetime(2026, 7, 1)),  # before window
    _version(1, datetime(2026, 7, 20)),  # before window (latest -> "before")
    _version(2, datetime(2026, 8, 10)),  # in window
    _version(3, datetime(2026, 8, 20)),  # in window (latest -> "after")
    _version(4, datetime(2026, 9, 5)),  # after window
]


def _fake_get_version(
    model_cls: Any, entity_uuid: UUID, version_uuid: UUID, entity: Any
) -> dict[str, Any] | None:
    n = version_uuid.int
    if n == 1:
        return None  # pruned
    return {
        "name": f"state-{n}",
        "_version": _version(n, VERSIONS[n]["issued_at"]),
    }


def _fake_get_activity(model_cls: Any, uuid: UUID, **kwargs: Any) -> Any:
    record = {
        "version_uuid": str(UUID(int=3)),
        "entity_kind": "chart",
        "entity_uuid": str(uuid),
        "entity_id": 10,
        "entity_name": "x",
        "transaction_id": 103,
        "issued_at": datetime(2026, 8, 20),
        "changed_by": None,
        "action_kind": "update",
        "field": "name",
        "from_value": "a",
        "to_value": "b",
    }
    return [record], 1, False


class _FakeQuery:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def filter(self, *args: Any, **kwargs: Any) -> _FakeQuery:
        return self

    join = order_by = filter

    def limit(self, n: int) -> _FakeQuery:
        return _FakeQuery(self._rows[:n])

    def all(self) -> list[Any]:
        return self._rows


@pytest.fixture
def evidence_env() -> Any:
    chart = _chart()
    inventory = {
        "charts": {
            "count": 1,
            "result": [
                {"id": 10, "uuid": str(CHART_UUID), "slice_name": "x", "viz_type": "t"}
            ],
            "truncated": False,
            "page": 0,
            "page_size": 10,
        },
        "dashboards": {
            "count": 0,
            "result": [],
            "truncated": False,
            "page": 0,
            "page_size": 10,
        },
    }
    session_rows: dict[str, list[Any]] = {"Slice": [chart], "Dashboard": []}

    def fake_query(*entities: Any) -> _FakeQuery:
        if (name := getattr(entities[0], "__name__", "")) in session_rows:
            return _FakeQuery(session_rows[name])
        return _FakeQuery([])  # ReportExecutionLog / Query

    with (
        patch(f"{MOD}.get_dataset_related_objects", return_value=inventory),
        patch(f"{MOD}.VersionDAO.list_versions", return_value=list(VERSIONS)),
        patch(f"{MOD}.VersionDAO.get_version", side_effect=_fake_get_version),
        patch(f"{MOD}.get_activity", side_effect=_fake_get_activity),
        patch(f"{MOD}.retention_disclosure", return_value=RETENTION),
        patch(f"{MOD}.db") as db,
        patch(f"{MOD}.ReportExecutionLogFilter") as rlf,
        patch(f"{MOD}.QueryFilter") as qf,
        patch(f"{MOD}.SQLAInterface"),
    ):
        db.session.query.side_effect = fake_query
        db.or_ = lambda *a: a
        rlf.return_value.apply.side_effect = lambda q, v: q
        qf.return_value.apply.side_effect = lambda q, v: q
        yield


def _build(**overrides: Any) -> dict[str, Any]:
    from superset.versioning.evidence import build_dataset_migration_evidence

    kwargs: dict[str, Any] = {"since": SINCE, "until": UNTIL}
    kwargs.update(overrides)
    return build_dataset_migration_evidence(_dataset(), **kwargs)


@pytest.mark.usefixtures("evidence_env")
def test_before_after_snapshots_addressed_by_stable_handles() -> None:
    evidence = _build()
    dataset_asset, chart_asset = evidence["assets"]

    assert dataset_asset["kind"] == "dataset"
    assert chart_asset == {
        **chart_asset,
        "kind": "chart",
        "id": 10,
        "uuid": str(CHART_UUID),
        "name": "Orders by day",
    }
    # window filtering uses issued_at, not positional version_number
    assert [
        v["transaction_id"] for v in chart_asset["versions_in_window"]["result"]
    ] == [
        102,
        103,
    ]
    assert chart_asset["versions_in_window"]["count"] == 2
    assert chart_asset["versions_in_window"]["truncated"] is False

    before, after = chart_asset["before"], chart_asset["after"]
    # version 1 was pruned: the handle is still reported, the state is not
    assert before["version_uuid"] == str(UUID(int=1))
    assert before["transaction_id"] == 101
    assert before["state"] is None
    assert before["unavailable_reason"] == "pruned_or_missing"
    assert after["version_uuid"] == str(UUID(int=3))
    assert after["transaction_id"] == 103
    assert after["state"] == {"name": "state-3"}
    assert after["version"]["version_uuid"] == str(UUID(int=3))
    assert "version_number" not in {"version_uuid", "transaction_id"} & set(before)

    assert chart_asset["activity"]["count"] == 1
    assert chart_asset["activity"]["result"][0]["transaction_id"] == 103
    assert evidence["coverage"]["retention"] == RETENTION
    assert evidence["coverage"]["complete"] is True
    assert evidence["query_executions"]["matching"].startswith("heuristic")


@pytest.mark.usefixtures("evidence_env")
def test_no_since_means_no_before_snapshot() -> None:
    evidence = _build(since=None, until=None)
    chart_asset = evidence["assets"][1]
    assert chart_asset["before"] is None
    assert chart_asset["after"]["transaction_id"] == 104  # latest overall
    assert chart_asset["versions_in_window"]["count"] == 5


@pytest.mark.usefixtures("evidence_env")
def test_record_limit_bounds_before_hashing_and_marks_incomplete() -> None:
    evidence = _build(record_limit=1)
    chart_asset = evidence["assets"][1]
    assert len(chart_asset["versions_in_window"]["result"]) == 1
    assert chart_asset["versions_in_window"]["count"] == 2
    assert chart_asset["versions_in_window"]["truncated"] is True
    assert evidence["coverage"]["complete"] is False


@pytest.mark.usefixtures("evidence_env")
def test_digest_is_deterministic_sha256_over_canonical_dict() -> None:
    from superset.versioning.evidence import digest_evidence, evidence_response_payload

    params: dict[str, Any] = {"since": SINCE, "until": UNTIL}
    first = evidence_response_payload(_dataset(), params)
    second = evidence_response_payload(_dataset(), params)

    assert first["digest"]["algorithm"] == "sha256"
    assert first["digest"]["covers"] == "evidence"
    assert first["digest"] == second["digest"]
    assert first["evidence"] == second["evidence"]
    # generated_at is outside the digest, so it may differ without changing it
    assert "generated_at" in first
    assert "generated_at" not in first["evidence"]

    # independently recomputable: sha256(json.dumps(evidence, sort_keys=True))
    expected = hashlib.sha256(
        json.dumps(first["evidence"], sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert first["digest"]["value"] == expected
    assert digest_evidence(first["evidence"])["value"] == expected

    # any change to the hashed content changes the digest
    tampered = json.loads(json.dumps(first["evidence"]))
    tampered["assets"][1]["after"]["state"]["name"] = "forged"
    assert digest_evidence(tampered)["value"] != expected


@pytest.mark.usefixtures("evidence_env")
def test_digest_ignores_configured_hash_algorithm() -> None:
    from superset.versioning.evidence import digest_evidence

    with patch("superset.utils.hashing.hash_from_str") as hfs:
        hfs.return_value = "x"
        digest_evidence({"a": 1})
    assert hfs.call_args.kwargs["algorithm"] == "sha256"


@pytest.mark.parametrize(
    "overrides",
    [
        {"page": -1},
        {"page_size": 0},
        {"page_size": 26},
        {"since": UNTIL, "until": SINCE},
    ],
)
@pytest.mark.usefixtures("evidence_env")
def test_rejects_unbounded_or_inverted_input(overrides: dict[str, Any]) -> None:
    from superset.versioning.evidence import EvidenceParamsError

    with pytest.raises(EvidenceParamsError, match="page|since"):
        _build(**overrides)


def test_parse_query_params_reuses_activity_datetime_parsing() -> None:
    from superset.versioning.evidence import (
        EvidenceParamsError,
        parse_evidence_query_params,
    )

    params = parse_evidence_query_params(
        {"since": "2026-08-01T00:00:00Z", "page_size": "5", "until": None}
    )
    assert params == {
        "since": datetime(2026, 8, 1),
        "until": None,
        "page": 0,
        "page_size": 5,
        "record_limit": 200,
    }
    with pytest.raises(EvidenceParamsError, match="since"):
        parse_evidence_query_params({"since": "yesterday"})
    with pytest.raises(EvidenceParamsError, match="page_size"):
        parse_evidence_query_params({"page_size": "many"})
