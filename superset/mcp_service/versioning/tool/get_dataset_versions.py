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
"""Get dataset version history FastMCP tool."""

from fastmcp import Context
from superset_core.mcp.decorators import tool, ToolAnnotations

from superset.mcp_service.versioning.schemas import (
    AssetVersionSnapshotResponse,
    AssetVersionsRequest,
    AssetVersionsResponse,
    AuditError,
)
from superset.mcp_service.versioning.service import versions_tool_body


@tool(
    tags=["audit", "versioning"],
    class_permission_name="Dataset",
    annotations=ToolAnnotations(
        title="Get dataset versions",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
async def get_dataset_versions(
    request: AssetVersionsRequest, ctx: Context
) -> AssetVersionsResponse | AssetVersionSnapshotResponse | AuditError:
    """Version history of a dataset, or one full version snapshot.

    Same entries as ``GET /api/v1/dataset/<uuid>/versions/`` and
    ``.../versions/<version_uuid>/`` so humans and agents can cross-check.

    - Without ``version_uuid``: a page of version rows (``version_uuid``,
      ``transaction_id``, ``issued_at``, ``changed_by``, field-level
      ``changes``), with ``count`` / ``truncated`` completeness markers.
    - With ``version_uuid``: the dataset's state at that version plus the
      ``_version`` block. Address versions by ``version_uuid`` /
      ``transaction_id`` — ``version_number`` is positional and shifts once
      older versions are pruned.
    - ``retention`` discloses the pruning cutoff: an absent version before
      ``history_begins_at`` is not evidence that nothing happened.

    Access: requires read access to the dataset itself (fail-closed gate
    shared with the REST endpoints). Not exposed to guest tokens.
    """
    from superset.connectors.sqla.models import SqlaTable

    return await versions_tool_body("dataset", SqlaTable, request, ctx)
