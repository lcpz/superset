<!--
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
-->

# Version Control + Auditing: 5-PR Narrative, Context & Execution Plan

This document is the working plan for closing the audit-fragmentation gaps
between Superset's versioning surface, its REST read model, and the MCP tool
surface. It is written so that a human reviewer or an AI agent can pick up any
one of the PRs below, understand why it exists, and verify it independently
against the code it references. All file references point at the current
`master` of this fork; see [References](#references) for the anchor lines.

## 1. Problem thesis

Superset already records a lot of audit-relevant state: versioned shadow rows
with `version_uuid`/`transaction_id` handles, an activity stream, soft-delete
tombstones, a deterministic `related_objects` join, and a canonical hashing
primitive. What it does **not** do is expose that state in one coherent,
reviewable shape. Six gaps were verified in the code:

1. **Reverse lineage is REST-only.** `DatasetDAO.get_related_objects` answers
   "which charts and dashboards depend on this dataset", but nothing on the MCP
   surface can reach it. An agent asked to assess a dataset retirement cannot
   enumerate the blast radius.
2. **`ChartInfo` cannot be joined back to a dataset.** The MCP chart
   serializer emits `datasource_name` and `datasource_type` only. Names are
   neither unique nor stable, so an agent cannot deterministically map a chart
   to the dataset it reads from.
3. **Completeness is undisclosed on `related_objects`.** The DAO returns
   unbounded `.all()` lists with no `count`/`truncated` envelope, so a consumer
   cannot tell whether it saw everything. The activity endpoint already has
   the right disclosure primitives (`count` + `truncated`), but they are not
   applied uniformly.
4. **Version and activity history are REST-only.** The versioning API has a
   fail-closed access template (`resolve_endpoint_path_entity`), but there is
   no MCP tool for chart/dashboard/dataset versions or activity, so an agent
   cannot see *what changed* or *who changed it*.
5. **No reviewable evidence artifact.** Export bundles serialize YAML with
   `sort_keys=False`, so hashing exported bytes is not reproducible. There is
   no canonical, bounded payload that two independent reviewers can hash and
   compare.
6. **Execution trails are not correlated.** `Query` rows (SQL Lab) and
   `ReportExecutionLog` rows have no shared identifier linking them to a
   migration or review event, so real customer engagements cannot tie "what
   ran" to "why it ran".

Each gap maps to exactly one PR below.

## 2. Target scenario

**Retire a widely-used dataset and migrate its charts and dashboards.**

A data team wants to replace dataset `D_old` with `D_new`. To do this safely
and defensibly they need:

- **Reverse lineage** — every chart reading `D_old`, and every dashboard that
  hosts one of those charts.
- **Definition preservation** — the pre-migration definition of each affected
  chart/dashboard/dataset, addressable by `version_uuid` so it survives
  retention pruning.
- **Change visibility** — the activity records showing who changed what, when,
  during the migration window.
- **Retention/limit disclosure** — an explicit statement of whether any list
  was truncated, windowed, or subject to retention pruning, so absence of
  evidence is never mistaken for evidence of absence.
- **Independent re-verification** — a human reviewer and an AI reviewer must
  each be able to recompute the same digest over the same bounded payload and
  get the same answer, without trusting the exporter.

## 3. Existing deterministic read model to reuse

Nothing below requires a new storage model. The plan reuses:

| Primitive | Location | Role |
| --- | --- | --- |
| `DatasetDAO.get_related_objects` | `superset/daos/dataset.py` | Deterministic `Slice.datasource_id == <dataset id>` + `datasource_type == TABLE` join, then `Dashboard.slices` join. Reverse-lineage source of truth. |
| REST `GET /api/v1/dataset/<id_or_uuid>/related_objects` | `superset/datasets/api.py` | Existing consumer of the DAO; shape to mirror. |
| Versions / activity endpoints | `superset/versioning/api_helpers.py`, `superset/versioning/schemas.py` | `version_uuid` / `transaction_id` handles; `count` + `truncated` disclosure on activity. |
| `resolve_endpoint_path_entity` | `superset/versioning/api_helpers.py` | UUID parse → active lookup → `raise_for_access` with a per-model kwarg; unknown model **fails closed** with `LookupError`. |
| `hash_from_dict` | `superset/utils/hashing.py` | `json.dumps(..., sort_keys=True)` then configurable algorithm (`sha256`/`md5`). Canonical digest primitive. |
| `SoftDeleteMixin` + `do_orm_execute` listener | `superset/models/helpers.py` | Global soft-delete filtering, gated by `is_feature_enabled("SOFT_DELETE")`, applied to selects including relationship loads. |
| MCP truncation categories | `superset/mcp_service/utils/token_utils.py` | `INFO_TOOLS` are truncated; `DATA_QUERY_TOOLS` drop tail rows; everything else hard-blocks on oversize responses. |
| Guest allowlist | `superset/mcp_service/mcp_config.py` | `MCP_GUEST_ALLOWED_TOOLS` is default-deny; new tools are not guest-visible unless explicitly added. |

## 4. The PRs, in dependency order

### PR 1 — `feat(mcp): expose datasource_id and dataset uuid on ChartInfo`

**Gap closed:** #2.

**Change:** add `datasource_id: int | None` and `dataset_uuid: str | None` to
`ChartInfo` in `superset/mcp_service/chart/schemas.py`, populated in
`serialize_chart_object`. `dataset_uuid` is set only when the chart is
table-backed (`chart.table` is present), mirroring how
`superset/commands/chart/export.py` derives `dataset_uuid` from
`model.table.uuid`.

```python
class ChartInfo(BaseModel):
    ...
    datasource_id: int | None = Field(None, description="Datasource id")
    datasource_name: str | None
    datasource_type: str | None
    dataset_uuid: str | None = Field(
        None, description="UUID of the backing dataset when table-backed"
    )
```

**Why first:** PRs 3 and 5 join charts to datasets by id/uuid. Without this
the reverse-lineage output cannot be cross-checked against `list_charts` /
`get_chart_info` output.

**Verification:** `get_chart_info` on a table-backed chart returns a
`datasource_id` equal to the `id` of the dataset whose `related_objects`
lists that chart; non-table charts return `dataset_uuid=None`.

**Flags / migration:** none. Additive schema field; `get_chart_info` is in
`INFO_TOOLS`, so a larger payload is truncated rather than hard-blocked.

### PR 2 — `feat(api): pagination and completeness disclosure on related_objects and activity`

**Gap closed:** #3.

**Change:** additive only.

- `related_objects` response gains `count` (per collection), `truncated`,
  and a page cursor; the DAO gains `limit`/`offset` parameters with a server
  ceiling. Default behaviour (no params) is unchanged except for the extra
  fields.
- Activity responses keep the existing `count` / `truncated` semantics from
  `ActivityResponseSchema` and additionally disclose the retention window
  (`SUPERSET_VERSION_HISTORY_RETENTION_DAYS`) so a consumer can tell that
  "no records" may mean "pruned".

```text
GET /api/v1/dataset/<uuid>/related_objects?page=0&page_size=100
{
  "charts":     {"count": 342, "result": [...], "truncated": true},
  "dashboards": {"count":  57, "result": [...], "truncated": false},
  "retention":  {"version_history_days": 90}
}
```

**Why second:** every later tool reads through these envelopes; the MCP tools
in PR 3/4 must never re-implement bounding logic.

**Verification:** with `page_size` smaller than the true count, `truncated`
is `true` and `count` is the true total; with `page_size` above the count,
`truncated` is `false`. Existing REST clients that ignore the new keys are
unaffected.

**Flags / migration:** none.

### PR 3 — `feat(mcp): dataset reverse-usage tool`

**Gap closed:** #1.

**Change:** new MCP tool `get_dataset_usage` under
`superset/mcp_service/dataset/tool/`, backed by
`DatasetDAO.get_related_objects` via the PR 2 envelope. Access is gated by
the same preflight the versioning REST endpoints use:

```python
entity, entity_uuid = resolve_endpoint_path_entity(api, SqlaTable, request.dataset_uuid)
# 400 on bad uuid, 404 when not active, 403 when raise_for_access fails,
# LookupError (fail closed) if SqlaTable has no registered kwarg.
```

The tool is **not** added to `MCP_GUEST_ALLOWED_TOOLS`; guests stay
default-denied. It is a list tool, so it is *not* added to `INFO_TOOLS` or
`DATA_QUERY_TOOLS`; oversize responses hard-block, which is the desired
behaviour for lineage (a silently truncated lineage list is worse than an
error). Callers page via PR 2.

**Verification:** for a fixture dataset with N charts across M dashboards,
the tool returns exactly the ids that
`GET /api/v1/dataset/<id>/related_objects` returns, each chart's
`datasource_id` (PR 1) equals the dataset id, and a Gamma user without
dataset access receives a 403-shaped error rather than an empty list.

**Flags / migration:** none.

### PR 4 — `feat(mcp): version and activity tools for chart, dashboard, dataset`

**Gap closed:** #4.

**Change:** new tools `list_versions`, `get_version`, `list_activity`
accepting `entity_type ∈ {chart, dashboard, dataset}` and an entity uuid.
Each call goes through `resolve_endpoint_path_entity` for the model class
matching `entity_type`; responses reuse the REST marshmallow schemas in
`superset/versioning/schemas.py` (`version_uuid`, `version_number`,
`transaction_id`, and the activity `count`/`truncated` envelope) so the MCP
and REST views are byte-for-byte comparable.

Versions are addressed by **`version_uuid`**, never by `version_number`
alone; see [Residual notes](#5-residual-notes).

**Verification:** editing a chart title produces one new version reachable
from both `GET /api/v1/chart/<uuid>/versions` and the MCP `list_versions`
tool with the same `version_uuid`; `list_activity` for the chart shows the
edit with the acting user; requests for an entity the user cannot read
return 403 shape.

**Flags / migration:** none (versioning tables already exist).

### PR 5 — `feat(mcp): reviewable dataset-migration evidence export`

**Gap closed:** #5.

**Change:** new tool `export_dataset_migration_evidence(dataset_uuid,
since, until, page_size)` that composes PR 1–4 into a single JSON payload
and stamps it with a canonical digest:

```python
payload = {
    "dataset": {...},                       # definition at `version_uuid`
    "charts":  {"count": n, "truncated": t, "result": [...]},   # PR 2/3
    "dashboards": {...},
    "versions": {...},                      # PR 4, keyed by version_uuid
    "activity": {"count": n, "truncated": t, "window": {"since":..., "until":...}},
    "retention": {"version_history_days": ..., "soft_delete_enabled": ...},
}
digest = hash_from_dict(payload, algorithm="sha256")
return {"payload": payload, "digest": {"algorithm": "sha256", "value": digest}}
```

Rules:

- **Bound, then hash — never truncate.** The payload is bounded *before*
  hashing by the explicit `page_size` / `since` / `until` inputs, and the
  bounds are part of the hashed payload. The tool is not added to
  `INFO_TOOLS` or `DATA_QUERY_TOOLS`; if the bounded payload still exceeds
  the MCP token limit it hard-blocks and the caller narrows the window.
  A digest over a post-hoc-truncated payload would be unreproducible.
- **Canonical serialization.** `hash_from_dict` uses `sort_keys=True`, so
  key order in the source dicts is irrelevant. The `sha256` algorithm is
  pinned explicitly rather than inheriting `HASH_ALGORITHM` (which may be
  `md5`), so two deployments with different config produce the same digest.
- **Do not hash export-bundle bytes.** `ExportAssetsCommand` and the
  per-model exporters emit YAML with `sort_keys=False` and a wall-clock
  `timestamp`; those bytes are not reproducible and are out of scope for
  evidence.

**Verification (the independent re-verification step):** a human reviewer
fetches the same REST endpoints with the same bounds, assembles the same
dict, and runs `hash_from_dict(payload, algorithm="sha256")`; an AI reviewer
calls the MCP tool. Both must produce the same digest. Any mismatch is a
finding.

**Flags / migration:** none. Soft-delete disclosure reads
`is_feature_enabled("SOFT_DELETE")`.

### PR 6 (future extension) — `feat: cross-trail correlation identifier`

**Gap closed:** #6. **Not in the current series.**

**Change:** add a nullable `correlation_id` column to `Query`
(`superset/models/sql_lab.py`) and `ReportExecutionLog`
(`superset/reports/models.py`), stamped from a request header / MCP
argument, so SQL Lab executions and scheduled report runs triggered during
a migration can be joined to the PR 5 evidence digest.

**Why deferred:** it requires an Alembic migration and therefore the SIP-59
approval process called out in `.github/PULL_REQUEST_TEMPLATE.md`
(atomic, rollback-safe, backwards-compatible, runtime estimates). PRs 1–5
are deliberately migration-free so they can land independently. This PR
becomes relevant once a real customer engagement needs execution evidence,
not just definition evidence.

## 5. Residual notes

- **`get_related_objects` parameter is misnamed.** The DAO signature is
  `get_related_objects(database_id: int)` but the value is compared against
  `Slice.datasource_id`, i.e. it is a *dataset* id. PR 3 should call it by
  position and PR 2 may rename the parameter (keeping a keyword alias) but
  must not change semantics.
- **Positional `version_number` is unstable across prunes.** The scheduled
  `prune_old_versions` task drops shadow rows older than
  `SUPERSET_VERSION_HISTORY_RETENTION_DAYS`, so the same integer can refer
  to different rows before and after a prune cycle. All tools and evidence
  payloads address versions by `version_uuid` (with `transaction_id` as the
  stable live handle) and treat `version_number` as display-only.
- **Soft-delete guarantee is flag-gated.** The global `do_orm_execute`
  listener only attaches `deleted_at IS NULL` criteria when
  `is_feature_enabled("SOFT_DELETE")` is true. With the flag off, deletes
  are hard deletes and "definition preservation" for deleted objects relies
  on version shadow rows alone. PR 5 discloses the flag state in
  `retention.soft_delete_enabled`.
- **Report execution evidence needs `ReportExecutionLog`, not
  `get_report_info`.** The MCP `get_report_info` tool wraps
  `ReportScheduleDAO` and returns schedule metadata only; it does not
  surface execution rows. Any "did the report run / what did it produce"
  evidence must read `ReportExecutionLog` directly (PR 6 territory).
- **Guest principals.** None of the new tools are added to
  `MCP_GUEST_ALLOWED_TOOLS`. Embedded guest tokens remain unable to see
  lineage, versions, activity, or evidence exports.

## 6. Implementation notes / lessons learned

Recorded while landing PRs 1–5 (`lcpz/superset#8`–`#12`).

**What shipped vs. the plan**

- PR 1 (#8): `ChartInfo.datasource_id` + `ChartInfo.dataset_uuid`; the UUID
  is `None` for non-`table` datasources or dangling `table` relationships.
- PR 2 (#9): `related_objects` gained additive `truncated`/`page`/`page_size`
  per collection; child access filtering happens *before* counting so
  `count` is the visible count. `retention` block
  (`version_history_days`, `pruning_enabled`, `history_begins_at`) on
  versions and activity. `DatasetDAO.get_related_objects(database_id)` was
  renamed to `dataset_id` (positional call sites unaffected).
- PR 3 (#10): `get_dataset_usage` MCP tool, sharing
  `superset/datasets/related_objects.py` with the REST endpoint.
- PR 4 (#11): six per-asset tools (`get_{chart,dashboard,dataset}_{versions,activity}`)
  over a common `superset/mcp_service/versioning/service.py`; a single
  version is addressed by `version_uuid` only.
- PR 5 (#12): `GET /api/v1/dataset/<uuid>/migration_evidence/` and
  `export_dataset_migration_evidence`, both over
  `superset/versioning/evidence.py`. Streaming-on-demand, no migration.
  `ReportExecutionLog` is read directly under `ReportExecutionLogFilter`
  (the narrative placed this in PR 6 territory; it turned out to be cheap
  to include, only the *correlation id* is deferred). Query evidence uses
  a heuristic table-name match and is labelled as such.

**Access control**

- `resolve_endpoint_path_entity` expects a FAB API object whose
  `response_40x` methods return Flask `Response`s and raises
  `PathEntityResponseError`. The MCP side reuses it through a tiny adapter
  (`superset/mcp_service/utils/audit_access.py::resolve_audit_entity`) that
  captures the status and maps 400/403/404 to structured tool errors, so
  REST and MCP fail closed identically.
- None of the new tools are in `INFO_TOOLS`/`DATA_QUERY_TOOLS` or
  `MCP_GUEST_ALLOWED_TOOLS`; tests assert both.

**Determinism / hashing**

- `hash_from_dict(..., algorithm=...)` is typed
  `Literal["md5", "sha256"] | None`; pin the constant as
  `Literal["sha256"]` or mypy rejects it. Hash the `evidence` dict, keep
  `generated_at` outside it, and never truncate after hashing — bounds
  (`page_size ≤ 25`, `record_limit ≤ 200`) are applied first and reported
  via `truncated`/`coverage.complete`.
- Continuum `issued_at` is naive UTC; `retention_disclosure()` and the
  evidence window comparisons use naive UTC too, to match the pruning task.
- Pruned `before`/`after` snapshots keep their `version_uuid`/`transaction_id`
  and report `unavailable_reason: "pruned_or_missing"` rather than dropping
  the row, so a digest still covers the handle.

**Test-harness quirks**

- Importing `superset.mcp_service.app` or any ORM-model module outside the
  pytest conftest fails with "App not initialized yet" — not a defect.
- MCP dataset tests use `Client(mcp)` with `get_user_from_request` patched;
  DAO/security patches must target the *shared helper* module, not the tool.
- Lint that bit: mypy runs on tests (bare `dict` rejected; `SimpleNamespace`
  stand-ins need an `Any`-typed factory), ruff `PT011` needs `match=`,
  `PT018` forbids compound asserts, `auto-walrus` rewrites tests.
- Dev VM: Python 3.11 venv via `uv venv --python 3.11 .venv`; dev
  requirements install minus `mysqlclient`/`python-ldap` (no system
  headers). Run `pre-commit` with the venv activated.

**Deferred**

- PR 6 (cross-trail correlation id on `Query`/`ReportExecutionLog`
  transactions, SIP-59 migration) remains future work; PR 5 flags query
  correlation as heuristic until it lands.

## References

Line numbers are against the fork's `master` at the time of writing and are
provided for traceability; symbol names are the durable anchor.

| Topic | Reference |
| --- | --- |
| Reverse-lineage DAO join key and misnamed `database_id` param | `superset/daos/dataset.py:144-165` (`DatasetDAO.get_related_objects`) |
| Fail-closed access template to anchor all new tools | `superset/versioning/api_helpers.py:291-299` (`resolve_endpoint_path_entity`, `_RAISE_FOR_ACCESS_KWARG` lookup → `LookupError`, `raise_for_access` → 403) |
| Soft-delete global listener (flag-gated, includes relationship loads) | `superset/models/helpers.py:1331-1336` (`SoftDeleteMixin` docstring), `superset/models/helpers.py:1448-1457` (`is_select and not is_column_load and is_feature_enabled("SOFT_DELETE")`) |
| MCP truncation categories (everything else hard-blocks) | `superset/mcp_service/utils/token_utils.py:463-470` (`INFO_TOOLS`), `superset/mcp_service/utils/token_utils.py:476-482` (`DATA_QUERY_TOOLS`) |
| Guest default-deny allowlist | `superset/mcp_service/mcp_config.py:204-212` (`MCP_GUEST_ALLOWED_TOOLS`) |
| Canonical hashing primitive; `sort_keys=True` and configurable algorithm | `superset/utils/hashing.py:75-95` (`hash_from_dict`), `superset/utils/hashing.py:32-45` (`_HASH_FUNCTIONS`, `get_hash_algorithm`) |
| Non-canonical export serialization (why not to hash bundle bytes) | `superset/commands/export/assets.py:42-48` (wall-clock `timestamp`, `yaml.safe_dump(..., sort_keys=False)`), `superset/commands/chart/export.py:74-75` (`dataset_uuid` from `model.table.uuid`; see also `sort_keys=False` at line 81) |
| `ChartInfo` datasource fields today (PR 1 target) | `superset/mcp_service/chart/schemas.py:520-527` (`serialize_chart_object` → `ChartInfo(datasource_name=..., datasource_type=...)`) |
| Report tool stops at schedule metadata; execution evidence needs `ReportExecutionLog` (PR 5/6) | `superset/mcp_service/report/tool/get_report_info.py:84-92` (`ModelGetInfoCore(dao_class=ReportScheduleDAO, ...)`), `superset/reports/models.py` (`class ReportExecutionLog`) |
| `Query` columns for PR 6 correlation stamping | `superset/models/sql_lab.py:138-170` (`Query` model columns) |
| Activity `count` / `truncated` disclosure primitives (PR 2 reuse) | `superset/versioning/schemas.py:461-483` (`ActivityResponseSchema`) |
| `version_number` instability under retention pruning | `superset/versioning/queries.py:214-219` (`prune_old_versions`, `SUPERSET_VERSION_HISTORY_RETENTION_DAYS`) |
| PR template / SIP-59 migration gate | `.github/PULL_REQUEST_TEMPLATE.md:22-25` |
