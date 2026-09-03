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
"""Tests for completeness + retention disclosure primitives."""

from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import pytest
from flask import Flask

from superset.versioning.disclosure import (
    MAX_PAGE_SIZE,
    paginate_with_disclosure,
    parse_page_params,
    retention_disclosure,
)


def test_paginate_without_page_size_returns_everything() -> None:
    env = paginate_with_disclosure(list(range(5)))
    assert env == {
        "count": 5,
        "result": [0, 1, 2, 3, 4],
        "truncated": False,
        "page": 0,
        "page_size": 5,
    }


def test_paginate_first_page_truncated_with_true_count() -> None:
    env = paginate_with_disclosure(list(range(5)), page=0, page_size=2)
    assert env["result"] == [0, 1]
    assert env["count"] == 5
    assert env["truncated"] is True


def test_paginate_last_page_not_truncated() -> None:
    env = paginate_with_disclosure(list(range(5)), page=2, page_size=2)
    assert env["result"] == [4]
    assert env["truncated"] is False


def test_paginate_beyond_end_is_empty_and_not_truncated() -> None:
    env = paginate_with_disclosure(list(range(5)), page=9, page_size=2)
    assert env["result"] == []
    assert env["count"] == 5
    assert env["truncated"] is False


def test_paginate_clamps_page_size_to_ceiling() -> None:
    env = paginate_with_disclosure(list(range(3)), page=0, page_size=10_000)
    assert env["page_size"] == MAX_PAGE_SIZE


def test_parse_page_params_defaults_to_unpaginated() -> None:
    assert parse_page_params({}) == (0, None)
    assert parse_page_params({"page": "2", "page_size": "10"}) == (2, 10)


@pytest.mark.parametrize("args", [{"page": "-1"}, {"page_size": "0"}, {"page": "x"}])
def test_parse_page_params_rejects_bad_input(args: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="page|invalid literal"):
        parse_page_params(args)


def test_retention_disclosure_reports_cutoff(app: Flask) -> None:
    with mock.patch.dict(app.config, {"SUPERSET_VERSION_HISTORY_RETENTION_DAYS": 30}):
        env = retention_disclosure(now=datetime(2026, 3, 31, 12, 0, 0))
    assert env == {
        "version_history_days": 30,
        "pruning_enabled": True,
        "history_begins_at": "2026-03-01T12:00:00",
    }


def test_retention_disclosure_disabled(app: Flask) -> None:
    with mock.patch.dict(app.config, {"SUPERSET_VERSION_HISTORY_RETENTION_DAYS": 0}):
        env = retention_disclosure()
    assert env == {
        "version_history_days": None,
        "pruning_enabled": False,
        "history_begins_at": None,
    }


def test_related_objects_helper_filters_access_then_paginates(app: Flask) -> None:
    from superset.datasets import related_objects as mod

    charts = [
        SimpleNamespace(id=i, uuid=None, slice_name=f"c{i}", viz_type="table")
        for i in range(4)
    ]
    dashboards = [
        SimpleNamespace(
            id=i, uuid=None, json_metadata=None, slug=None, dashboard_title=f"d{i}"
        )
        for i in range(2)
    ]
    with (
        mock.patch.object(
            mod.DatasetDAO,
            "get_related_objects",
            return_value={"charts": charts, "dashboards": dashboards},
        ),
        mock.patch.object(
            mod.security_manager,
            "can_access_chart",
            side_effect=lambda c: c.id != 1,
        ),
        mock.patch.object(
            mod.security_manager, "can_access_dashboard", return_value=True
        ),
    ):
        data = mod.get_dataset_related_objects(mock.Mock(id=42), page=0, page_size=2)

    assert data["charts"]["count"] == 3  # hidden chart excluded from count
    assert [c["id"] for c in data["charts"]["result"]] == [0, 2]
    assert data["charts"]["truncated"] is True
    assert data["dashboards"]["count"] == 2
    assert data["dashboards"]["truncated"] is False
    assert data["dashboards"]["result"][0]["title"] == "d0"
