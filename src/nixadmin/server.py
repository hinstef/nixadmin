"""The daemon — Unix socket server, query dispatch, monitor broadcast.

Wires every service together and owns the query lifecycle:

    classify (if local) → mutation/route → maybe confirm → run chain → stream
    → history → done

One query in flight per session (serialized via the session lock); different
sessions run concurrently. Monitor events are broadcast to all connected clients.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, cast

from nixadmin import actions, autofix, redact, remediation
from nixadmin import log as logmod
from nixadmin import protocol as wire
from nixadmin.config import Config
from nixadmin.context import ContextCache
from nixadmin.errors import NixadminError, ProtocolError
from nixadmin.history import make_history
from nixadmin.llm import local as local_llm
from nixadmin.llm import remote as remote_llm
from nixadmin.monitors import MonitorRunner
from nixadmin.registry import load_modules
from nixadmin.routing import Chain, Decision, detect_mutation, resolve, resolve_desired_chain
from nixadmin.safety import SafetyGate
from nixadmin.sdk import Module
from nixadmin.session import SessionRegistry
from nixadmin.store import make_store
from nixadmin.supervision import notify, watchdog_interval
from nixadmin.tasks import TaskSet
from nixadmin.timeline import TimelineService

log = logmod.get_logger(__name__)

HELPER_SOCKET = "/run/nixadmin-helper.sock"

# How often autofix polls for failed units. Poll-driven so it catches user-scope
# failures (the services monitor is system-bus only) and observes recovery.
AUTOFIX_POLL_INTERVAL = 15.0
EVENT_PRUNE_INTERVAL = 24 * 60 * 60.0
MAX_WIRE_MESSAGE_BYTES = 64 * 1024
CLIENT_SEND_TIMEOUT_S = 10.0

# Friendly names for the redaction placeholders, so the escalation confirm can say
# *what kind* of thing was stripped without ever echoing the secret itself.
_REMOVED_NAMES = {
    "[email]": "email", "[ip]": "IP address", "[api-key]": "API key",
    "[token]": "token", "Bearer [token]": "token", "[aws-key]": "AWS key",
    "[slack-token]": "Slack token", "[secret]": "secret",
    "/home/[user]": "home path", "/Users/[user]": "home path",
}


async def _silent_status(_text: str) -> None:
    """A no-op status sink for autofix — there's no client turn to narrate to."""
    return None


def _summarize_removed(removed: list[str]) -> str:
    """A clause for the escalation prompt naming what redaction removed."""
    if not removed:
        return ", with any personal details removed"
    names: list[str] = []
    for r in removed:
        name = _REMOVED_NAMES.get(r, "detail")
        if name not in names:
            names.append(name)
    if len(names) == 1:
        joined = names[0]
    else:
        joined = ", ".join(names[:-1]) + " and " + names[-1]
    return f", with your {joined} removed"


