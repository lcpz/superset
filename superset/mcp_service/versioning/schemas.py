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
"""Pydantic schemas for the version-history / activity MCP tools.

Version and activity records are the marshmallow-dumped dicts the REST
``/versions/`` and ``/activity/`` endpoints return (``VersionListItemSchema``
/ ``ActivityRecordSchema``), passed through verbatim so a human reading the
REST response and an agent reading the MCP response see identical entries.
"""

from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from superset.mcp_service.common.error_schemas import MCPBaseError

MAX_PAGE_SIZE = 200


class RetentionDisclosure(BaseModel):
    """Retention/pruning disclosure (mirrors ``RetentionDisclosureSchema``)."""

    version_history_days: int | None = Field(
        None,
        description=(
            "SUPERSET_VERSION_HISTORY_RETENTION_DAYS; null when pruning is disabled"
        ),
    )
    pruning_enabled: bool = Field(
        ..., description="True when versions older than the cutoff may be pruned"
    )
    history_begins_at: str | None = Field(
        None,
        description=(
            "Earliest UTC timestamp guaranteed retained; entries before it may "
            "have been pruned and their absence is not evidence of inactivity"
        ),
    )


class AssetVersionsRequest(BaseModel):
    """Input for ``get_<asset>_versions``."""

    model_config = ConfigDict(populate_by_name=True)

    uuid: Annotated[
        str,
        Field(
            description="UUID of the chart / dashboard / dataset",
            validation_alias=AliasChoices("uuid", "asset_uuid", "identifier"),
        ),
    ]
    version_uuid: str | None = Field(
        None,
        description=(
            "When set, return the full snapshot of this one version instead of "
            "the list. Use the stable version_uuid (never version_number, which "
            "shifts after retention pruning)."
        ),
    )
    page: Annotated[int, Field(0, ge=0, description="0-based page")] = 0
    page_size: Annotated[
        int,
        Field(25, ge=1, le=MAX_PAGE_SIZE, description="Versions per page"),
    ] = 25


class AssetActivityRequest(BaseModel):
    """Input for ``get_<asset>_activity``."""

    model_config = ConfigDict(populate_by_name=True)

    uuid: Annotated[
        str,
        Field(
            description="UUID of the chart / dashboard / dataset",
            validation_alias=AliasChoices("uuid", "asset_uuid", "identifier"),
        ),
    ]
    since: str | None = Field(None, description="ISO-8601 lower bound (inclusive)")
    until: str | None = Field(None, description="ISO-8601 upper bound (inclusive)")
    include: Literal["all", "self", "related"] = Field(
        "all",
        description=(
            "'self' = edits to this asset; 'related' = edits to linked assets "
            "(charts on a dashboard, dataset behind a chart); 'all' = both. "
            "Datasets have no related layer: 'related' is always empty and "
            "'all' equals 'self'."
        ),
    )
    q: str | None = Field(None, description="Server-side substring search")
    page: Annotated[int, Field(0, ge=0, description="0-based page")] = 0
    page_size: Annotated[
        int,
        Field(25, ge=1, le=MAX_PAGE_SIZE, description="Records per page"),
    ] = 25


class AssetRef(BaseModel):
    """Identity of the asset whose history is being returned."""

    kind: Literal["chart", "dashboard", "dataset"]
    id: int
    uuid: str
    name: str | None = None


class AssetVersionsResponse(BaseModel):
    """Paginated version list with completeness + retention disclosure."""

    asset: AssetRef
    result: list[dict[str, Any]] = Field(
        ..., description="VersionListItemSchema rows (newest first as REST)"
    )
    count: int = Field(..., description="Total versions currently retained")
    truncated: bool = Field(..., description="True when more pages exist")
    page: int
    page_size: int
    retention: RetentionDisclosure


class AssetVersionSnapshotResponse(BaseModel):
    """One version snapshot: entity fields plus the ``_version`` block."""

    asset: AssetRef
    version_uuid: str
    transaction_id: int | None = None
    snapshot: dict[str, Any] = Field(
        ..., description="Same body as GET .../versions/<version_uuid>/"
    )
    retention: RetentionDisclosure


class AssetActivityResponse(BaseModel):
    """Paginated activity stream with completeness + retention disclosure."""

    asset: AssetRef
    include: str
    result: list[dict[str, Any]] = Field(..., description="ActivityRecordSchema rows")
    count: int = Field(
        ...,
        description=(
            "Total matching records across pages (post-visibility filter); a "
            "floor when truncated is true"
        ),
    )
    truncated: bool = Field(
        ...,
        description=(
            "True when the fetch ceiling was hit and older records exist; "
            "narrow since/until to see them"
        ),
    )
    page: int
    page_size: int
    retention: RetentionDisclosure


class AuditError(MCPBaseError):
    """Error payload for version/activity tools."""
