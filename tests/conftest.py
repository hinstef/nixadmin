from __future__ import annotations

import pytest


@pytest.fixture
def daemon_socket(tmp_path):
    return str(tmp_path / "nixadmin.sock")
