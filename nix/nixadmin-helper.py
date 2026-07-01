"""
nixadmin-helper — privileged Unix-socket daemon for nixos-rebuild.

Runs as root. Accepts JSON requests from the nixadmin group, executes
nixos-rebuild (or a `systemctl restart <unit>`), and streams stdout+stderr back
line by line.

Protocol (newline-terminated JSON on both sides):
  Request:  {"action": "test"|"switch"|"boot"|"revert"}
        or  {"action": "restart", "unit": "<name>.service"}  (the tray's "fix it")
  Response: zero or more {"stream": "<line>"}
  Finally:  {"exit": <returncode>}

The rebuild runs in a DETACHED transient systemd unit (systemd-run), owned by
PID 1 — NOT in this helper's cgroup. This is essential: a `switch` that changes
the helper itself makes switch-to-configuration restart nixadmin-helper.service
mid-activation; if the rebuild were a child of that unit, stopping it would tear
down the very process driving the switch (we hit exactly this, and KillMode=process
did not prevent it). In a separate transient unit the rebuild runs to completion
independently — even if this helper is restarted, only the live stream is lost.
We follow the unit's journal for streaming and read its real exit code via
systemctl.
"""

import grp
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

SOCKET_PATH = "/run/nixadmin-helper.sock"
ALLOWED_ACTIONS = {"test", "switch", "revert", "boot"}

SBIN = "/run/current-system/sw/bin"
NIXOS_REBUILD = f"{SBIN}/nixos-rebuild"
SYSTEMD_RUN = f"{SBIN}/systemd-run"
SYSTEMCTL = f"{SBIN}/systemctl"
JOURNALCTL = f"{SBIN}/journalctl"

# Serialize rebuilds: this socket is the privilege boundary, and any client in the
# nixadmin group can connect directly (bypassing the daemon's per-session lock).
# Two concurrent activations can leave the system in a mixed generation state, so
# only one rebuild runs at a time; a second request waits its turn.
#
# The lock is intentionally IN-MEMORY (not a lockfile): if the helper crashes the
# lock dies with it and systemd restarts unlocked — a pidfile/flock would instead
# leave a stale lock and deadlock. The one case in-memory can't cover is a rebuild
# that *hangs* forever, so a watchdog kills any rebuild that exceeds
# REBUILD_TIMEOUT_S. We never time-release or steal the lock while a rebuild is
# live — that would re-introduce the concurrent-activation corruption.
_rebuild_lock = threading.Lock()

# Backstop against a genuinely hung rebuild — generous, not a cap on slow builds.
REBUILD_TIMEOUT_S = int(os.environ.get("NIXADMIN_REBUILD_TIMEOUT", "3600"))

# Restart action: a quick privileged `systemctl restart <unit>` for the tray's
# "fix it". Not a rebuild — runs directly, not in a detached unit.
RESTART_TIMEOUT_S = 90
# Never restart ourselves: `systemctl restart nixadmin-helper` would kill this
# process mid-restart (self-destruct). The daemon is a user unit, unreachable here.
RESTART_DENY = {"nixadmin-helper.service"}
# A well-formed systemd unit name: safe charset + a known suffix. No shell is used,
# so this is belt-and-braces against a malformed/garbage target from a client.
_UNIT_RE = re.compile(r"^[A-Za-z0-9@._:\\-]+\.(service|socket|timer|target|path|mount)$")

DEVNULL = subprocess.DEVNULL


# --- pure decision logic (unit-tested; see tests/test_helper_smoke.py) --------- #


def build_cmd(action: str, flake_dir: str, hostname: str) -> list:
    """nixos-rebuild argv for an action. 'revert' has no subcommand of its own — it
    maps to 'switch --rollback'. The path: prefix bypasses nix's git-ownership check
    on the user-owned flake."""
    if action == "revert":
        return [NIXOS_REBUILD, "switch", "--rollback"]
    return [NIXOS_REBUILD, action, "--flake", f"path:{flake_dir}#{hostname}"]


