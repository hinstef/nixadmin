"""
nixadmin-helper — privileged Unix-socket daemon for nixos-rebuild.

Runs as root. Accepts JSON requests from the nixadmin group, executes
nixos-rebuild, and streams stdout+stderr back line by line.

Protocol (newline-terminated JSON on both sides):
  Request:  {"action": "test"|"switch"|"boot"|"revert"}
  Response: zero or more {"stream": "<line>"}
  Finally:  {"exit": <returncode>}
"""

import grp
import json
import os
import socket
import subprocess
import sys
import threading

SOCKET_PATH = "/run/nixadmin-helper.sock"
ALLOWED_ACTIONS = {"test", "switch", "revert", "boot"}

# Serialize rebuilds: this socket is the privilege boundary, and any client in the
# nixadmin group can connect directly (bypassing the daemon's per-session lock).
# Two concurrent activations can leave the system in a mixed generation state, so
# only one rebuild runs at a time; a second request waits its turn.
#
# The lock is intentionally IN-MEMORY (not a lockfile): if the helper crashes the
# lock dies with it and systemd restarts unlocked — a pidfile/flock would instead
# leave a stale lock and deadlock. The one case in-memory can't cover is a rebuild
# that *hangs* forever (finally never runs), so a watchdog kills any rebuild that
# exceeds REBUILD_TIMEOUT_S, which releases the lock via finally. We never time-
# release or steal the lock while a rebuild is live — that would re-introduce the
# concurrent-activation corruption this lock exists to prevent.
_rebuild_lock = threading.Lock()

# Backstop against a genuinely hung rebuild — generous, not a cap on slow builds.
REBUILD_TIMEOUT_S = int(os.environ.get("NIXADMIN_REBUILD_TIMEOUT", "3600"))

FLAKE_DIR = os.environ["NIXADMIN_FLAKE_DIR"]
HOSTNAME   = os.environ["NIXADMIN_HOSTNAME"]


def _send(f, obj) -> bool:
    """Write one newline-JSON message. Returns False if the client is gone
    (broken pipe) so the caller can stop writing without aborting the rebuild."""
    try:
        f.write(json.dumps(obj).encode() + b"\n")
        return True
    except (BrokenPipeError, OSError):
        return False


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
                cmd = ["/run/current-system/sw/bin/nixos-rebuild", "switch", "--rollback"]
            else:
                cmd = ["/run/current-system/sw/bin/nixos-rebuild", action, "--flake", f"path:{FLAKE_DIR}#{HOSTNAME}"]
            # "boot" stages the new generation for next reboot without activating it live

            # Only one rebuild at a time. Tell the client if it has to wait.
            if not _rebuild_lock.acquire(blocking=False):
                _send(f, {"stream": "another rebuild is in progress; waiting…\n"})
                _rebuild_lock.acquire()
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,  # merge stderr into stdout
                    text=True,
                    bufsize=1,                 # line-buffered
                )
                # Watchdog: kill a rebuild that hangs past the timeout, regardless
                # of where we're blocked (the read loop, not just wait()). Killing
                # it closes stdout -> the loop ends -> the lock is released below.
                watchdog = threading.Timer(REBUILD_TIMEOUT_S, proc.kill)
                watchdog.start()
                try:
                    client_alive = True
                    for line in proc.stdout:
                        # If the client vanished, stop writing but keep draining so
                        # the rebuild doesn't block on a full pipe (and isn't orphaned).
                        if client_alive and not _send(f, {"stream": line}):
                            client_alive = False
                    proc.wait()
                finally:
                    watchdog.cancel()
                if proc.returncode is not None and proc.returncode < 0:
                    _send(f, {"stream": f"rebuild aborted after {REBUILD_TIMEOUT_S}s "
                                        f"(or killed: signal {-proc.returncode}).\n"})
            finally:
                _rebuild_lock.release()
            _send(f, {"exit": proc.returncode})

    except BrokenPipeError:
        pass  # client disconnected mid-stream — fine
    except Exception as e:
        print(f"[nixadmin-helper] client error: {e}", file=sys.stderr)
    finally:
        conn.close()


def main() -> None:
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
