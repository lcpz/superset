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
"""Tests for the per-asset get_<asset>_versions / get_<asset>_activity tools."""

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch
from uuid import UUID

import pytest
from fastmcp import Client

from superset.mcp_service.app import mcp
from superset.mcp_service.utils.audit_access import AuditAccessError
from superset.utils import json

SVC = "superset.mcp_service.versioning.service"
ENTITY_UUID = UUID("11111111-2222-4333-8444-555555555555")
VERSION_UUID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")

RETENTION = {
    "version_history_days": 30,
    "pruning_enabled": True,
    "history_begins_at": "2026-08-03T00:00:00",
}


@pytest.fixture(autouse=True)
def mock_auth():
    with patch("superset.mcp_service.auth.get_user_from_request") as mock_get_user:
        user = Mock()
        user.id = 1
        user.username = "admin"
        mock_get_user.return_value = user
        yield


@pytest.fixture(autouse=True)
def retention():
    with patch(f"{SVC}.retention_disclosure", return_value=RETENTION):
        yield


def _entity(kind: str) -> SimpleNamespace:
    attrs = {"chart": "slice_name", "dashboard": "dashboard_title"}
    return SimpleNamespace(
        id=7, uuid=ENTITY_UUID, **{attrs.get(kind, "table_name"): f"my {kind}"}
    )


def _version_row(n: int) -> dict[str, Any]:
    return {
        "version_uuid": UUID(int=n),
        "version_number": n,
        "transaction_id": 100 + n,
        "operation_type": "update",
        "issued_at": datetime(2026, 9, 1, 12, n),
        "changed_by": None,
        "changes": [],
    }


async def _call(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with Client(mcp) as client:
        result = await client.call_tool(tool, {"request": payload})
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["chart", "dashboard", "dataset"])
async def test_versions_list_is_paginated_with_retention(kind: str) -> None:
    with (
        patch(f"{SVC}.resolve_audit_entity", return_value=(_entity(kind), ENTITY_UUID)),
        patch(
            f"{SVC}.VersionDAO.list_versions",
            return_value=[_version_row(n) for n in range(5)],
        ),
    ):
        data = await _call(
            f"get_{kind}_versions",
            {"uuid": str(ENTITY_UUID), "page": 1, "page_size": 2},
        )

    assert data["asset"] == {
        "kind": kind,
        "id": 7,
        "uuid": str(ENTITY_UUID),
        "name": f"my {kind}",
    }
    assert data["count"] == 5
    assert data["truncated"] is True
    assert data["page"] == 1
    assert [r["transaction_id"] for r in data["result"]] == [102, 103]
    assert data["result"][0]["version_uuid"] == str(UUID(int=2))
    assert data["retention"] == RETENTION


@pytest.mark.asyncio
async def test_version_snapshot_by_version_uuid() -> None:
    snapshot = {
        "slice_name": "old name",
        "_version": _version_row(3),
    }
    with (
        patch(
            f"{SVC}.resolve_audit_entity",
            return_value=(_entity("chart"), ENTITY_UUID),
        ),
        patch(f"{SVC}.VersionDAO.get_version", return_value=snapshot) as get_version,
    ):
        data = await _call(
            "get_chart_versions",
            {"uuid": str(ENTITY_UUID), "version_uuid": str(VERSION_UUID)},
        )

    assert get_version.call_args.args[2] == VERSION_UUID
    assert data["version_uuid"] == str(VERSION_UUID)
    assert data["transaction_id"] == 103
    assert data["snapshot"]["slice_name"] == "old name"
    assert data["snapshot"]["_version"]["version_uuid"] == str(UUID(int=3))
    assert data["retention"] == RETENTION


@pytest.mark.asyncio
async def test_version_snapshot_missing_mentions_pruning() -> None:
    with (
        patch(
            f"{SVC}.resolve_audit_entity",
            return_value=(_entity("dashboard"), ENTITY_UUID),
        ),
        patch(f"{SVC}.VersionDAO.get_version", return_value=None),
    ):
        data = await _call(
            "get_dashboard_versions",
            {"uuid": str(ENTITY_UUID), "version_uuid": str(VERSION_UUID)},
        )
    assert data["error_type"] == "NotFound"
    assert "pruned" in data["message"]


