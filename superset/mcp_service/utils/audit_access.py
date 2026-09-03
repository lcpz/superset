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
"""Fail-closed entity resolution for audit-oriented MCP tools.

Wraps :func:`superset.versioning.api_helpers.resolve_endpoint_path_entity`
(the same UUID-parse → ``find_active_by_uuid`` → ``raise_for_access``
preflight the ``/versions/`` and ``/activity/`` REST endpoints run) so the
MCP surface can't drift to a weaker gate than the REST surface. Unknown
model classes raise ``LookupError`` from the dispatch table — fail closed.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from flask_appbuilder import Model

from superset.versioning.api_helpers import (
    PathEntityResponseError,
    resolve_endpoint_path_entity,
)


class AuditAccessError(Exception):
    """Entity could not be resolved: invalid UUID (400), missing (404) or
    forbidden (403). ``error_type`` is the MCP error label to report."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message

    @property
    def error_type(self) -> str:
        return {400: "InvalidUuid", 403: "AccessDenied", 404: "NotFound"}.get(
            self.status, "AccessError"
        )


class _StatusCapture:
    """Minimal stand-in for the FAB ``api`` argument: records the status."""

    def response_400(self, message: str = "Bad request") -> tuple[int, str]:
        return 400, message

    def response_403(self, message: str = "Forbidden") -> tuple[int, str]:
        return 403, message

    def response_404(self, message: str = "Not found") -> tuple[int, str]:
        return 404, message


def resolve_audit_entity(model_cls: type[Model], uuid_str: str) -> tuple[Any, UUID]:
    """Resolve *uuid_str* to a live, access-checked entity or raise
    :class:`AuditAccessError`.

    Missing and forbidden are reported distinctly only because the REST
    endpoints already do so; callers that must not disclose existence can
    collapse both to ``NotFound``.
    """
    try:
        return resolve_endpoint_path_entity(_StatusCapture(), model_cls, uuid_str)
    except PathEntityResponseError as exc:
        status, message = exc.response
        raise AuditAccessError(status, message) from exc
