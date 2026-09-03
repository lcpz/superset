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
"""Shared bodies for the per-asset version/activity MCP tools.

Each function mirrors one REST endpoint body in
:mod:`superset.versioning.api_helpers` / :mod:`superset.versioning.activity`
and reuses the same DAO calls, marshmallow schemas, query-param parsing and
fail-closed access gate, so the MCP and REST surfaces cannot drift apart.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastmcp import Context
from flask_appbuilder import Model

from superset.daos.version import VersionDAO
from superset.extensions import event_logger
from superset.mcp_service.utils.audit_access import (
    AuditAccessError,
    resolve_audit_entity,
)
from superset.mcp_service.versioning.schemas import (
    AssetActivityRequest,
    AssetActivityResponse,
    AssetRef,
    AssetVersionSnapshotResponse,
    AssetVersionsRequest,
    AssetVersionsResponse,
    AuditError,
    RetentionDisclosure,
)
from superset.versioning.activity.orchestrator import (
    ActivityParamsError,
    get_activity,
    parse_activity_query_params,
)
from superset.versioning.disclosure import (
    paginate_with_disclosure,
    retention_disclosure,
)
from superset.versioning.schemas import ActivityRecordSchema, VersionListItemSchema

AssetKind = Literal["chart", "dashboard", "dataset"]

_version_item_schema = VersionListItemSchema()
_activity_record_schema = ActivityRecordSchema()

_NAME_ATTR: dict[str, str] = {
    "chart": "slice_name",
    "dashboard": "dashboard_title",
    "dataset": "table_name",
}


def _asset_ref(kind: AssetKind, entity: Any) -> AssetRef:
    return AssetRef(
        kind=kind,
        id=entity.id,
        uuid=str(entity.uuid),
        name=getattr(entity, _NAME_ATTR[kind], None),
    )


def _retention() -> RetentionDisclosure:
    return RetentionDisclosure(**retention_disclosure())


def _error(exc: AuditAccessError) -> AuditError:
    return AuditError(error_type=exc.error_type, message=exc.message)


async def versions_tool_body(
    kind: AssetKind,
    model_cls: type[Model],
    request: AssetVersionsRequest,
    ctx: Context,
) -> AssetVersionsResponse | AssetVersionSnapshotResponse | AuditError:
    """Body of ``get_<asset>_versions``: list page or single snapshot."""
    await ctx.info("Resolving %s versions: uuid=%s" % (kind, request.uuid))
    try:
        entity, entity_uuid = resolve_audit_entity(model_cls, request.uuid)
    except AuditAccessError as exc:
        await ctx.warning("Version lookup blocked: %s" % exc.message)
        return _error(exc)

    asset = _asset_ref(kind, entity)
    with event_logger.log_context(action=f"mcp.get_{kind}_versions"):
        if request.version_uuid is not None:
            try:
                version_uuid = UUID(request.version_uuid)
            except ValueError:
                return AuditError(
                    error_type="InvalidUuid", message="Invalid version UUID"
                )
            snapshot = VersionDAO.get_version(
                model_cls, entity_uuid, version_uuid, entity=entity
            )
            if snapshot is None:
                return AuditError(
                    error_type="NotFound",
                    message=(
                        "Version not found; it may have been pruned by retention "
                        "(see get_%s_versions retention disclosure)" % kind
                    ),
                )
            if "_version" in snapshot:
                snapshot["_version"] = _version_item_schema.dump(snapshot["_version"])
            version_block = snapshot.get("_version") or {}
            return AssetVersionSnapshotResponse(
                asset=asset,
                version_uuid=str(version_uuid),
                transaction_id=version_block.get("transaction_id"),
                snapshot=snapshot,
                retention=_retention(),
            )

        versions = VersionDAO.list_versions(model_cls, entity_uuid, entity=entity)
        if versions is None:
            return AuditError(error_type="NotFound", message=f"{kind} not found")
        rows = _version_item_schema.dump(versions, many=True)
        page = paginate_with_disclosure(
            rows, page=request.page, page_size=request.page_size
        )
        return AssetVersionsResponse(asset=asset, retention=_retention(), **page)


async def activity_tool_body(
    kind: AssetKind,
    model_cls: type[Model],
    request: AssetActivityRequest,
    ctx: Context,
) -> AssetActivityResponse | AuditError:
    """Body of ``get_<asset>_activity``: one page of the activity stream."""
    await ctx.info("Resolving %s activity: uuid=%s" % (kind, request.uuid))
    try:
        entity, _ = resolve_audit_entity(model_cls, request.uuid)
    except AuditAccessError as exc:
        await ctx.warning("Activity lookup blocked: %s" % exc.message)
        return _error(exc)

    raw_args = {
        "since": request.since,
        "until": request.until,
        "include": request.include,
        "q": request.q,
        "page": str(request.page),
        "page_size": str(request.page_size),
    }
    try:
        params = parse_activity_query_params(
            {k: v for k, v in raw_args.items() if v is not None}
        )
    except ActivityParamsError as exc:
        return AuditError(error_type="InvalidParams", message=str(exc))

    with event_logger.log_context(action=f"mcp.get_{kind}_activity"):
        records, count, truncated = get_activity(
            model_cls, entity.uuid, resolved_entity=entity, **params
        )
    return AssetActivityResponse(
        asset=_asset_ref(kind, entity),
        include=params["include"],
        result=_activity_record_schema.dump(records, many=True),
        count=count,
        truncated=truncated,
        page=params["page"],
        page_size=params["page_size"],
        retention=_retention(),
    )
