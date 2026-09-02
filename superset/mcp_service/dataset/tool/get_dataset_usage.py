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
Get dataset usage (reverse lineage) FastMCP tool.
"""

import logging
from datetime import datetime, timezone

from fastmcp import Context
from superset_core.mcp.decorators import tool, ToolAnnotations

from superset import is_feature_enabled
from superset.extensions import event_logger
from superset.mcp_service.dataset.schemas import (
    DatasetError,
    DatasetUsageResponse,
    GetDatasetUsageRequest,
)
from superset.mcp_service.utils.audit_access import (
    AuditAccessError,
    resolve_audit_entity,
)

logger = logging.getLogger(__name__)


@tool(
    tags=["discovery", "audit"],
    class_permission_name="Dataset",
    annotations=ToolAnnotations(
        title="Get dataset usage",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
async def get_dataset_usage(
    request: GetDatasetUsageRequest, ctx: Context
) -> DatasetUsageResponse | DatasetError:
    """List the charts and dashboards that depend on a dataset (reverse lineage).

    Use this before retiring or migrating a dataset to inventory its
    dependents. Returns the same objects as
    ``GET /api/v1/dataset/<uuid>/related_objects`` so humans and agents can
    cross-check each other.

    SCOPE AND HONESTY MARKERS:
    - Only TABLE-backed charts (``datasource_type == "table"``) are found;
      dashboards are those containing at least one such chart. Saved queries,
      SQL Lab history and reports are NOT inventoried.
    - Objects you cannot access are silently dropped; ``count`` is the total
      you are allowed to see, ``truncated`` tells you whether more pages exist.
      Always page until ``truncated`` is false before concluding "no more".
    - ``soft_delete_filtered`` reports whether soft-deleted charts/dashboards
      were excluded (SOFT_DELETE feature flag on) or whether deletes are hard.

    Access: requires read access to the dataset itself (same fail-closed
    gate as the versions/activity endpoints), plus per-object chart/dashboard
    access.

    Example:
    ```json
    {"dataset_uuid": "a1b2c3d4-...", "page": 0, "page_size": 50}
    ```
    """
    from superset.connectors.sqla.models import SqlaTable
    from superset.datasets.related_objects import get_dataset_related_objects

    await ctx.info("Resolving dataset usage: dataset_uuid=%s" % request.dataset_uuid)
    try:
        dataset, _ = resolve_audit_entity(SqlaTable, request.dataset_uuid)
    except AuditAccessError as exc:
        await ctx.warning("Dataset usage blocked: %s" % exc.message)
        return DatasetError.create(error=exc.message, error_type=exc.error_type)

    try:
        with event_logger.log_context(action="mcp.get_dataset_usage.lookup"):
            data = get_dataset_related_objects(
                dataset, page=request.page, page_size=request.page_size
            )
        response = DatasetUsageResponse(
            dataset_id=dataset.id,
            dataset_uuid=str(dataset.uuid),
            table_name=dataset.table_name,
            soft_delete_filtered=is_feature_enabled("SOFT_DELETE"),
            charts=data["charts"],
            dashboards=data["dashboards"],
        )
        await ctx.info(
            "Dataset usage resolved: charts=%s dashboards=%s truncated=%s/%s"
            % (
                response.charts.count,
                response.dashboards.count,
                response.charts.truncated,
                response.dashboards.truncated,
            )
        )
        return response
    except Exception as e:
        await ctx.error("Dataset usage lookup failed: %s" % e)
        return DatasetError(
            error=f"Failed to get dataset usage: {str(e)}",
            error_type="InternalError",
            timestamp=datetime.now(timezone.utc),
        )
