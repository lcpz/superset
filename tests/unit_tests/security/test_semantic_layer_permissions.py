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

from unittest.mock import MagicMock

import pytest

from superset.security.manager import SupersetSecurityManager


def _pvm(permission: str, view_menu: str) -> MagicMock:
    pvm = MagicMock()
    pvm.permission.name = permission
    pvm.view_menu.name = view_menu
    return pvm


@pytest.mark.parametrize("view_menu", ["SemanticLayer", "SemanticView"])
def test_semantic_layer_write_is_admin_only(app_context: None, view_menu: str) -> None:
    """Managing data connections is reserved for Admin."""
    from superset.extensions import appbuilder

    sm = SupersetSecurityManager(appbuilder)

    assert sm._is_gamma_pvm(_pvm("can_write", view_menu)) is False
    assert sm._is_admin_only(_pvm("can_write", view_menu)) is True


@pytest.mark.parametrize("view_menu", ["SemanticLayer", "SemanticView"])
def test_semantic_layer_read_is_gamma(app_context: None, view_menu: str) -> None:
    from superset.extensions import appbuilder

    sm = SupersetSecurityManager(appbuilder)

    assert sm._is_gamma_pvm(_pvm("can_read", view_menu)) is True