@pytest.mark.asyncio
async def test_version_snapshot_invalid_version_uuid() -> None:
    with patch(
        f"{SVC}.resolve_audit_entity", return_value=(_entity("chart"), ENTITY_UUID)
    ):
        data = await _call(
            "get_chart_versions", {"uuid": str(ENTITY_UUID), "version_uuid": "nope"}
        )
    assert data["error_type"] == "InvalidUuid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool",
    [
        "get_chart_versions",
        "get_dashboard_versions",
        "get_dataset_versions",
        "get_chart_activity",
        "get_dashboard_activity",
        "get_dataset_activity",
    ],
)
@pytest.mark.parametrize(
    ("status", "error_type"),
    [(400, "InvalidUuid"), (403, "AccessDenied"), (404, "NotFound")],
)
async def test_tools_fail_closed(tool: str, status: int, error_type: str) -> None:
    with patch(
        f"{SVC}.resolve_audit_entity", side_effect=AuditAccessError(status, "nope")
    ):
        data = await _call(tool, {"uuid": str(ENTITY_UUID)})
    assert data["error_type"] == error_type


@pytest.mark.asyncio
async def test_activity_reuses_rest_param_parsing_and_marks_completeness() -> None:
    record = {
        "version_uuid": str(UUID(int=9)),
        "entity_kind": "chart",
        "entity_uuid": str(ENTITY_UUID),
        "entity_id": 7,
        "entity_name": "my chart",
        "transaction_id": 109,
        "issued_at": datetime(2026, 9, 1, 12, 0),
        "changed_by": None,
        "action_kind": "update",
        "field": "slice_name",
        "from_value": "a",
        "to_value": "b",
    }
    with (
        patch(
            f"{SVC}.resolve_audit_entity",
            return_value=(_entity("dashboard"), ENTITY_UUID),
        ),
        patch(f"{SVC}.get_activity", return_value=([record], 41, True)) as activity,
    ):
        data = await _call(
            "get_dashboard_activity",
            {
                "uuid": str(ENTITY_UUID),
                "include": "related",
                "since": "2026-08-01T00:00:00Z",
                "page": 2,
                "page_size": 20,
                "q": "name",
            },
        )

    kwargs = activity.call_args.kwargs
    assert kwargs["include"] == "related"
    assert kwargs["since"] == datetime(2026, 8, 1)  # Z suffix -> naive UTC
    assert kwargs["page"] == 2
    assert kwargs["page_size"] == 20
    assert kwargs["q"] == "name"
    assert kwargs["resolved_entity"].id == 7

    assert data["include"] == "related"
    assert data["count"] == 41
    assert data["truncated"] is True
    assert data["page"] == 2
    assert data["result"][0]["version_uuid"] == str(UUID(int=9))
    assert data["result"][0]["issued_at"].startswith("2026-09-01T12:00:00")
    assert data["retention"] == RETENTION


@pytest.mark.asyncio
async def test_activity_rejects_malformed_since() -> None:
    with patch(
        f"{SVC}.resolve_audit_entity", return_value=(_entity("chart"), ENTITY_UUID)
    ):
        data = await _call(
            "get_chart_activity", {"uuid": str(ENTITY_UUID), "since": "yesterday"}
        )
    assert data["error_type"] == "InvalidParams"


def test_dataset_activity_docstring_discloses_no_related_layer() -> None:
    from superset.mcp_service.versioning.tool import get_dataset_activity

    doc = get_dataset_activity.__doc__ or ""
    assert "NO related layer" in doc


def test_tools_default_deny_for_guests_and_not_truncatable() -> None:
    from superset.mcp_service.mcp_config import MCP_GUEST_ALLOWED_TOOLS
    from superset.mcp_service.utils.token_utils import DATA_QUERY_TOOLS, INFO_TOOLS
    from superset.mcp_service.versioning.tool import __all__ as tools

    for name in tools:
        assert name not in MCP_GUEST_ALLOWED_TOOLS
        assert name not in INFO_TOOLS
        assert name not in DATA_QUERY_TOOLS
