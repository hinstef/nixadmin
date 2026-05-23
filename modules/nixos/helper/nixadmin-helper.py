"""
nixadmin-helper — privileged Unix-socket daemon for nixos-rebuild.

Runs as root. Accepts JSON requests from the nixadmin group, executes
nixos-rebuild, and streams stdout+stderr back line by line.

Protocol (newline-terminated JSON on both sides):
  Request:  {"action": "test"|"switch"|"revert"}
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
ALLOWED_ACTIONS = {"test", "switch", "revert"}

FLAKE_DIR = os.environ["NIXADMIN_FLAKE_DIR"]
HOSTNAME   = os.environ["NIXADMIN_HOSTNAME"]


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

            cmd = ["nixos-rebuild", action, "--flake", f"{FLAKE_DIR}#{HOSTNAME}"]

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # merge stderr into stdout
                text=True,
                bufsize=1,                 # line-buffered
            )

            for line in proc.stdout:
                f.write(json.dumps({"stream": line}).encode() + b"\n")

            proc.wait()
            f.write(json.dumps({"exit": proc.returncode}).encode() + b"\n")

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
