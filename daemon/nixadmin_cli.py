"""
Terminal client for the nixadmin daemon.
Replaces nixadmin-rpc — connects to the Unix socket, streams responses.
"""

import asyncio
import json
import os
import signal
import sys
import threading
import time
import uuid

SOCKET = os.environ.get("NIXADMIN_SOCKET", "/run/user/1000/nixadmin.sock")
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class Spinner:
    def __init__(self):
        self._stop = threading.Event()
        self._thr = None

    def start(self):
        self._stop.clear()
        self._thr = threading.Thread(target=self._spin, daemon=True)
        self._thr.start()

    def stop(self):
        self._stop.set()
        if self._thr:
            self._thr.join(timeout=0.3)
        sys.stdout.write("\r                    \r")
        sys.stdout.flush()

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            sys.stdout.write(f"\r{SPINNER[i % len(SPINNER)]} Thinking...")
            sys.stdout.flush()
            i += 1
            time.sleep(0.08)


async def run():
    try:
        reader, writer = await asyncio.open_unix_connection(SOCKET)
    except FileNotFoundError:
        print(f"nixadmin: daemon not running (socket {SOCKET} not found)", file=sys.stderr)
        sys.exit(1)

    spinner = Spinner()
    pending: dict[str, asyncio.Future] = {}
    streaming = False

    async def recv_loop():
        nonlocal streaming
        async for raw in reader:
            msg = json.loads(raw)
            t = msg.get("type")

            if t == "delta":
                if not streaming:
                    spinner.stop()
                    streaming = True
                sys.stdout.write(msg["text"])
                sys.stdout.flush()

            elif t == "done":
                if streaming:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                streaming = False
                spinner.stop()
                qid = msg.get("id")
                if qid in pending:
                    pending.pop(qid).set_result(None)

            elif t == "error":
                spinner.stop()
                print(f"\nerror: {msg['text']}", file=sys.stderr)
                qid = msg.get("id")
                if qid in pending:
                    pending.pop(qid).set_result(None)

            elif t == "confirm":
                spinner.stop()
                ans = input(f"\n{msg['text']} [y/N] ").strip().lower()
                writer.write((json.dumps({
                    "type": "respond", "id": msg["id"],
                    "value": ans in ("y", "yes"),
                }) + "\n").encode())
                await writer.drain()

            elif t == "event":
                sev = msg.get("severity", "info")
                prefix = {"info": "ℹ", "warning": "⚠", "error": "✗"}.get(sev, "•")
                print(f"\n{prefix} {msg['text']}")

    asyncio.create_task(recv_loop())

    def on_sigint(sig, frame):
        writer.close()
        print()
        sys.exit(0)
    signal.signal(signal.SIGINT, on_sigint)

    print("nixadmin> ", end="", flush=True)
    while True:
        try:
            line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
        except (EOFError, KeyboardInterrupt):
            break
        line = line.strip()
        if not line:
            print("nixadmin> ", end="", flush=True)
            continue

        qid = str(uuid.uuid4())[:8]
        future = asyncio.get_event_loop().create_future()
        pending[qid] = future
        streaming = False
        spinner.start()

        writer.write((json.dumps({"type": "query", "id": qid, "text": line}) + "\n").encode())
        await writer.drain()
        await future

        print("\nnixadmin> ", end="", flush=True)

    writer.close()


if __name__ == "__main__":
    asyncio.run(run())
