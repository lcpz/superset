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
"""Shared body for dataset reverse-usage (``related_objects``) lookups.

Used by the REST ``GET /api/v1/dataset/<id_or_uuid>/related_objects``
endpoint and by MCP tools so both surfaces return the same charts and
dashboards with the same access filtering, pagination and completeness
disclosure.
"""

from __future__ import annotations

from typing import Any

from superset.connectors.sqla.models import SqlaTable
from superset.daos.dataset import DatasetDAO
from superset.extensions import security_manager
from superset.versioning.disclosure import paginate_with_disclosure


def serialize_related_chart(chart: Any) -> dict[str, Any]:
    """Minimal chart projection carried by the related-objects response."""
    return {
        "id": chart.id,
        "uuid": str(chart.uuid) if chart.uuid else None,
        "slice_name": chart.slice_name,
        "viz_type": chart.viz_type,
    }


def serialize_related_dashboard(dashboard: Any) -> dict[str, Any]:
    """Minimal dashboard projection carried by the related-objects response."""
    return {
        "id": dashboard.id,
        "uuid": str(dashboard.uuid) if dashboard.uuid else None,
        "json_metadata": dashboard.json_metadata,
        "slug": dashboard.slug,
        "title": dashboard.dashboard_title,
    }


def get_dataset_related_objects(
    dataset: SqlaTable,
    *,
    page: int = 0,
    page_size: int | None = None,
) -> dict[str, Any]:
    """Access-filtered, paginated charts and dashboards depending on *dataset*.

    Returns ``{"charts": envelope, "dashboards": envelope}`` where each
    envelope is produced by
    :func:`superset.versioning.disclosure.paginate_with_disclosure`
    (``count`` / ``result`` / ``truncated`` / ``page`` / ``page_size``).
    ``count`` is the total number of objects the requester may see; objects
    the requester cannot access are silently dropped before counting, so
    a Gamma user never learns how many hidden dependents exist.
    """
    data = DatasetDAO.get_related_objects(dataset.id)
    charts = [
        serialize_related_chart(chart)
        for chart in data["charts"]
        if security_manager.can_access_chart(chart)
    ]
    dashboards = [
        serialize_related_dashboard(dashboard)
        for dashboard in data["dashboards"]
        if security_manager.can_access_dashboard(dashboard)
    ]
    return {
        "charts": paginate_with_disclosure(charts, page=page, page_size=page_size),
        "dashboards": paginate_with_disclosure(
            dashboards, page=page, page_size=page_size
        ),
    }