class ClientConn:
    """One connected client. Owns its writer and pending confirm/input futures."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.pending: dict[str, asyncio.Future[wire.Respond]] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.owned_tasks: set[asyncio.Task[None]] = set()
        self._send_lock = asyncio.Lock()
        self.closed = False

    async def send(self, msg: wire.Message) -> None:
        payload = wire.encode(msg).encode()
        if len(payload) > MAX_WIRE_MESSAGE_BYTES:
            raise ProtocolError("outgoing message exceeds wire limit")
        async with self._send_lock:
            self.writer.write(payload)
            async with asyncio.timeout(CLIENT_SEND_TIMEOUT_S):
                await self.writer.drain()

    async def confirm(self, qid: str, text: str) -> bool:
        await self.send(wire.Confirm(id=qid, text=text))
        resp = await self._await_response(qid)
        return bool(resp and resp.confirmed)

    async def _await_response(self, qid: str) -> wire.Respond | None:
        fut: asyncio.Future[wire.Respond] = asyncio.get_event_loop().create_future()
        self.pending[qid] = fut
        try:
            return await fut
        except asyncio.CancelledError:
            return None
        finally:
            self.pending.pop(qid, None)

    def deliver_response(self, resp: wire.Respond) -> None:
        fut = self.pending.get(resp.id)
        if fut and not fut.done():
            fut.set_result(resp)


class Daemon:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.modules = load_modules()
        self.history = make_history(config.history)
        # Persistent system-event timeline (failures, explanations, restarts…).
        # The daemon is the single writer; clients read it back over the socket.
        self.store = make_store(config.events, config.state_dir)
        self.timeline = TimelineService(self.store, remediation.failed_units)
        # Autofix: policy + per-episode dedup (units acted on in their current
        # failure episode) + a lock so overlapping failure events serialise.
        self.autofix_cfg = autofix.AutofixConfig(
            enable=config.autofix, system=config.autofix_system,
            max_attempts=config.autofix_max_attempts,
        )
        self._autofix_seen: set[tuple[str, str]] = set()
        self._autofix_lock = asyncio.Lock()
        self._autofix_task: asyncio.Task[None] | None = None
        self._task_set = TaskSet("daemon")
        self._background_tasks = self._task_set.tasks
        self.sessions = SessionRegistry()
        self.safety = SafetyGate(HELPER_SOCKET)
        self.context = ContextCache([
            m.context_provider for m in self.modules if m.context_provider is not None
        ])
        self.monitors = MonitorRunner(self.modules, self._broadcast)
        self.conns: set[ClientConn] = set()
        self.local_ready = False
        self._local_ready_evt = asyncio.Event()
        # Only ready if the remote can actually authenticate (key/proxy present),
        # so we never route work to a backend that will fail with an auth error.
        self.remote_ready = config.remote_usable
        # map exposed tool name -> shell command (fixed; no model-supplied args)
        self._tool_cmds = {
            f"{m.name}_{f.name}": f.cmd
            for m in self.modules for f in m.fetchers if f.expose_as_tool
        }

    # ---- lifecycle -------------------------------------------------------- #

    async def run(self) -> None:
        sock = self.cfg.socket_path
        Path(sock).unlink(missing_ok=True)  # noqa: ASYNC240 — instant local op at startup
        server = await asyncio.start_unix_server(
            self._handle_client, path=sock, limit=MAX_WIRE_MESSAGE_BYTES,
        )
        os.chmod(sock, 0o660)
        log.info("listening", socket=sock, default_chain=self.cfg.default_chain)

        await self.monitors.start()
        if self.cfg.flake_dir:
            try:
                await actions.prune_abandoned_worktrees(self.cfg.flake_dir)
            except Exception as error:  # noqa: BLE001 — housekeeping is best-effort
                log.warning("worktree housekeeping failed", error=str(error))
        if self.cfg.event_retention_days > 0:
            await self._prune_events()
            self._spawn(self._event_prune_loop())
        # Seed the autofix episode-set with units already failed at startup, so we
        # act on failures that *happen* from now on — not a boot-time bulk restart.
        # Seeded before the poll loop starts, so the first tick can't treat a
        # pre-existing failure as new.
        if self.autofix_cfg.enable:
            self._autofix_seen = {
                (u["unit"], u["scope"]) for u in await remediation.failed_units()
            }
            self._autofix_task = asyncio.create_task(self._autofix_loop())
        self._spawn(self._readiness_loop())
        if watchdog_interval() is not None:
            self._spawn(self._watchdog_loop())
        notify("READY=1\nSTATUS=Listening for requests")
        async with server:
            await server.serve_forever()

    async def aclose(self) -> None:
        notify("STOPPING=1\nSTATUS=Shutting down")
        if self._autofix_task is not None:
            self._autofix_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._autofix_task
        await self._task_set.aclose()
        await self.monitors.aclose()
        await self.store.aclose()
        Path(self.cfg.socket_path).unlink(missing_ok=True)  # noqa: ASYNC240 — instant local op

    def _spawn(
        self, coroutine: Coroutine[Any, Any, None], *, owner: ClientConn | None = None,
    ) -> asyncio.Task[None]:
        """Start daemon-owned work and retain it until completion or shutdown."""
        task = self._task_set.spawn(coroutine)
        if owner is not None:
            owner.owned_tasks.add(task)

        def finished(done: asyncio.Task[None]) -> None:
            if owner is not None:
                owner.owned_tasks.discard(done)
                if not done.cancelled() and done.exception() is not None:
                    with contextlib.suppress(RuntimeError):
                        self._task_set.spawn(self._close_connection(owner))

        task.add_done_callback(finished)
        return task

    # ---- connection handling --------------------------------------------- #

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conn = ClientConn(reader, writer)
        self.conns.add(conn)
        try:
            await conn.send(wire.Hello(
                chains=self._chains(),
                ready={"local": self.local_ready, "remote": self.remote_ready},
                default_chain=self.cfg.default_chain,
                modules=[m.name for m in self.modules],
            ))
            async for raw in reader:
                try:
                    line = raw.decode().strip()
                except UnicodeDecodeError as error:
                    log.warning("invalid client encoding", error=str(error))
                    break
                if not line:
                    continue
                await self._on_message(conn, line)
        except (
            asyncio.IncompleteReadError,
            ConnectionResetError,
            ValueError,
            TimeoutError,
        ) as error:
            log.warning("client connection closed", error=str(error))
        finally:
            await self._close_connection(conn)

    async def _close_connection(self, conn: ClientConn) -> None:
        if conn.closed:
            return
        conn.closed = True
        self.conns.discard(conn)
        tasks = tuple(conn.owned_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=1.0)
        for pending in conn.pending.values():
            pending.cancel()
        conn.writer.close()
        with contextlib.suppress(Exception):
            async with asyncio.timeout(1.0):
                await conn.writer.wait_closed()

    async def _on_message(self, conn: ClientConn, line: str) -> None:
        try:
            msg = wire.decode(line)
        except NixadminError as e:
            log.warning("dropping malformed message", error=str(e))
            return

        if isinstance(msg, wire.Query):
            task = self._spawn(self._dispatch(conn, msg), owner=conn)
            conn.tasks[msg.id] = task

            def forget_query(done: asyncio.Task[None]) -> None:
                if conn.tasks.get(msg.id) is done:
                    conn.tasks.pop(msg.id, None)

            task.add_done_callback(forget_query)
        elif isinstance(msg, wire.Cancel):
            pending = conn.tasks.get(msg.id)
            if pending:
                pending.cancel()
        elif isinstance(msg, wire.Respond):
            conn.deliver_response(msg)
        elif isinstance(msg, wire.ListFailures):
            self._spawn(self._list_failures(conn, msg), owner=conn)
        elif isinstance(msg, wire.RestartUnit):
            self._spawn(self._restart_unit(conn, msg), owner=conn)
        elif isinstance(msg, wire.ExplainUnit):
            self._spawn(self._explain_unit(conn, msg), owner=conn)
        elif isinstance(msg, wire.UnitJournal):
            self._spawn(self._unit_journal(conn, msg), owner=conn)
        elif isinstance(msg, wire.GetTimeline):
            self._spawn(self._get_timeline(conn, msg), owner=conn)
        elif isinstance(msg, wire.GetLedger):
            self._spawn(self._get_ledger(conn, msg), owner=conn)

    async def _list_failures(self, conn: ClientConn, msg: wire.ListFailures) -> None:
        """Structured current failures for a client to render actions from.

        Also the transition detector: comparing the live set against the last-seen
        set turns the tray/web poll into failure_observed / failure_cleared events
        on the persistent timeline, with no extra polling loop of our own."""
        units = await remediation.failed_units()
        await self.timeline.record_failure_transitions(units)
        await conn.send(wire.Failures(id=msg.id, units=units))

    async def _record_failure_transitions(self, units: list[dict[str, str]]) -> None:
        await self.timeline.record_failure_transitions(units)

    async def _get_timeline(self, conn: ClientConn, msg: wire.GetTimeline) -> None:
        """Read-only: the persisted event timeline for the web hub."""
        # Fetch one extra row so the client can offer an Older button without a
        # separate count query. IDs are append-only, making this cursor stable
        # while new events arrive at the head of the timeline.
        events, next_cursor = await self.timeline.page(
            msg.limit, unit=msg.unit, before_id=msg.before_id,
        )
        await conn.send(wire.Timeline(
            id=msg.id, events=events, next_cursor=next_cursor,
        ))

    async def _get_ledger(self, conn: ClientConn, msg: wire.GetLedger) -> None:
        """Read-only: the kept-well ledger — the looked-after-itself streak plus a
        quiet tally, folded from the event store. Honest by construction: the live
        failed-unit count is passed in, so anything broken *now* zeroes the streak
        rather than flattering it.

        The relevant kinds are queried *separately* so a flood of benign events
        (journals, monitor pings) on a busy machine can't push the rare
        attention/upkeep events out of a single capped scan; the store's true
        ``MIN(ts)`` supplies the streak floor so a truncated scan can't understate
        a long streak. All queries run concurrently — they're independent."""
        await conn.send(wire.Ledger(id=msg.id, data=await self.timeline.kept_well()))

    async def _restart_unit(self, conn: ClientConn, msg: wire.RestartUnit) -> None:
        """A tray fix-it: restart one already-resolved, currently-failed unit.

        Deterministic and guarded — we act only on a unit the daemon *itself*
        currently reports failed (so a stale or forged request can't restart an
        arbitrary service), and the scope is taken from that live view, not the
        client. Verified and reported like any remediation."""
        try:
            failed = {(u["unit"], u["scope"]): u for u in await remediation.failed_units()}
            match = failed.get((msg.unit, msg.scope))
            if match is None:
                await conn.send(wire.Delta(id=msg.id, text=f"{msg.unit} isn't failing right now."))
                await conn.send(wire.Done(id=msg.id, chain="local", model=self.cfg.local_model))
                return
            outcome = await remediation.restart_resolved(
                match["unit"], match["scope"],
                status=lambda text: conn.send(wire.Status(id=msg.id, text=text)),
                restart_system=self.safety.apply_restart,
            )
            await self.store.append(
                "restart", unit=match["unit"], scope=match["scope"],
                text=outcome.message, meta={"source": "tray", "ok": outcome.ok},
            )
            await conn.send(wire.Delta(id=msg.id, text=outcome.message))
            await conn.send(wire.Done(id=msg.id, chain="local", model=self.cfg.local_model))
        except NixadminError as e:
            await conn.send(wire.Error(id=msg.id, text=str(e)))

    async def _unit_journal(self, conn: ClientConn, msg: wire.UnitJournal) -> None:
        """Recent journal lines for a unit (read-only detail for the web view)."""
        scope = msg.scope if msg.scope in ("system", "user") else "system"
        text = await remediation.unit_journal(msg.unit, scope)
        await self.store.append("journal_snapshot", unit=msg.unit, scope=scope, text=text)
        await conn.send(wire.Journal(id=msg.id, unit=msg.unit, text=text))

    async def _explain_unit(self, conn: ClientConn, msg: wire.ExplainUnit) -> None:
        """On-demand, plain-words explanation of a failure.

        Detection stays deterministic; here the local model only *translates* the
        unit's journal into human terms (what/why/likely fix) — it never acts. It
        fires only on this explicit request (lazy), and may warm up first."""
        try:
            failed = {(u["unit"], u["scope"]): u for u in await remediation.failed_units()}
            match = failed.get((msg.unit, msg.scope))
            if match is None:
                await conn.send(wire.Delta(id=msg.id, text=f"{msg.unit} isn't failing right now."))
                await conn.send(wire.Done(id=msg.id))
                return
            if not self.cfg.has_local:
                await conn.send(wire.Delta(
                    id=msg.id, text="I need a local model configured to explain this."))
                await conn.send(wire.Done(id=msg.id))
                return

            log.info("explain", unit=match["unit"], scope=match["scope"])
            journal = await remediation.unit_journal(match["unit"], match["scope"])
            question = f"Why did {match['unit']} fail, and will restarting it fix it?"
            message = local_llm.augment(question, journal)
            if not await local_llm.is_ready(self.cfg.local_url, self.cfg.local_model):
                await conn.send(wire.Status(id=msg.id, text="warming up the local model…"))
            answer = ""
            async for delta in local_llm.summarize(
                message, model=self.cfg.local_model, url=self.cfg.local_url
            ):
                answer += delta
                await conn.send(wire.Delta(id=msg.id, text=delta))
            if answer.strip():
                await self.store.append(
                    "explanation", unit=match["unit"], scope=match["scope"],
                    text=answer, meta={"model": self.cfg.local_model},
                )
            await conn.send(wire.Done(id=msg.id, chain="local", model=self.cfg.local_model))
        except NixadminError as e:
            await conn.send(wire.Error(id=msg.id, text=str(e)))

    def _chains(self) -> list[str]:
        chains = ["remote"] if self.cfg.remote_model else []
        if self.cfg.has_local:
            chains.insert(0, "local")
        return chains

    # ---- dispatch --------------------------------------------------------- #

    async def _dispatch(self, conn: ClientConn, query: wire.Query) -> None:
        lock = self.sessions.lock(query.session)
        async with lock:
            logmod.bind(query_id=query.id, session=query.session)
            try:
                await self._run_query(conn, query)
            except asyncio.CancelledError:
                await conn.send(wire.Done(id=query.id))
            except NixadminError as e:
                await conn.send(wire.Error(id=query.id, text=str(e)))
            except Exception as e:  # noqa: BLE001
                log.exception("dispatch crashed")
                await conn.send(wire.Error(id=query.id, text=f"internal error: {e}"))
            finally:
                conn.tasks.pop(query.id, None)
                logmod.clear()

    async def _run_query(self, conn: ClientConn, query: wire.Query) -> None:
        explicit: Chain | None = (
            cast(Chain, query.chain) if query.chain in ("local", "remote") else None
        )

        # Remediation (a safe runtime fix like "restart the backup service") is
        # deterministic — handled before classify so it never pays the model-warm
        # cost. Confirmed + verified; independent of read/mutation routing.
        rem = remediation.parse(query.text)
        if rem is not None:
            await self._run_remediation(conn, query, rem)
            return

        # Classify against the local model (skipped on a remote-only machine).
        # If the model is cold (unloaded when idle), tell the user we're loading
        # and give classify a generous timeout — the request itself triggers the
        # on-demand load (~6s). Never skip classify on a cold model: answering with
        # no grounding produces a false "all clear".
        matched: list[Module] = []
        if self.cfg.has_local:
            warming = not self.local_ready
            if warming:
                await conn.send(wire.Status(
                    id=query.id, text="Warming up the local assistant… one moment."))
            ct = local_llm.COLD_CLASSIFY_TIMEOUT if warming else local_llm.CLASSIFY_TIMEOUT
            matched = await local_llm.classify(
                query.text, self.modules, model=self.cfg.local_model,
                url=self.cfg.local_url, timeout_s=ct,
            )

        mutation = detect_mutation(query.text)
        desired, pinned = resolve_desired_chain(
            explicit=explicit, matched=matched, default_chain=self.cfg.default_chain
        )

        # Mutation intent. First try the deterministic action tier (common writes
        # like install/remove an app) — works locally, no frontier model needed.
        if mutation:
            action = actions.parse_action(query.text)
            if action and action.kind in ("install_app", "remove_app"):
                await self._run_action(conn, query, action)
                return
            if action and action.kind == "toggle":
                await conn.send(wire.Delta(
                    id=query.id,
                    text="I can only install and remove apps for now — changing "
                         "settings like that isn't supported yet.",
                ))
                await conn.send(wire.Done(id=query.id, chain="local",
                                          model=self.cfg.local_model))
                return
            # Open-ended change → offer to escalate to the frontier (with consent
            # and redaction). No local fallback — we genuinely can't make it here.
            await self._offer_escalation(
                conn, query, matched, local_fallback=False,
                reason="That's a change I can't make on your device by myself.",
            )
            return

        decision = resolve(
            desired=desired, pinned_local=pinned,
            local_ready=self.local_ready, remote_ready=self.remote_ready,
        )
        chain = await self._apply_decision(conn, query, decision)
        if chain is None:
            return
        if chain == "local":
            # The local model judges its own competence: if it isn't confident it
            # can answer this on-device, offer the frontier (never switch silently).
            # Only worth the extra model call when there's actually a frontier to
            # escalate to — otherwise every local read pays for a moot assessment.
            if self.remote_ready and self.cfg.has_local and await local_llm.assess_escalation(
                query.text, model=self.cfg.local_model, url=self.cfg.local_url
            ):
                await self._offer_escalation(
                    conn, query, matched, local_fallback=True,
                    reason="I'm not sure I can answer this well on your device alone.",
                )
                return
            await self._run_local(conn, query, matched)
        else:
            await self._run_remote(conn, query)

    async def _suggest_package(self, phrase: str) -> str:
        """Real nixpkgs candidates (difflib) judged by the local model."""
        names = await actions.load_package_names(self.cfg.flake_dir)
        candidates = actions.fuzzy_candidates(phrase, names)
        if not candidates:
            return ""
        return await local_llm.judge_package(
            phrase, candidates, model=self.cfg.local_model, url=self.cfg.local_url
        )

    async def _run_action(
        self, conn: ClientConn, query: wire.Query, action: actions.Action
    ) -> None:
        """Deterministic write — validated in a worktree, confirmed, then applied."""
        result = await actions.run_app_action(
            action,
            flake_dir=self.cfg.flake_dir,
            hostname=self.cfg.hostname,
            confirm=lambda text: conn.confirm(query.id, text),
            status=lambda text: conn.send(wire.Status(id=query.id, text=text)),
            switch=self.safety.apply_switch,
            rollback=self.safety.apply_revert,
            suggest=self._suggest_package,
        )
        await self.store.append("action", text=result,
                                meta={"kind": action.kind, "request": query.text})
        await conn.send(wire.Delta(id=query.id, text=result))
        await conn.send(wire.Done(id=query.id, chain="local", model=self.cfg.local_model))

    async def _run_remediation(
        self, conn: ClientConn, query: wire.Query, rem: remediation.Remediation
    ) -> None:
        """Safe runtime fix (e.g. restart a failed unit) — confirmed and verified."""
        result = await remediation.run(
            rem,
            confirm=lambda text: conn.confirm(query.id, text),
            status=lambda text: conn.send(wire.Status(id=query.id, text=text)),
            restart_system=self.safety.apply_restart,
        )
        await self.store.append(
            "restart", text=result, meta={"source": "query", "request": query.text},
        )
        await conn.send(wire.Delta(id=query.id, text=result))
        await conn.send(wire.Done(id=query.id, chain="local", model=self.cfg.local_model))

    async def _redact_query(self, text: str) -> redact.Redaction:
        """Redact before anything leaves the device. With a local model: scrub +
        contextual rewrite. Without one (remote-only machine): the deterministic
        scrub still runs — we never send raw text while claiming it was cleaned."""
        if self.cfg.has_local:
            return await redact.redact(text, model=self.cfg.local_model, url=self.cfg.local_url)
        return redact.scrub_only(text)

    async def _offer_escalation(
        self, conn: ClientConn, query: wire.Query, matched: list[Module],
        *, reason: str, local_fallback: bool,
    ) -> None:
        """Never-silent escalation to the frontier: redact → show exactly what
        would leave the device → confirm → send.

        When there is no frontier to escalate to (remote not configured), don't
        redact or prompt to send data nowhere — answer locally if we can, else say
        plainly it can't be done here. On decline, same fallback.
        """
        if not self.remote_ready:
            await self._stay_local(
                conn, query, matched, local_fallback,
                change_msg="I can't make that change on my own yet — it needs the "
                           "fuller assistant, which isn't set up on this machine.",
            )
            return

        redaction = await self._redact_query(query.text)
        removed = _summarize_removed(redaction.removed)
        prompt = (
            f"{reason}\n\nI can ask the fuller (cloud) assistant, but that means "
            "this leaves your device. Here's exactly what I'd send"
            f"{removed}:\n\n“{redaction.redacted}”\n\n"
            "To answer, it may look things up on your device; anything sensitive is "
            "removed from those too, and nothing else about your machine is sent. "
            "Send it?"
        )
        if not await conn.confirm(query.id, prompt):
            await self._stay_local(
                conn, query, matched, local_fallback,
                change_msg="Okay — keeping this on your device; I'll leave that "
                           "change for now.",
            )
            return
        await self._run_remote(conn, query, text=redaction.redacted)

    async def _stay_local(
        self, conn: ClientConn, query: wire.Query, matched: list[Module],
        local_fallback: bool, *, change_msg: str,
    ) -> None:
        """Stay on-device: a best-effort local answer for a read, or ``change_msg``
        (the honest limitation) for a change we won't/can't make here."""
        if local_fallback and self.cfg.has_local:
            await self._run_local(conn, query, matched)
        else:
            await conn.send(wire.Delta(id=query.id, text=change_msg))
            await conn.send(wire.Done(id=query.id, chain="local", model=self.cfg.local_model))

    async def _apply_decision(
        self, conn: ClientConn, query: wire.Query, decision: Decision
    ) -> Chain | None:
        """Resolve a routing Decision into a concrete chain, handling confirms/waits.

        Returns the chain to run, or None if the query is finished (declined/failed).
        """
        if decision.action == "proceed":
            return decision.chain
        if decision.action == "unavailable":
            await conn.send(wire.Error(id=query.id, text=decision.message))
            return None
        if decision.action == "wait_local":
            await conn.send(wire.Status(id=query.id, text=decision.message))
            await self._wait_local_ready()
            return "local"
        if decision.action == "confirm_remote":
            await conn.send(wire.Status(id=query.id, text=decision.message))
            if await conn.confirm(query.id, decision.message):
                return "remote"
            # declined → honor the original local intent if it can be served
            if self.local_ready:
                return "local"
            await self._wait_local_ready()
            return "local"
        return decision.chain

    # ---- chains ----------------------------------------------------------- #

    async def _run_local(self, conn: ClientConn, query: wire.Query, matched: list[Module]) -> None:
        context = ""
        if matched:
            from nixadmin.prefetch import prefetch
            context = await prefetch(matched)
            # grounding guard: classified but no data → don't let the model guess
            if not context.strip():
                await conn.send(wire.Delta(id=query.id, text="I couldn't check that right now."))
                await conn.send(wire.Done(id=query.id, chain="local", model=self.cfg.local_model))
                return

        # Verbose record → journald, at DEBUG so it is OFF by default: the grounding
        # and the model's answer can contain sensitive system state (a failed unit's
        # log may include tokens/paths) and the conversation itself, which we must
        # not persist to the journal in normal operation. Enable via log_level=DEBUG
        # to answer "why did it say that?" without re-running. At INFO we log only
        # the shape, not the content.
        log.info("grounding", modules=[m.name for m in matched], chars=len(context))
        log.debug("grounding detail", context=context)

        message = local_llm.augment(query.text, context)
        answer = ""
        async for delta in local_llm.summarize(
            message, model=self.cfg.local_model, url=self.cfg.local_url
        ):
            answer += delta
            await conn.send(wire.Delta(id=query.id, text=delta))

        log.debug("local answer", answer=answer)

        await self.history.append(query.session, "user", query.text)
        await self.history.append(query.session, "assistant", answer)
        await self.store.append("ask", text=query.text,
                                meta={"answer": answer, "chain": "local"})
        await conn.send(wire.Done(id=query.id, chain="local", model=self.cfg.local_model))

    async def _run_remote(
        self, conn: ClientConn, query: wire.Query, *, text: str | None = None
    ) -> None:
        """Run the frontier chain. ``text`` overrides ``query.text`` — used to send
        the **redacted** payload on an escalated query, so what we record and what
        we send both reflect what actually left the device (never the raw input).

        On an escalated query, what leaves the device is only what the person
        reviewed: the redacted query, plus whatever the assistant looks up here via
        tools. The pre-assembled grounding context and prior turns are **not** sent
        — they were never shown in the consent prompt, so shipping them (even
        scrubbed) would break the "exactly what I'd send" promise. Tool results run
        **on this machine** and can pull a failed unit's journal, tokens, or paths
        into the cloud conversation, so each is deterministically scrubbed as it
        returns (a reduction of known secret shapes, disclosed in the confirm — not
        a guarantee against every possible identifier) (bv1)."""
        sent = text if text is not None else query.text
        escalated = text is not None
        tools = remote_llm.build_tools(self.modules)
        if escalated:
            history: list[dict[str, str]] = []
            system_extra = ""
        else:
            history = await self.history.recent(query.session, 20)
            system_extra = "\n\n" + (await self.context.assemble())
        state = self.sessions.state(query.session)

        async def run_tool(name: str, args: dict[str, Any]) -> str:
            result = await self._call_tool(name, args, conn, query, state)
            return redact.scrub(result).text if escalated else result

        answer = ""
        async for delta in remote_llm.run(
            sent, model=self.cfg.remote_model, api_base=self.cfg.remote_base,
            tools=tools, run_tool=run_tool, history=history, system_extra=system_extra,
        ):
            answer += delta
            await conn.send(wire.Delta(id=query.id, text=delta))

        await self.history.append(query.session, "user", sent)
        await self.history.append(query.session, "assistant", answer)
        await self.store.append("ask", text=sent,
                                meta={"answer": answer, "chain": "remote",
                                      "escalated": text is not None})
        await conn.send(wire.Done(id=query.id, chain="remote", model=self.cfg.remote_model))

    async def _call_tool(
        self, name: str, args: dict[str, Any], conn: ClientConn,
        query: wire.Query, state: Any,
    ) -> str:
        """Execute one frontier-requested tool locally, returning its raw output.

        The escalation redaction wraps this in ``_run_remote`` — keep this focused
        on dispatch so the scrub lives in exactly one place."""
        if name == "nixadmin_rebuild":
            return await self.safety.rebuild(
                args.get("action", ""), state=state,
                confirm=lambda text: conn.confirm(query.id, text),
            )
        cmd = self._tool_cmds.get(name)
        if cmd is None:
            return f"(unknown tool: {name})"
        from nixadmin.prefetch import _run_blocking
        return await asyncio.to_thread(_run_blocking, cmd, 15)

    # ---- readiness + broadcast ------------------------------------------- #

    async def _readiness_loop(self) -> None:
        if not self.cfg.has_local:
            return
        delay = 2.0
        while True:
            ready = await local_llm.is_ready(self.cfg.local_url, self.cfg.local_model)
            if ready and not self.local_ready:
                self.local_ready = True
                self._local_ready_evt.set()
                await self._broadcast_ready("local")
                log.info("local chain ready")
            elif not ready and self.local_ready:
                self.local_ready = False
                self._local_ready_evt.clear()
            await asyncio.sleep(delay if not self.local_ready else 30.0)

    async def _event_prune_loop(self) -> None:
        while True:
            await asyncio.sleep(EVENT_PRUNE_INTERVAL)
            await self._prune_events()

    async def _watchdog_loop(self) -> None:
        interval = watchdog_interval()
        if interval is None:
            return
        while True:
            await asyncio.sleep(interval)
            notify("WATCHDOG=1")

    async def _prune_events(self) -> None:
        cutoff = time.time() - self.cfg.event_retention_days * 24 * 60 * 60
        removed = await self.store.prune(cutoff)
        if removed:
            log.info("expired old events", removed=removed,
                     retention_days=self.cfg.event_retention_days)

    async def _wait_local_ready(self) -> None:
        await self._local_ready_evt.wait()

    async def _broadcast(self, source: str, severity: str, text: str) -> None:
        await self.store.append("monitor_event", severity=severity, text=text,
                                meta={"source": source})
        await self._send_event(source, severity, text)

    async def _send_event(self, source: str, severity: str, text: str) -> None:
        """Fan an Event out to connected clients (no store write of its own)."""
        await self._send_all(wire.Event(source=source, severity=severity, text=text))

    # ---- autofix ---------------------------------------------------------- #

    async def _autofix_loop(self) -> None:
        """Poll for failed units and act. Poll-driven (not purely event-driven) on
        purpose: the ``services`` monitor only sees the *system* bus, so a poll of
        ``failed_units()`` is what catches **user**-scope failures too, and it's
        what observes *recovery* so a healed unit is re-armed for its next failure."""
        while True:
            await asyncio.sleep(AUTOFIX_POLL_INTERVAL)
            try:
                await self._run_autofix()
            except Exception as e:  # noqa: BLE001 — a bad tick must not kill the loop
                log.warning("autofix loop tick failed", error=str(e))

    async def _run_autofix(self) -> None:
        """Act on newly-failed units. One action per failure episode; the event
        store's history is the cross-episode restart-loop guard."""
        async with self._autofix_lock:
            units = await remediation.failed_units()
            failed = {(u["unit"], u["scope"]): u for u in units}
            # A recovered unit's episode is over → forget it so a future failure is
            # handled fresh.
            self._autofix_seen &= set(failed)
            for key, u in failed.items():
                if key in self._autofix_seen:
                    continue
                self._autofix_seen.add(key)
                try:
                    await self._autofix_unit(u["unit"], u["scope"])
                except NixadminError as e:
                    log.warning("autofix failed", unit=u["unit"], error=str(e))

    async def _autofix_unit(self, unit: str, scope: str) -> None:
        since = time.time() - self.autofix_cfg.window_s
        prior = await self.store.recent(50, unit=unit, kind="autofix", since=since)
        attempts = sum(1 for e in prior if e.get("meta", {}).get("action") == "restart")
        decision = autofix.decide(
            scope=scope, prior_attempts=attempts, cfg=self.autofix_cfg
        )
        if decision == "skip":
            return
        if decision == "inform":
            text = (
                f"{unit} keeps failing and needs a real fix."
                if attempts else f"{unit} stopped and needs attention."
            )
            await self.store.append("autofix", unit=unit, scope=scope,
                                    severity="warning", text=text, meta={"action": "inform"})
            await self._send_event("autofix", "warning", text)
            return

        # decision == "restart": act, verify, record. The outcome's ok/message come
        # from restart_resolved's single post-restart check — consistent by
        # construction (no second failed_units() snapshot that could disagree).
        log.info("autofix restart", unit=unit, scope=scope, attempt=attempts + 1)
        outcome = await remediation.restart_resolved(
            unit, scope, status=_silent_status, restart_system=self.safety.apply_restart,
        )
        result = "healthy" if outcome.ok else "still_failing"
        await self.store.append(
            "autofix", unit=unit, scope=scope,
            severity="info" if outcome.ok else "warning",
            text=outcome.message,
            meta={"action": "restart", "outcome": result, "attempt": attempts + 1},
        )
        if outcome.ok:
            await self._send_event(
                "autofix", "info", f"{unit} stopped, so I restarted it — it's healthy again.")
        else:
            await self._send_event(
                "autofix", "warning",
                f"{unit} stopped and a restart didn't fix it — it needs attention.")

    async def _broadcast_ready(self, chain: str) -> None:
        await self._send_all(wire.Ready(chain=chain))

    async def _send_all(self, msg: wire.Message) -> None:
        async def send(conn: ClientConn) -> None:
            try:
                await conn.send(msg)
            except Exception:  # noqa: BLE001
                await self._close_connection(conn)

        await asyncio.gather(*(send(conn) for conn in tuple(self.conns)))


async def _serve(daemon: Daemon) -> None:
    try:
        await daemon.run()
    finally:
        await daemon.aclose()


def main() -> None:
    config = Config.from_env()
    logmod.configure(config.log_format, config.log_level)
    daemon = Daemon(config)
    try:
        asyncio.run(_serve(daemon))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
