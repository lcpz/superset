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
"""Get dataset activity stream FastMCP tool."""

from fastmcp import Context
from superset_core.mcp.decorators import tool, ToolAnnotations

from superset.mcp_service.versioning.schemas import (
    AssetActivityRequest,
    AssetActivityResponse,
    AuditError,
)
from superset.mcp_service.versioning.service import activity_tool_body


@tool(
    tags=["audit", "versioning"],
    class_permission_name="Dataset",
    annotations=ToolAnnotations(
        title="Get dataset activity",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
async def get_dataset_activity(
    request: AssetActivityRequest, ctx: Context
) -> AssetActivityResponse | AuditError:
    """Who changed what, and when, for a dataset (and optionally linked assets).

    Same records as ``GET /api/v1/dataset/<uuid>/activity/``: one row per
    field-level change with ``version_uuid``, ``entity_kind``, ``entity_uuid``,
    ``changed_by`` and ``issued_at``, so each row can be verified against
    ``get_<kind>_versions(version_uuid=...)``.

    Datasets have NO related layer: ``include='related'`` always returns
    an empty page and ``'all'`` reduces to the dataset's own edits (use
    ``get_dataset_usage`` + per-chart activity to audit dependents).

    Records for entities you cannot access are silently dropped and ``count``
    reflects only visible rows.
    ``truncated`` means the fetch ceiling was hit — narrow ``since``/``until``.
    ``retention`` discloses the pruning cutoff; absence of records before
    ``history_begins_at`` is not evidence of inactivity.

    Access: requires read access to the dataset itself (fail-closed gate shared
    with the REST endpoints). Not exposed to guest tokens.
    """
    from superset.connectors.sqla.models import SqlaTable

    return await activity_tool_body("dataset", SqlaTable, request, ctx)