def unit_is_finished(active_state: str) -> bool:
    """A oneshot+RemainAfterExit unit is done once it leaves 'activating':
    success => 'active' (exited), failure => 'failed'."""
    return active_state in ("active", "failed")


def exit_code_from(active_state: str, exec_main_status: str) -> int:
    """0 when the unit exited cleanly ('active'); otherwise its ExecMainStatus,
    forced nonzero so a failed or killed unit never reports success."""
    if active_state == "active":
        return 0
    try:
        code = int(exec_main_status)
    except (ValueError, TypeError):
        code = 1
    return code or 1


def is_reapable(active_state: str) -> bool:
    """True only for a FINISHED rebuild unit (safe to remove at startup). A running
    rebuild ('activating'/'reloading') must be left alone — reaping it would kill a
    live switch, the exact corruption this mechanism exists to prevent."""
    return active_state in ("active", "failed", "inactive")


def valid_unit(unit: str) -> bool:
    """True if `unit` is a well-formed systemd unit name we're allowed to restart —
    rejects malformed/garbage names and the deny-list (never restart ourselves)."""
    return bool(_UNIT_RE.match(unit)) and unit not in RESTART_DENY


def _send(f, obj) -> bool:
    """Write one newline-JSON message. Returns False if the client is gone
    (broken pipe) so the caller can stop writing without aborting the rebuild."""
    try:
        f.write(json.dumps(obj).encode() + b"\n")
        return True
    except (BrokenPipeError, OSError):
        return False


