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

    ``history_begins_at`` is the current prune cutoff (``now - retention``):
    version and activity records issued before it may already have been
    pruned, so their absence is not evidence they never existed. It is
    ``None`` when pruning is disabled (non-positive retention).
    """
    retention_days = int(
        current_app.config.get("SUPERSET_VERSION_HISTORY_RETENTION_DAYS", 30)
    )
    if retention_days <= 0:
        return {
            "version_history_days": None,
            "pruning_enabled": False,
            "history_begins_at": None,
        }
    # Naive UTC, matching ``version_transaction.issued_at`` and the prune
    # task's own cutoff arithmetic.
    reference = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = reference - timedelta(days=retention_days)
    return {
        "version_history_days": retention_days,
        "pruning_enabled": True,
        "history_begins_at": cutoff.isoformat(),
    }
