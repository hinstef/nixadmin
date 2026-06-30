"""
nixadmin-helper — privileged Unix-socket daemon for nixos-rebuild.

Runs as root. Accepts JSON requests from the nixadmin group, executes
nixos-rebuild, and streams stdout+stderr back line by line.

Protocol (newline-terminated JSON on both sides):
  Request:  {"action": "test"|"switch"|"boot"|"revert"}
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

FLAKE_DIR = os.environ["NIXADMIN_FLAKE_DIR"]
HOSTNAME   = os.environ["NIXADMIN_HOSTNAME"]

DEVNULL = subprocess.DEVNULL


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
    # oneshot + RemainAfterExit: running => "activating"; success => "active"
    # (exited); failure => "failed". Done once it leaves the activating state.
    return _show(svc, "ActiveState") in ("active", "failed")


def _exit_code(svc: str) -> int:
    if _show(svc, "ActiveState") == "active":
        return 0  # exited cleanly
    try:
        code = int(_show(svc, "ExecMainStatus"))
    except ValueError:
        code = 1
    return code or 1  # ensure nonzero on a failed/killed unit


def _cleanup_unit(svc: str) -> None:
    # A RemainAfterExit unit lingers after exit so we can read its status; remove it.
    subprocess.run([SYSTEMCTL, "stop", svc], stdout=DEVNULL, stderr=DEVNULL)
    subprocess.run([SYSTEMCTL, "reset-failed", svc], stdout=DEVNULL, stderr=DEVNULL)


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
            if action not in ALLOWED_ACTIONS:
                f.write(
                    json.dumps({
                        "error": f"unknown action '{action}'; allowed: {sorted(ALLOWED_ACTIONS)}"
                    }).encode() + b"\n"
                )
                return

            # Use path: prefix to bypass nix's git ownership check.
            # Without it, nix (via libgit2) rejects repos owned by non-root users.
            # "revert" maps to "switch --rollback" — there is no nixos-rebuild revert subcommand.
            if action == "revert":
                cmd = [NIXOS_REBUILD, "switch", "--rollback"]
            else:
                cmd = [NIXOS_REBUILD, action, "--flake", f"path:{FLAKE_DIR}#{HOSTNAME}"]
            # "boot" stages the new generation for next reboot without activating it live

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
        if len(parts) >= 3 and parts[2] in ("active", "failed", "inactive"):
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
