"""External command boundary tests."""

from __future__ import annotations

import sys
import time

import pytest

from nixadmin.errors import ExternalProcessError
from nixadmin.util import run, run_checked


async def test_run_reports_unavailable_command():
    with pytest.raises(ExternalProcessError) as caught:
        await run("nixadmin-command-that-does-not-exist")
    assert caught.value.kind == "unavailable"


async def test_run_checked_has_stable_exit_category():
    with pytest.raises(ExternalProcessError) as caught:
        await run_checked(sys.executable, "-c", "raise SystemExit(7)")
    assert caught.value.kind == "command_failed"
    assert caught.value.exit_code == 7


async def test_run_timeout_is_bounded():
    with pytest.raises(ExternalProcessError) as caught:
        await run(sys.executable, "-c", "import time; time.sleep(10)", deadline_s=0.02)
    assert caught.value.kind == "timeout"


async def test_timeout_kills_pipe_inheriting_descendants():
    script = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)']); "
        "time.sleep(10)"
    )
    started = time.monotonic()
    with pytest.raises(ExternalProcessError) as caught:
        await run(sys.executable, "-c", script, deadline_s=0.05)
    assert caught.value.kind == "timeout"
    assert time.monotonic() - started < 1.0
