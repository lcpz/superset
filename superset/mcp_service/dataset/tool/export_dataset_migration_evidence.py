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
"""MCP tool: export a reviewable, SHA-256 digested dataset-migration
evidence bundle (same body as ``GET /api/v1/dataset/<uuid>/migration_evidence/``).
"""

import logging

from fastmcp import Context
from superset_core.mcp.decorators import tool, ToolAnnotations

from superset.extensions import event_logger
from superset.mcp_service.dataset.schemas import (
    DatasetError,
    DatasetMigrationEvidenceResponse,
    ExportDatasetMigrationEvidenceRequest,
)
from superset.mcp_service.utils.audit_access import (
    AuditAccessError,
    resolve_audit_entity,
)

logger = logging.getLogger(__name__)


@tool(
    tags=["audit", "versioning", "export"],
    class_permission_name="Dataset",
    annotations=ToolAnnotations(
        title="Export dataset migration evidence",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
async def export_dataset_migration_evidence(
    request: ExportDatasetMigrationEvidenceRequest, ctx: Context
) -> DatasetMigrationEvidenceResponse | DatasetError:
    """Export one bounded page of dataset-migration evidence with a SHA-256 digest.

    For a dataset UUID and a time window this bundles, from the same sources
    as the REST audit endpoints:
    - inventory: one page of dependent TABLE-backed charts and their dashboards
      (get_dataset_usage semantics, access-filtered before counting)
    - assets[]: for the dataset and each inventoried asset, the versions
      issued in the window, the `before` snapshot (last version before `since`)
      and `after` snapshot (last version at/before `until`), each addressed by
      pruning-stable `version_uuid` + `transaction_id`, plus its own activity
      records (who/when)
    - report_executions: ReportExecutionLog rows for reports/alerts on those
      charts/dashboards (scoped like the report logs API)
    - query_executions: SQL Lab queries on the dataset's database mentioning
      the table name — HEURISTIC, disclosed in `matching`; a hard correlation
      id is future work (PR 6)
    - coverage: retention disclosure, `complete` flag, caveats

    INTEGRITY: `digest.value` = sha256 over the canonical `evidence` dict with
    sorted keys (`superset.utils.hashing.hash_from_dict(evidence,
    algorithm="sha256")`). Inputs are bounded (page_size, record_limit) BEFORE
    hashing and never truncated afterwards; if the response is too large the
    call fails outright — lower page_size/record_limit or narrow the window.
    `generated_at` sits outside the digest. This tool is excluded from MCP
    response caching so the bundle always reflects live state.

    HONEST LIMIT: the digest proves this bundle was not altered after export.
    It does not prove the live database was truthful, and anything older than
    `coverage.retention.history_begins_at` may have been pruned.

    Access: read access to the dataset (fail-closed), then per-object chart/
    dashboard access; report/query rows are scoped by their own filters.

    Example:
    ```json
    {"dataset_uuid": "a1b2c3d4-...", "since": "2026-08-01T00:00:00Z",
     "until": "2026-09-01T00:00:00Z", "page": 0, "page_size": 10}
    ```
    """
    from superset.connectors.sqla.models import SqlaTable
    from superset.versioning.evidence import (
        evidence_response_payload,
        EvidenceParamsError,
        parse_evidence_query_params,
    )

    await ctx.info(
        "Exporting migration evidence: dataset_uuid=%s page=%s"
        % (request.dataset_uuid, request.page)
    )
    try:
        dataset, _ = resolve_audit_entity(SqlaTable, request.dataset_uuid)
    except AuditAccessError as exc:
        await ctx.warning("Evidence export blocked: %s" % exc.message)
        return DatasetError.create(error=exc.message, error_type=exc.error_type)

    try:
        params = parse_evidence_query_params(
            {
                "since": request.since,
                "until": request.until,
                "page": request.page,
                "page_size": request.page_size,
                "record_limit": request.record_limit,
            }
        )
        with event_logger.log_context(
            action="mcp.export_dataset_migration_evidence.build"
        ):
            payload = evidence_response_payload(dataset, params)
    except EvidenceParamsError as exc:
        return DatasetError.create(error=str(exc), error_type="InvalidParams")
    except Exception as e:
        await ctx.error("Evidence export failed: %s" % e)
        return DatasetError.create(
            error=f"Failed to export migration evidence: {str(e)}",
            error_type="InternalError",
        )

    await ctx.info(
        "Evidence exported: assets=%s complete=%s digest=%s"
        % (
            len(payload["evidence"]["assets"]),
            payload["evidence"]["coverage"]["complete"],
            payload["digest"]["value"][:12],
        )
    )
    return DatasetMigrationEvidenceResponse(**payload)
