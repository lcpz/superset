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
"""Tests for the get_dataset_usage MCP tool (dataset reverse lineage)."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch
from uuid import UUID

import pytest
from fastmcp import Client

from superset.mcp_service.app import mcp
from superset.mcp_service.utils.audit_access import AuditAccessError
from superset.utils import json

DATASET_UUID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
TOOL = "superset.mcp_service.dataset.tool.get_dataset_usage"


@pytest.fixture(autouse=True)
def mock_auth():
    with patch("superset.mcp_service.auth.get_user_from_request") as mock_get_user:
        user = Mock()
        user.id = 1
        user.username = "admin"
        mock_get_user.return_value = user
        yield


def _dataset() -> SimpleNamespace:
    return SimpleNamespace(id=42, uuid=DATASET_UUID, table_name="vehicle_sales")


def _related(n_charts: int, n_dashboards: int) -> dict[str, list[SimpleNamespace]]:
    return {
        "charts": [
            SimpleNamespace(id=i, uuid=None, slice_name=f"c{i}", viz_type="table")
            for i in range(n_charts)
        ],
        "dashboards": [
            SimpleNamespace(
                id=i, uuid=None, json_metadata=None, slug=None, dashboard_title=f"d{i}"
            )
            for i in range(n_dashboards)
        ],
    }


async def _call(payload: dict[str, Any]) -> dict[str, Any]:
    async with Client(mcp) as client:
        result = await client.call_tool("get_dataset_usage", {"request": payload})
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_usage_paginated_with_completeness_markers() -> None:
    with (
        patch(f"{TOOL}.resolve_audit_entity", return_value=(_dataset(), DATASET_UUID)),
        patch(
            "superset.datasets.related_objects.DatasetDAO.get_related_objects",
            return_value=_related(3, 1),
        ),
        patch(
            "superset.datasets.related_objects.security_manager.can_access_chart",
            return_value=True,
        ),
        patch(
            "superset.datasets.related_objects.security_manager.can_access_dashboard",
            return_value=True,
        ),
        patch(f"{TOOL}.is_feature_enabled", return_value=False),
    ):
        data = await _call({"dataset_uuid": str(DATASET_UUID), "page_size": 2})

    assert data["dataset_id"] == 42
    assert data["dataset_uuid"] == str(DATASET_UUID)
    assert data["scope"] == "table_backed_charts"
    assert data["soft_delete_filtered"] is False
    assert data["charts"]["count"] == 3
    assert data["charts"]["truncated"] is True
    assert [c["id"] for c in data["charts"]["result"]] == [0, 1]
    assert data["dashboards"]["count"] == 1
    assert data["dashboards"]["truncated"] is False
    assert data["dashboards"]["result"][0]["title"] == "d0"


@pytest.mark.asyncio
async def test_usage_drops_inaccessible_children_before_counting() -> None:
    with (
        patch(f"{TOOL}.resolve_audit_entity", return_value=(_dataset(), DATASET_UUID)),
        patch(
            "superset.datasets.related_objects.DatasetDAO.get_related_objects",
            return_value=_related(2, 2),
        ),
        patch(
            "superset.datasets.related_objects.security_manager.can_access_chart",
            return_value=False,
        ),
        patch(
            "superset.datasets.related_objects.security_manager.can_access_dashboard",
            side_effect=lambda d: d.id == 1,
        ),
        patch(f"{TOOL}.is_feature_enabled", return_value=True),
    ):
        data = await _call({"dataset_uuid": str(DATASET_UUID)})

    assert data["charts"]["count"] == 0
    assert data["dashboards"]["count"] == 1
    assert data["soft_delete_filtered"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [(400, "InvalidUuid"), (403, "AccessDenied"), (404, "NotFound")],
)
async def test_usage_fails_closed_on_access_errors(status: int, error_type: str):
    with patch(
        f"{TOOL}.resolve_audit_entity",
        side_effect=AuditAccessError(status, "nope"),
    ):
        data = await _call({"dataset_uuid": str(DATASET_UUID)})
    assert data["error_type"] == error_type


def test_usage_tool_not_guest_allowed() -> None:
    from superset.mcp_service.mcp_config import MCP_GUEST_ALLOWED_TOOLS

    assert "get_dataset_usage" not in MCP_GUEST_ALLOWED_TOOLS


def test_usage_tool_not_in_truncation_categories() -> None:
    from superset.mcp_service.utils.token_utils import DATA_QUERY_TOOLS, INFO_TOOLS

    assert "get_dataset_usage" not in INFO_TOOLS
    assert "get_dataset_usage" not in DATA_QUERY_TOOLS


def test_resolve_audit_entity_maps_rest_statuses(app) -> None:
    from superset.connectors.sqla.models import SqlaTable
    from superset.mcp_service.utils.audit_access import resolve_audit_entity

    with pytest.raises(AuditAccessError) as exc:
        resolve_audit_entity(SqlaTable, "not-a-uuid")
    assert exc.value.status == 400

    with (
        patch(
            "superset.versioning.api_helpers.VersionDAO.find_active_by_uuid",
            return_value=None,
        ),
        pytest.raises(AuditAccessError) as exc,
    ):
        resolve_audit_entity(SqlaTable, str(DATASET_UUID))
    assert exc.value.status == 404
