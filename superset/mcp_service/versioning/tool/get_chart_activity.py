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
"""Get chart activity stream FastMCP tool."""

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
    class_permission_name="Chart",
    annotations=ToolAnnotations(
        title="Get chart activity",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
async def get_chart_activity(
    request: AssetActivityRequest, ctx: Context
) -> AssetActivityResponse | AuditError:
    """Who changed what, and when, for a chart (and optionally linked assets).

    Same records as ``GET /api/v1/chart/<uuid>/activity/``: one row per
    field-level change with ``version_uuid``, ``entity_kind``, ``entity_uuid``,
    ``changed_by`` and ``issued_at``, so each row can be verified against
    ``get_<kind>_versions(version_uuid=...)``.

    ``include='related'`` adds edits to the dataset behind the chart;
    ``'all'`` is both.

    Records for entities you cannot access are silently dropped and ``count``
    reflects only visible rows.
    ``truncated`` means the fetch ceiling was hit — narrow ``since``/``until``.
    ``retention`` discloses the pruning cutoff; absence of records before
    ``history_begins_at`` is not evidence of inactivity.

    Access: requires read access to the chart itself (fail-closed gate shared
    with the REST endpoints). Not exposed to guest tokens.
    """
    from superset.models.slice import Slice

    return await activity_tool_body("chart", Slice, request, ctx)
