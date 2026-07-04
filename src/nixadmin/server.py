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
from pathlib import Path
from typing import Any, cast

from nixadmin import actions, remediation
from nixadmin import log as logmod
from nixadmin import protocol as wire
from nixadmin.config import Config
from nixadmin.context import ContextCache
from nixadmin.errors import NixadminError
from nixadmin.history import make_history
from nixadmin.llm import local as local_llm
from nixadmin.llm import remote as remote_llm
from nixadmin.monitors import MonitorRunner
from nixadmin.registry import load_modules
from nixadmin.routing import Chain, Decision, detect_mutation, resolve, resolve_desired_chain
from nixadmin.safety import SafetyGate
from nixadmin.sdk import Module
from nixadmin.session import SessionRegistry

log = logmod.get_logger(__name__)

HELPER_SOCKET = "/run/nixadmin-helper.sock"


class ClientConn:
    """One connected client. Owns its writer and pending confirm/input futures."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.pending: dict[str, asyncio.Future[wire.Respond]] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}

    async def send(self, msg: wire.Message) -> None:
        self.writer.write(wire.encode(msg).encode())
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
        server = await asyncio.start_unix_server(self._handle_client, path=sock)
        os.chmod(sock, 0o660)
        log.info("listening", socket=sock, default_chain=self.cfg.default_chain)

        await self.monitors.start()
        asyncio.create_task(self._readiness_loop())
        async with server:
            await server.serve_forever()

    async def aclose(self) -> None:
        await self.monitors.aclose()
        Path(self.cfg.socket_path).unlink(missing_ok=True)  # noqa: ASYNC240 — instant local op

    # ---- connection handling --------------------------------------------- #

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conn = ClientConn(reader, writer)
        self.conns.add(conn)
        await conn.send(wire.Hello(
            chains=self._chains(),
            ready={"local": self.local_ready, "remote": self.remote_ready},
            default_chain=self.cfg.default_chain,
            modules=[m.name for m in self.modules],
        ))
        try:
            async for raw in reader:
                line = raw.decode().strip()
                if not line:
                    continue
                await self._on_message(conn, line)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            self.conns.discard(conn)
            with contextlib.suppress(Exception):
                writer.close()

    async def _on_message(self, conn: ClientConn, line: str) -> None:
        try:
            msg = wire.decode(line)
        except NixadminError as e:
            log.warning("dropping malformed message", error=str(e))
            return

        if isinstance(msg, wire.Query):
            task = asyncio.create_task(self._dispatch(conn, msg))
            conn.tasks[msg.id] = task
        elif isinstance(msg, wire.Cancel):
            pending = conn.tasks.get(msg.id)
            if pending:
                pending.cancel()
        elif isinstance(msg, wire.Respond):
            conn.deliver_response(msg)
        elif isinstance(msg, wire.ListFailures):
            asyncio.create_task(self._list_failures(conn, msg))
        elif isinstance(msg, wire.RestartUnit):
            asyncio.create_task(self._restart_unit(conn, msg))
        elif isinstance(msg, wire.ExplainUnit):
            asyncio.create_task(self._explain_unit(conn, msg))
        elif isinstance(msg, wire.UnitJournal):
            asyncio.create_task(self._unit_journal(conn, msg))

    async def _list_failures(self, conn: ClientConn, msg: wire.ListFailures) -> None:
        """Structured current failures for a client to render actions from."""
        units = await remediation.failed_units()
        await conn.send(wire.Failures(id=msg.id, units=units))

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
            result = await remediation.restart_resolved(
                match["unit"], match["scope"],
                status=lambda text: conn.send(wire.Status(id=msg.id, text=text)),
                restart_system=self.safety.apply_restart,
            )
            await conn.send(wire.Delta(id=msg.id, text=result))
            await conn.send(wire.Done(id=msg.id, chain="local", model=self.cfg.local_model))
        except NixadminError as e:
            await conn.send(wire.Error(id=msg.id, text=str(e)))

    async def _unit_journal(self, conn: ClientConn, msg: wire.UnitJournal) -> None:
        """Recent journal lines for a unit (read-only detail for the web view)."""
        scope = msg.scope if msg.scope in ("system", "user") else "system"
        text = await remediation.unit_journal(msg.unit, scope)
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
            async for delta in local_llm.summarize(
                message, model=self.cfg.local_model, url=self.cfg.local_url
            ):
                await conn.send(wire.Delta(id=msg.id, text=delta))
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
            # Open-ended change → remote agent (or a plain limitation).
            await self._handle_mutation(conn, query, desired, pinned, matched)
            return

        decision = resolve(
            desired=desired, pinned_local=pinned,
            local_ready=self.local_ready, remote_ready=self.remote_ready,
        )
        chain = await self._apply_decision(conn, query, decision)
        if chain is None:
            return
        if chain == "local":
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
        await conn.send(wire.Delta(id=query.id, text=result))
        await conn.send(wire.Done(id=query.id, chain="local", model=self.cfg.local_model))

    async def _handle_mutation(
        self, conn: ClientConn, query: wire.Query, desired: Chain, pinned: bool,
        matched: list[Module],
    ) -> None:
        if not self.remote_ready:
            # Not an error — a plain-language limitation. Making changes needs the
            # remote assistant, which isn't configured on this machine.
            await conn.send(wire.Delta(
                id=query.id,
                text="I can't make changes yet — that needs the full assistant, "
                     "which isn't set up on this machine.",
            ))
            await conn.send(wire.Done(id=query.id, chain="local", model=self.cfg.local_model))
            return
        # Escalate to remote. If the query was pinned local (privacy), the change
        # still needs remote tools, so confirm that it will leave the device.
        if desired == "local":
            if pinned:
                ok = await conn.confirm(
                    query.id,
                    "Making this change needs the full assistant and will leave this "
                    "device. Proceed?",
                )
                if not ok:
                    await conn.send(wire.Done(id=query.id))
                    return
            else:
                await conn.send(wire.Status(
                    id=query.id, text="This needs the full assistant — switching over."))
        await self._run_remote(conn, query)

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
        await conn.send(wire.Done(id=query.id, chain="local", model=self.cfg.local_model))

    async def _run_remote(self, conn: ClientConn, query: wire.Query) -> None:
        tools = remote_llm.build_tools(self.modules)
        history = await self.history.recent(query.session, 20)
        system_extra = "\n\n" + (await self.context.assemble())
        state = self.sessions.state(query.session)

        async def run_tool(name: str, args: dict[str, Any]) -> str:
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

        answer = ""
        async for delta in remote_llm.run(
            query.text, model=self.cfg.remote_model, api_base=self.cfg.remote_base,
            tools=tools, run_tool=run_tool, history=history, system_extra=system_extra,
        ):
            answer += delta
            await conn.send(wire.Delta(id=query.id, text=delta))

        await self.history.append(query.session, "user", query.text)
        await self.history.append(query.session, "assistant", answer)
        await conn.send(wire.Done(id=query.id, chain="remote", model=self.cfg.remote_model))

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

    async def _wait_local_ready(self) -> None:
        await self._local_ready_evt.wait()

    async def _broadcast(self, source: str, severity: str, text: str) -> None:
        msg = wire.Event(source=source, severity=severity, text=text)
        await self._send_all(msg)

    async def _broadcast_ready(self, chain: str) -> None:
        await self._send_all(wire.Ready(chain=chain))

    async def _send_all(self, msg: wire.Message) -> None:
        for conn in list(self.conns):
            try:
                await conn.send(msg)
            except Exception:  # noqa: BLE001
                self.conns.discard(conn)


def main() -> None:
    config = Config.from_env()
    logmod.configure(config.log_format, config.log_level)
    daemon = Daemon(config)
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
