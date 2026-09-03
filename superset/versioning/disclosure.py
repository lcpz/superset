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
"""Completeness and retention disclosure primitives.

Audit consumers (humans and MCP agents) must never mistake "no records
returned" for "no records exist". Two independent effects can hide rows:

* **Bounding** — a list was paginated or hit a fetch ceiling. Disclosed by
  the ``count`` / ``truncated`` pair (the same semantics as
  ``ActivityResponseSchema``), extended here with the page bounds so a
  consumer can re-issue the exact same request.
* **Retention pruning** — the ``version_history.prune_old_versions`` task
  drops version rows whose transaction is older than
  ``SUPERSET_VERSION_HISTORY_RETENTION_DAYS``. Disclosed by
  :func:`retention_disclosure`, which reports the window and the earliest
  instant history is guaranteed to reach back to.

Both helpers are plain-dict producers so the REST endpoints and the MCP
tools share one implementation instead of re-deriving bounding logic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence, TypeVar

from flask import current_app

T = TypeVar("T")

#: Server-side ceiling for ``page_size`` on paginated disclosure envelopes.
MAX_PAGE_SIZE: int = 200
DEFAULT_PAGE_SIZE: int = 100

#: Beat-schedule task name of the version-history prune task. Must stay in
#: sync with ``AppInitializer._RETENTION_TASK_NAME`` and the default
#: ``CeleryConfig.beat_schedule`` entry in ``superset/config.py``.
_RETENTION_TASK_NAME: str = "version_history.prune_old_versions"


def _prune_task_scheduled() -> bool:
    """Whether the version-history prune task is actually scheduled to run.

    A positive ``SUPERSET_VERSION_HISTORY_RETENTION_DAYS`` only expresses a
    *policy*; pruning only happens when Celery is configured and its
    ``beat_schedule`` includes the ``version_history.prune_old_versions``
    task. Mirrors the detection in
    ``AppInitializer._warn_if_retention_beat_missing``. When
    ``CELERY_CONFIG`` is a dotted import string it is resolved by Celery's
    loader rather than here; resolving operator code solely to answer this
    question would duplicate the loader and risk import side effects (the
    same reason ``AppInitializer._warn_if_retention_beat_missing`` skips it),
    so we conservatively report the schedule as unknown (``False``) rather
    than claiming a prune that may never fire.
    """
    celery_config: Any = current_app.config.get("CELERY_CONFIG")
    if celery_config is None:
        return False  # Celery disabled entirely; the prune task never fires.
    if isinstance(celery_config, str):
        # Resolved by Celery's loader; the schedule is not inspectable here.
        # Report False so pruning_enabled never over-claims (history_begins_at
        # stays conservative regardless).
        return False
    beat_schedule = (
        celery_config.get("beat_schedule")
        if isinstance(celery_config, dict)
        else getattr(celery_config, "beat_schedule", None)
    )
    if not beat_schedule:
        return False
    # Match on the ``task`` each entry runs, not the schedule key: an operator
    # may register the retention task under any key. Also tolerate the default
    # config's convention of using the task name as the key.
    scheduled_tasks: set[Any] = {
        entry.get("task") for entry in beat_schedule.values() if isinstance(entry, dict)
    }
    scheduled_tasks.update(beat_schedule)
    return _RETENTION_TASK_NAME in scheduled_tasks


def bounded_page_size(page_size: int | None, default: int = DEFAULT_PAGE_SIZE) -> int:
    """Clamp *page_size* into ``[1, MAX_PAGE_SIZE]`` (``None`` → *default*)."""
    if page_size is None:
        return default
    return max(1, min(int(page_size), MAX_PAGE_SIZE))


def parse_page_params(args: Any) -> tuple[int, int | None]:
    """Read optional ``page`` / ``page_size`` query params from *args*.

    Returns ``(page, page_size)`` where ``page_size`` is ``None`` when the
    client did not ask for pagination (callers then return the full list,
    preserving pre-pagination response contracts). Raises ``ValueError`` on
    non-integer or negative input.
    """
    raw_page = args.get("page")
    raw_size = args.get("page_size")
    page = int(raw_page) if raw_page not in (None, "") else 0
    page_size = int(raw_size) if raw_size not in (None, "") else None
    if page < 0 or (page_size is not None and page_size < 1):
        raise ValueError("page must be >= 0 and page_size must be >= 1")
    return page, page_size


def paginate_with_disclosure(
    items: Sequence[T],
    *,
    page: int = 0,
    page_size: int | None = None,
) -> dict[str, Any]:
    """Slice *items* into a page and wrap it in a completeness envelope.

    Returns::

        {
            "count": <true total across all pages>,
            "result": [<page items>],
            "truncated": <True when items exist beyond this page>,
            "page": <page>,
            "page_size": <bounded page size>,
        }

    ``count`` is always the full total (the caller has already applied any
    access filtering), so ``truncated`` is exact rather than a floor.

    ``page_size=None`` means "no pagination requested": every item is
    returned and ``page_size`` echoes the total, so existing consumers that
    never send paging params keep receiving the complete list.
    """
    count = len(items)
    if page_size is None:
        return {
            "count": count,
            "result": list(items),
            "truncated": False,
            "page": 0,
            "page_size": count,
        }
    size = bounded_page_size(page_size)
    offset = max(0, int(page)) * size
    result = list(items[offset : offset + size])
    return {
        "count": count,
        "result": result,
        "truncated": offset + len(result) < count,
        "page": max(0, int(page)),
        "page_size": size,
    }


def retention_disclosure(now: datetime | None = None) -> dict[str, Any]:
    """Describe the version-history retention window in effect.

    Returns::

        {
            "version_history_days": <int | None>,   # None when pruning is off
            "pruning_enabled": <bool>,
            "history_begins_at": <ISO-8601 naive-UTC | None>,
        }

    ``pruning_enabled`` reflects whether pruning is *actually active*: it
    requires both a positive ``SUPERSET_VERSION_HISTORY_RETENTION_DAYS`` and
    the ``version_history.prune_old_versions`` task being scheduled in
    ``CELERY_CONFIG.beat_schedule`` (see :func:`_prune_task_scheduled`). A
    positive retention value with Celery disabled or the task unscheduled
    reports ``pruning_enabled=False``.

    ``history_begins_at`` is the completeness floor: the most recent instant
    before which history may be incomplete. It is the *latest* of two
    boundaries, because a record can be missing due to either one:

    * the *current* prune cutoff (``now - retention``) under the policy in
      effect right now, and
    * the durable prune watermark — the most recent ``issued_at`` the prune
      task has ever actually deleted (see
      :attr:`SharedKey.VERSION_HISTORY_PRUNE_WATERMARK`).

    The watermark is what makes this correct across policy changes: if a
    shorter window pruned aggressively in the past, that destruction stands
    even after retention is widened or disabled. So the watermark applies
    even when ``pruning_enabled`` is ``False``. ``history_begins_at`` is
    ``None`` only when pruning is disabled *and* nothing was ever pruned.
    Absence of a record before this floor is never by itself evidence it
    never existed.
    """
    retention_days = int(
        current_app.config.get("SUPERSET_VERSION_HISTORY_RETENTION_DAYS", 30)
    )
    watermark = _prune_watermark()
    if retention_days <= 0:
        # Pruning is off now, but past pruning is irreversible: the watermark
        # (if any) is still the completeness floor.
        return {
            "version_history_days": None,
            "pruning_enabled": False,
            "history_begins_at": watermark.isoformat() if watermark else None,
        }
    # Naive UTC, matching ``version_transaction.issued_at`` and the prune
    # task's own cutoff arithmetic.
    reference = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = reference - timedelta(days=retention_days)
    # The floor is the most recent of the current cutoff and the historical
    # high-water mark of destruction.
    floor = max(cutoff, watermark) if watermark else cutoff
    return {
        "version_history_days": retention_days,
        "pruning_enabled": _prune_task_scheduled(),
        "history_begins_at": floor.isoformat(),
    }


def _prune_watermark() -> datetime | None:
    """Read the durable prune high-water mark, or ``None`` if never pruned.

    Persisted by ``version_history.prune_old_versions`` as an ISO-8601
    naive-UTC string under :attr:`SharedKey.VERSION_HISTORY_PRUNE_WATERMARK`.
    Read failures (e.g. missing ``key_value`` table before migrations) are
    swallowed to ``None``: a disclosure endpoint must never 500 because the
    watermark could not be read.
    """
    # pylint: disable=import-outside-toplevel
    from superset.key_value.shared_entries import get_shared_value
    from superset.key_value.types import SharedKey

    try:
        raw = get_shared_value(SharedKey.VERSION_HISTORY_PRUNE_WATERMARK)
    except Exception:  # pylint: disable=broad-except
        return None
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None
