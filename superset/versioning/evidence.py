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
"""Reviewable dataset-migration evidence bundle.

Given a dataset and a bounded time window, :func:`build_dataset_migration_evidence`
assembles, from the *same* primitives the REST and MCP audit surfaces use:

* the dependent-asset inventory (``related_objects``; one page),
* for the dataset and each inventoried asset on that page, the versions
  issued inside the window plus the ``before``/``after`` snapshots addressed
  by pruning-stable ``version_uuid``/``transaction_id``,
* each asset's own activity records (who/when),
* report/alert executions for those charts/dashboards
  (``ReportExecutionLog`` under ``ReportExecutionLogFilter`` scoping),
* SQL Lab query executions on the dataset's database whose SQL mentions the
  table (``Query`` under ``QueryFilter`` scoping; heuristic, disclosed),
* retention/coverage disclosures.

The bundle is a canonical, JSON-safe ``dict``; :func:`digest_evidence` hashes
it with ``hash_from_dict(..., algorithm="sha256")``. Inputs are *bounded
first, then hashed*: every collection has a hard cap and reports
``truncated`` instead of being cut after the fact, so the digest always
covers exactly the bytes a reviewer sees.

Honest limit: the digest proves the bundle was not altered after it was
produced. It does not prove the live database was truthful at that time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from flask_appbuilder import Model
from flask_appbuilder.models.sqla.interface import SQLAInterface

from superset import db, security_manager
from superset.connectors.sqla.models import SqlaTable
from superset.daos.version import VersionDAO
from superset.datasets.related_objects import get_dataset_related_objects
from superset.models.dashboard import Dashboard
from superset.models.slice import Slice
from superset.models.sql_lab import Query
from superset.queries.filters import QueryFilter
from superset.reports.filters import ReportExecutionLogFilter
from superset.reports.models import ReportExecutionLog, ReportSchedule
from superset.utils.hashing import hash_from_dict
from superset.versioning.activity.orchestrator import (
    ActivityParamsError,
    get_activity,
    parse_activity_query_params,
)
from superset.versioning.disclosure import retention_disclosure
from superset.versioning.schemas import ActivityRecordSchema, VersionListItemSchema

EVIDENCE_SCHEMA_VERSION = 1
DIGEST_ALGORITHM: Literal["sha256"] = "sha256"

MAX_ASSETS_PER_PAGE = 25
DEFAULT_ASSETS_PER_PAGE = 10
MAX_RECORDS_PER_COLLECTION = 200

_QUERY_MATCHING_NOTE = (
    "heuristic: SQL Lab queries on the dataset's database whose sql/"
    "executed_sql contains the table name (literal, case-insensitive); no hard "
    "correlation id exists yet (future PR 6). Requires can_read on Query; "
    "otherwise authorized=False and the collection is empty."
)

_version_item_schema = VersionListItemSchema()
_activity_record_schema = ActivityRecordSchema()


class EvidenceParamsError(ValueError):
    """Raised for an unusable window/page combination."""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _epoch_ms(value: datetime) -> float:
    # Window bounds are naive UTC (see activity ``_parse_iso_datetime``);
    # ``Query.start_time`` is epoch milliseconds.
    return value.replace(tzinfo=timezone.utc).timestamp() * 1000


def _in_window(
    issued_at: datetime | None, since: datetime | None, until: datetime | None
) -> bool:
    if issued_at is None:
        return False
    if since is not None and issued_at < since:
        return False
    return not (until is not None and issued_at > until)


def _snapshot(
    model_cls: type[Model], entity: Any, version: dict[str, Any] | None
) -> dict[str, Any] | None:
    if version is None:
        return None
    snap = VersionDAO.get_version(
        model_cls, entity.uuid, UUID(str(version["version_uuid"])), entity=entity
    )
    if snap is None:
        return {
            "version_uuid": str(version["version_uuid"]),
            "transaction_id": version["transaction_id"],
            "state": None,
            "unavailable_reason": "pruned_or_missing",
        }
    version_meta = snap.pop("_version", None)
    return {
        "version_uuid": str(version["version_uuid"]),
        "transaction_id": version["transaction_id"],
        "issued_at": _iso(version.get("issued_at")),
        "version": _version_item_schema.dump(version_meta) if version_meta else None,
        "state": snap,
    }


def _asset_evidence(
    kind: str,
    model_cls: type[Model],
    entity: Any,
    name: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> dict[str, Any]:
    versions = VersionDAO.list_versions(model_cls, entity.uuid, entity=entity) or []
    in_window = [v for v in versions if _in_window(v.get("issued_at"), since, until)]
    before_candidates = [
        v
        for v in versions
        if since is not None
        and v.get("issued_at") is not None
        and v["issued_at"] < since
    ]
    before = before_candidates[-1] if before_candidates else None
    after_candidates = [
        v
        for v in versions
        if until is None or _in_window(v.get("issued_at"), None, until)
    ]
    after = after_candidates[-1] if after_candidates else None

    records, count, truncated = get_activity(
        model_cls,
        entity.uuid,
        since=since,
        until=until,
        include="self",
        page=0,
        page_size=limit,
        resolved_entity=entity,
    )
    return {
        "kind": kind,
        "id": entity.id,
        "uuid": str(entity.uuid),
        "name": name,
        "versions_in_window": {
            "result": _version_item_schema.dump(in_window[:limit], many=True),
            "count": len(in_window),
            "truncated": len(in_window) > limit,
        },
        "before": _snapshot(model_cls, entity, before),
        "after": _snapshot(model_cls, entity, after),
        "activity": {
            "result": _activity_record_schema.dump(records, many=True),
            "count": count,
            "truncated": truncated or count > len(records),
        },
    }


def _stable_retention() -> dict[str, Any]:
    """Retention disclosure with ``history_begins_at`` at day resolution.

    Retention is configured in whole days and the pruning task runs on a
    schedule, so the sub-second prune cutoff carries no information; keeping
    it would make two back-to-back exports of unchanged state hash differently.
    """
    retention = retention_disclosure()
    cutoff = retention.get("history_begins_at")
    if isinstance(cutoff, str):
        retention["history_begins_at"] = cutoff[:10]
    return retention


def _bounded(query: Any, limit: int) -> tuple[list[Any], bool]:
    rows = query.limit(limit + 1).all()
    return rows[:limit], len(rows) > limit


def _report_executions(
    chart_ids: list[int],
    dashboard_ids: list[int],
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> dict[str, Any]:
    if not chart_ids and not dashboard_ids:
        return {"result": [], "count": 0, "truncated": False}
    query = (
        db.session.query(ReportExecutionLog, ReportSchedule)
        .join(
            ReportSchedule, ReportExecutionLog.report_schedule_id == ReportSchedule.id
        )
        .filter(
            db.or_(
                ReportSchedule.chart_id.in_(chart_ids or [-1]),
                ReportSchedule.dashboard_id.in_(dashboard_ids or [-1]),
            )
        )
    )
    if since is not None:
        query = query.filter(ReportExecutionLog.scheduled_dttm >= since)
    if until is not None:
        query = query.filter(ReportExecutionLog.scheduled_dttm <= until)
    query = ReportExecutionLogFilter("id", SQLAInterface(ReportExecutionLog)).apply(
        query, None
    )
    rows, truncated = _bounded(query.order_by(ReportExecutionLog.id), limit)
    return {
        "result": [
            {
                "log_id": log.id,
                "log_uuid": str(log.uuid) if log.uuid else None,
                "report_schedule_id": schedule.id,
                "report_name": schedule.name,
                "report_type": schedule.type,
                "chart_id": schedule.chart_id,
                "dashboard_id": schedule.dashboard_id,
                "state": log.state,
                "scheduled_dttm": _iso(log.scheduled_dttm),
                "start_dttm": _iso(log.start_dttm),
                "end_dttm": _iso(log.end_dttm),
                "error_message": log.error_message,
            }
            for log, schedule in rows
        ],
        "count": len(rows),
        "truncated": truncated,
    }


_LIKE_ESCAPE = "\\"


def _like_pattern(table_name: str) -> str:
    """Literal substring LIKE pattern: ``%``, ``_`` and the escape char escaped."""
    escaped = (
        table_name.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def _query_executions(
    dataset: SqlaTable,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> dict[str, Any]:
    if not security_manager.can_access("can_read", "Query"):
        return {
            "matching": _QUERY_MATCHING_NOTE,
            "authorized": False,
            "result": [],
            "count": 0,
            "truncated": False,
        }
    needle = _like_pattern(dataset.table_name)
    query = db.session.query(Query).filter(
        Query.database_id == dataset.database_id,
        db.or_(
            Query.sql.ilike(needle, escape=_LIKE_ESCAPE),
            Query.executed_sql.ilike(needle, escape=_LIKE_ESCAPE),
        ),
    )
    if since is not None:
        query = query.filter(Query.start_time >= _epoch_ms(since))
    if until is not None:
        query = query.filter(Query.start_time <= _epoch_ms(until))
    query = QueryFilter("id", SQLAInterface(Query)).apply(query, None)
    rows, truncated = _bounded(query.order_by(Query.id), limit)
    return {
        "matching": _QUERY_MATCHING_NOTE,
        "authorized": True,
        "result": [
            {
                "query_id": q.id,
                "client_id": q.client_id,
                "user_id": q.user_id,
                "status": q.status,
                "start_time": q.start_time,
                "end_time": q.end_time,
                "schema": q.schema,
                "executed_sql": q.executed_sql,
            }
            for q in rows
        ],
        "count": len(rows),
        "truncated": truncated,
    }


def build_dataset_migration_evidence(
    dataset: SqlaTable,
    *,
    since: datetime | None,
    until: datetime | None,
    page: int = 0,
    page_size: int = DEFAULT_ASSETS_PER_PAGE,
    record_limit: int = MAX_RECORDS_PER_COLLECTION,
) -> dict[str, Any]:
    """Return the canonical (hashable) evidence dict for one bounded page."""
    if page < 0 or not 1 <= page_size <= MAX_ASSETS_PER_PAGE:
        raise EvidenceParamsError(
            f"page must be >= 0 and 1 <= page_size <= {MAX_ASSETS_PER_PAGE}"
        )
    if since is not None and until is not None and since > until:
        raise EvidenceParamsError("since must not be after until")
    record_limit = max(1, min(record_limit, MAX_RECORDS_PER_COLLECTION))

    # Pin an open-ended window to a concrete instant so the covered range —
    # and therefore the digest, `after` snapshot and execution rows — is
    # reproducible from the recorded params. Naive UTC to match `since` and
    # `retention_disclosure` (see ``_epoch_ms``).
    if until is None:
        until = datetime.now(timezone.utc).replace(tzinfo=None)

    inventory = get_dataset_related_objects(dataset, page=page, page_size=page_size)
    chart_ids = [c["id"] for c in inventory["charts"]["result"]]
    dashboard_ids = [d["id"] for d in inventory["dashboards"]["result"]]

    charts = (
        db.session.query(Slice).filter(Slice.id.in_(chart_ids)).order_by(Slice.id).all()
        if chart_ids
        else []
    )
    dashboards = (
        db.session.query(Dashboard)
        .filter(Dashboard.id.in_(dashboard_ids))
        .order_by(Dashboard.id)
        .all()
        if dashboard_ids
        else []
    )

    assets = [
        _asset_evidence(
            "dataset",
            SqlaTable,
            dataset,
            dataset.table_name,
            since,
            until,
            record_limit,
        )
    ]
    assets += [
        _asset_evidence("chart", Slice, c, c.slice_name, since, until, record_limit)
        for c in charts
    ]
    assets += [
        _asset_evidence(
            "dashboard", Dashboard, d, d.dashboard_title, since, until, record_limit
        )
        for d in dashboards
    ]

    report_executions = _report_executions(
        chart_ids, dashboard_ids, since, until, record_limit
    )
    query_executions = _query_executions(dataset, since, until, record_limit)

    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "dataset": {
            "id": dataset.id,
            "uuid": str(dataset.uuid),
            "table_name": dataset.table_name,
            "schema": dataset.schema,
            "database_id": dataset.database_id,
        },
        "window": {"since": _iso(since), "until": _iso(until)},
        "page": {"page": page, "page_size": page_size, "record_limit": record_limit},
        "inventory": inventory,
        "assets": assets,
        "report_executions": report_executions,
        "query_executions": query_executions,
        "coverage": {
            "retention": _stable_retention(),
            "inventory_scope": (
                "TABLE-backed charts and dashboards containing them; objects the "
                "caller cannot access are excluded before counting"
            ),
            "complete": not (
                inventory["charts"]["truncated"]
                or inventory["dashboards"]["truncated"]
                or report_executions["truncated"]
                or not query_executions["authorized"]
                or query_executions["truncated"]
                or any(
                    a["versions_in_window"]["truncated"] or a["activity"]["truncated"]
                    for a in assets
                )
            ),
            "notes": [
                "before = last version issued before `since` (null when since is "
                "unset or nothing precedes it); after = last version issued at or "
                "before `until` (an omitted `until` is pinned to the export "
                "instant recorded in window.until)",
                "versions/activity older than retention.history_begins_at may have "
                "been pruned; absence is not proof of no change",
                "digest proves byte-integrity of this bundle, not truthfulness of "
                "live database state",
            ],
        },
    }


def digest_evidence(evidence: dict[str, Any]) -> dict[str, str]:
    """SHA-256 over the canonical evidence dict (``sort_keys=True``).

    The algorithm is pinned explicitly; the configurable ``HASH_ALGORITHM``
    default (which may be md5) is never used for evidence.
    """
    return {
        "algorithm": DIGEST_ALGORITHM,
        "value": hash_from_dict(evidence, algorithm=DIGEST_ALGORITHM),
        "covers": "evidence",
    }


def parse_evidence_query_params(args: Any) -> dict[str, Any]:
    """Parse ``since``/``until``/``page``/``page_size``/``record_limit`` from
    a request-args mapping, reusing the activity endpoint's datetime parsing.
    Raises :class:`EvidenceParamsError` on malformed input."""
    try:
        activity = parse_activity_query_params(
            {
                k: args.get(k)
                for k in ("since", "until")
                if args.get(k) not in (None, "")
            }
        )
    except ActivityParamsError as exc:
        raise EvidenceParamsError(str(exc)) from exc
    params: dict[str, Any] = {
        "since": activity.get("since"),
        "until": activity.get("until"),
    }
    for key, default in (
        ("page", 0),
        ("page_size", DEFAULT_ASSETS_PER_PAGE),
        ("record_limit", MAX_RECORDS_PER_COLLECTION),
    ):
        raw = args.get(key)
        if raw in (None, ""):
            params[key] = default
            continue
        try:
            params[key] = int(raw)
        except (TypeError, ValueError) as exc:
            raise EvidenceParamsError(f"Invalid {key!r}: {raw!r}") from exc
    return params


def evidence_response_payload(
    dataset: SqlaTable, params: dict[str, Any]
) -> dict[str, Any]:
    """Full response body shared by REST and MCP: hashed ``evidence`` plus
    the unhashed ``digest`` and ``generated_at`` envelope."""
    evidence = build_dataset_migration_evidence(dataset, **params)
    return {
        "evidence": evidence,
        "digest": digest_evidence(evidence),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
