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
"""Tests for the export_dataset_migration_evidence MCP tool."""

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

TOOL = "superset.mcp_service.dataset.tool.export_dataset_migration_evidence"
EVIDENCE = "superset.versioning.evidence"
DATASET_UUID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture(autouse=True)
def mock_auth():
    with patch("superset.mcp_service.auth.get_user_from_request") as mock_get_user:
        user = Mock()
        user.id = 1
        user.username = "admin"
        mock_get_user.return_value = user
        yield


async def _call(payload: dict[str, Any]) -> dict[str, Any]:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "export_dataset_migration_evidence", {"request": payload}
        )
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_returns_rest_equivalent_payload_and_forwards_bounds() -> None:
    dataset = SimpleNamespace(id=1, uuid=UUID(DATASET_UUID), table_name="orders")
    evidence = {"assets": [{"kind": "dataset"}], "coverage": {"complete": True}}
    with (
        patch(f"{TOOL}.resolve_audit_entity", return_value=(dataset, dataset.uuid)),
        patch(
            f"{EVIDENCE}.build_dataset_migration_evidence", return_value=evidence
        ) as build,
    ):
        data = await _call(
            {
                "dataset_uuid": DATASET_UUID,
                "since": "2026-08-01T00:00:00Z",
                "page_size": 5,
                "record_limit": 50,
            }
        )

    assert build.call_args.kwargs == {
        "since": datetime(2026, 8, 1),
        "until": None,
        "page": 0,
        "page_size": 5,
        "record_limit": 50,
    }
    assert data["evidence"] == evidence
    assert data["digest"]["algorithm"] == "sha256"
    assert len(data["digest"]["value"]) == 64
    assert "generated_at" in data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [(400, "InvalidUuid"), (403, "AccessDenied"), (404, "NotFound")],
)
async def test_fails_closed_on_dataset_access(status: int, error_type: str) -> None:
    with patch(
        f"{TOOL}.resolve_audit_entity", side_effect=AuditAccessError(status, "nope")
    ) as resolve:
        data = await _call({"dataset_uuid": DATASET_UUID})
    assert data["error_type"] == error_type
    assert resolve.call_args.args[1] == DATASET_UUID


@pytest.mark.asyncio
async def test_malformed_window_is_invalid_params() -> None:
    dataset = SimpleNamespace(id=1, uuid=UUID(DATASET_UUID), table_name="orders")
    with patch(f"{TOOL}.resolve_audit_entity", return_value=(dataset, dataset.uuid)):
        data = await _call({"dataset_uuid": DATASET_UUID, "since": "yesterday"})
    assert data["error_type"] == "InvalidParams"


def test_tool_is_uncached_not_truncatable_and_guest_denied() -> None:
    from superset.mcp_service.mcp_config import (
        MCP_CACHE_CONFIG,
        MCP_GUEST_ALLOWED_TOOLS,
    )
    from superset.mcp_service.utils.token_utils import DATA_QUERY_TOOLS, INFO_TOOLS

    name = "export_dataset_migration_evidence"
    assert name in MCP_CACHE_CONFIG["excluded_tools"]
    assert name not in INFO_TOOLS
    assert name not in DATA_QUERY_TOOLS
    assert name not in MCP_GUEST_ALLOWED_TOOLS