def _show(svc: str, prop: str) -> str:
    r = subprocess.run([SYSTEMCTL, "show", svc, "-p", prop, "--value"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def _finished(svc: str) -> bool:
    return unit_is_finished(_show(svc, "ActiveState"))


def _exit_code(svc: str) -> int:
    return exit_code_from(_show(svc, "ActiveState"), _show(svc, "ExecMainStatus"))


def _cleanup_unit(svc: str) -> None:
    # A RemainAfterExit unit lingers after exit so we can read its status; remove it.
    subprocess.run([SYSTEMCTL, "stop", svc], stdout=DEVNULL, stderr=DEVNULL)
    subprocess.run([SYSTEMCTL, "reset-failed", svc], stdout=DEVNULL, stderr=DEVNULL)


def _run_restart(unit: str, f) -> None:
    """Run `systemctl restart <unit>` directly (not a detached unit — it's quick and
    doesn't restart this helper). Streams output and returns the real exit code; the
    daemon verifies the unit's resulting state afterward."""
    proc = subprocess.Popen(
        [SYSTEMCTL, "restart", unit],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    watchdog = threading.Timer(RESTART_TIMEOUT_S, proc.kill)
    watchdog.start()
    try:
        client_alive = True
        for line in proc.stdout:
            if client_alive and not _send(f, {"stream": line}):
                client_alive = False
        proc.wait()
    finally:
        watchdog.cancel()
    _send(f, {"exit": proc.returncode})


def _run_rebuild(cmd: list[str], f) -> None:
    """Run the rebuild in a detached transient unit and stream its journal back."""
    unit = f"nixadmin-rebuild-{int(time.time())}-{os.getpid()}"
    svc = f"{unit}.service"

    # Start the journal follower first (filtered to this unit) so no early output is
    # missed, then launch the detached unit.
    follower = subprocess.Popen(
        [JOURNALCTL, f"--unit={svc}", "-o", "cat", "--no-pager", "-f", "--since=now"],
        stdout=subprocess.PIPE, stderr=DEVNULL,
    )
    start = subprocess.run(
        [SYSTEMD_RUN, "--quiet", f"--unit={unit}", "--service-type=oneshot",
         "--property=RemainAfterExit=yes",
         f"--setenv=PATH={os.environ.get('PATH', SBIN)}",
         "--setenv=HOME=/root",
         *cmd],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if start.returncode != 0:
        follower.terminate()
        _send(f, {"stream": f"could not start rebuild unit: {start.stdout.strip()}\n"})
        _send(f, {"exit": start.returncode or 1})
        return

    done = threading.Event()

    def watch() -> None:
        deadline = time.monotonic() + REBUILD_TIMEOUT_S
        while not done.wait(1.0):
            if _finished(svc):
                break
            if time.monotonic() > deadline:
                subprocess.run([SYSTEMCTL, "kill", "--signal=SIGKILL", svc],
                               stdout=DEVNULL, stderr=DEVNULL)
                break
        follower.terminate()  # unblock the streaming loop

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()

    client_alive = True
    try:
        for raw in follower.stdout:
            line = raw.decode(errors="replace")
            if client_alive and not _send(f, {"stream": line}):
                client_alive = False  # client gone; keep draining so we exit cleanly
    finally:
        done.set()
        watcher.join(timeout=10)

    code = _exit_code(svc)
    if code != 0:
        _send(f, {"stream": f"(rebuild exited {code})\n"})
    _send(f, {"exit": code})
    _cleanup_unit(svc)


def handle_client(conn: socket.socket) -> None:
    try:
        with conn.makefile("rwb", buffering=0) as f:
            raw = f.readline()
            if not raw:
                return
            try:
                req = json.loads(raw.decode())
            except json.JSONDecodeError as e:
                f.write(json.dumps({"error": f"bad JSON: {e}"}).encode() + b"\n")
                return

            action = req.get("action", "")

            # Restart a system unit (the tray's "fix it") — a quick privileged op,
            # separate from the rebuild path. Validate the unit name (belt-and-braces;
            # no shell is used) and refuse the deny-list.
            if action == "restart":
                unit = req.get("unit", "")
                if not valid_unit(unit):
                    _send(f, {"error": f"invalid or disallowed unit: {unit!r}"})
                    return
                _run_restart(unit, f)
                return

            if action not in ALLOWED_ACTIONS:
                f.write(
                    json.dumps({
                        "error": f"unknown action '{action}'; allowed: "
                                 f"{sorted(ALLOWED_ACTIONS)} + restart"
                    }).encode() + b"\n"
                )
                return

            cmd = build_cmd(action, os.environ["NIXADMIN_FLAKE_DIR"],
                            os.environ["NIXADMIN_HOSTNAME"])

            # Only one rebuild at a time. Tell the client if it has to wait.
            if not _rebuild_lock.acquire(blocking=False):
                _send(f, {"stream": "another rebuild is in progress; waiting…\n"})
                _rebuild_lock.acquire()
            try:
                _run_rebuild(cmd, f)
            finally:
                _rebuild_lock.release()

    except BrokenPipeError:
        pass  # client disconnected mid-stream — fine
    except Exception as e:
        print(f"[nixadmin-helper] client error: {e}", file=sys.stderr)
    finally:
        conn.close()


def _cleanup_stale() -> None:
    """Remove finished rebuild units left behind if a previous helper was restarted
    mid-switch. Only touches *finished* units — a rebuild still running (e.g. one
    that outlived its helper) is left alone to complete."""
    r = subprocess.run(
        [SYSTEMCTL, "list-units", "--all", "--plain", "--no-legend", "--no-pager",
         "nixadmin-rebuild-*.service"],
        capture_output=True, text=True,
    )
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and is_reapable(parts[2]):
            _cleanup_unit(parts[0])


def main() -> None:
    _cleanup_stale()

    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)

    gid = grp.getgrnam("nixadmin").gr_gid
    os.chown(SOCKET_PATH, 0, gid)
    os.chmod(SOCKET_PATH, 0o660)

    server.listen(4)
    print(f"[nixadmin-helper] listening on {SOCKET_PATH}", flush=True)

    while True:
        conn, _ = server.accept()
        threading.Thread(target=handle_client, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
