"""Daemon autofix episode and restart-loop behavior."""

from __future__ import annotations

from nixadmin import remediation
from nixadmin.config import Config
from nixadmin.server import Daemon


async def test_autofix_unit_restarts_and_records_healthy(daemon_socket, tmp_path, monkeypatch):
    cfg = Config(socket_path=daemon_socket, events="sqlite", state_dir=str(tmp_path))
    daemon = Daemon(cfg)
    calls: list[tuple[str, str]] = []

    async def fake_restart(unit, scope, *, status, restart_system):  # noqa: ANN001
        calls.append((unit, scope))
        return remediation.RestartOutcome(True, f"Restarted {unit} — healthy again.")

    async def no_systemd_restarts(_unit, _scope):
        return 0

    monkeypatch.setattr("nixadmin.server.remediation.restart_resolved", fake_restart)
    monkeypatch.setattr("nixadmin.server.remediation.restart_count", no_systemd_restarts)

    await daemon._autofix_unit("foo.service", "user")
    assert calls == [("foo.service", "user")]
    evs = await daemon.store.recent(10, kind="autofix")
    assert evs[0]["meta"]["action"] == "restart"
    assert evs[0]["meta"]["outcome"] == "healthy"
    await daemon.aclose()


async def test_autofix_loop_guard_informs_without_restarting(daemon_socket, tmp_path, monkeypatch):
    cfg = Config(socket_path=daemon_socket, events="sqlite", state_dir=str(tmp_path))
    daemon = Daemon(cfg)  # max_attempts defaults to 1
    # One prior restart attempt already on record, within the window.
    await daemon.store.append("autofix", unit="foo.service", scope="user",
                              text="prior", meta={"action": "restart"})
    restarted = False

    async def fake_restart(*a, **k):
        nonlocal restarted
        restarted = True
        return "x"

    async def no_systemd_restarts(_unit, _scope):
        return 0

    monkeypatch.setattr("nixadmin.server.remediation.restart_resolved", fake_restart)
    monkeypatch.setattr("nixadmin.server.remediation.restart_count", no_systemd_restarts)
    await daemon._autofix_unit("foo.service", "user")
    assert restarted is False  # budget spent → don't loop
    evs = await daemon.store.recent(10, kind="autofix")
    assert evs[0]["meta"]["action"] == "inform"
    await daemon.aclose()


async def test_autofix_respects_systemd_restart_loop(daemon_socket, tmp_path, monkeypatch):
    cfg = Config(socket_path=daemon_socket, events="sqlite", state_dir=str(tmp_path))
    daemon = Daemon(cfg)
    restarted = False

    async def systemd_loop(_unit, _scope):
        return 4

    async def fake_restart(*_args, **_kwargs):
        nonlocal restarted
        restarted = True
        return remediation.RestartOutcome(True, "unexpected")

    monkeypatch.setattr("nixadmin.server.remediation.restart_count", systemd_loop)
    monkeypatch.setattr("nixadmin.server.remediation.restart_resolved", fake_restart)
    await daemon._autofix_unit("foo.service", "user")

    assert restarted is False
    event = (await daemon.store.recent(1, kind="autofix"))[0]
    assert event["meta"]["systemd_restarts"] == 4
    assert event["meta"]["recorded_attempts"] == 0
    await daemon.aclose()


async def test_run_autofix_once_per_episode_and_rearms_on_recovery(
    daemon_socket, tmp_path, monkeypatch
):
    cfg = Config(socket_path=daemon_socket, events="sqlite", state_dir=str(tmp_path))
    daemon = Daemon(cfg)
    calls: list[str] = []

    async def fake_restart(unit, scope, *, status, restart_system):  # noqa: ANN001
        calls.append(unit)
        return remediation.RestartOutcome(False, "restarted, still failing")

    failing = [{"unit": "foo.service", "scope": "user", "description": "x"}]

    async def fake_failed():
        return failing

    monkeypatch.setattr("nixadmin.server.remediation.restart_resolved", fake_restart)
    monkeypatch.setattr("nixadmin.server.remediation.failed_units", fake_failed)

    await daemon._run_autofix()   # foo is new → act once
    await daemon._run_autofix()   # same episode → no second action
    assert calls == ["foo.service"]
    assert ("foo.service", "user") in daemon._autofix_seen

    failing.clear()               # unit recovered
    await daemon._run_autofix()
    assert ("foo.service", "user") not in daemon._autofix_seen  # episode forgotten
    await daemon.aclose()

