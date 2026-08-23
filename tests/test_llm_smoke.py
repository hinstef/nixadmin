"""Smoke tests for LLM backends — pure helpers only (no network)."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from nixadmin.errors import BackendError
from nixadmin.llm import local, remote
from nixadmin.sdk import SPEC_VERSION, Fetcher, Module


def _modules():
    return [
        Module(spec_version=SPEC_VERSION, name="apps", description="installed apps software",
               fetchers=[Fetcher(name="list", cmd="nixadmin-apps",
                                 description="installed apps", expose_as_tool=True)]),
        Module(spec_version=SPEC_VERSION, name="disk", description="disk storage space",
               fetchers=[Fetcher(name="usage", cmd="df -h")]),  # not exposed
    ]


def test_classify_prompt_lists_modules():
    prompt = local.build_classify_prompt("what apps?", _modules())
    assert "apps:" in prompt and "disk:" in prompt and "what apps?" in prompt


def test_parse_classify_maps_names_to_modules():
    matched = local.parse_classify_response("apps", _modules())
    assert [m.name for m in matched] == ["apps"]


def test_parse_classify_none():
    assert local.parse_classify_response("none", _modules()) == []


def test_augment_injects_context_or_passes_through():
    assert local.augment("q", "") == "q"
    out = local.augment("q", "$ df\n/ 50%")
    assert "Live system data" in out and "df" in out


def test_escalate_prompt_and_parse():
    prompt = local.build_escalate_prompt("what's the best photo editor?")
    assert "LOCAL" in prompt and "ESCALATE" in prompt
    assert "what's the best photo editor?" in prompt
    assert local.parse_escalate_response("ESCALATE") is True
    assert local.parse_escalate_response("escalate — needs the cloud") is True
    assert local.parse_escalate_response("LOCAL") is False
    assert local.parse_escalate_response("") is False           # bias: stay local
    assert local.parse_escalate_response("i think local") is False


def test_redact_prompt_asks_to_strip_pii():
    prompt = local.build_redact_prompt("email me at a@b.com")
    assert "sensitive" in prompt.lower() or "personal" in prompt.lower()
    assert "email me at a@b.com" in prompt


def test_build_tools_only_exposed_fetchers_plus_rebuild():
    tools = remote.build_tools(_modules())
    names = {t["function"]["name"] for t in tools}
    assert "apps_list" in names          # exposed fetcher
    assert "disk_usage" not in names     # not exposed
    assert "nixadmin_rebuild" in names   # built-in always present


def test_rebuild_tool_constrains_action_enum():
    tool = next(t for t in remote.build_tools([]) if t["function"]["name"] == "nixadmin_rebuild")
    assert tool["function"]["parameters"]["properties"]["action"]["enum"] == \
        ["test", "switch", "boot", "revert"]


def test_remote_module_does_not_import_litellm_eagerly():
    code = "import sys; import nixadmin.llm.remote; assert 'litellm' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)


async def test_remote_lazy_import_failure_is_a_backend_error(monkeypatch):
    def missing(_name):
        raise ModuleNotFoundError("litellm missing")

    monkeypatch.setattr(remote, "import_module", missing)

    async def unused_tool(_name, _args):
        return ""

    with pytest.raises(BackendError, match="backend unavailable"):
        async for _ in remote.run(
            "hello", model="missing", api_base=None, tools=[], run_tool=unused_tool,
        ):
            pass


async def test_remote_tool_loop_accumulates_streamed_call_and_returns_answer(monkeypatch):
    calls: list[tuple[str, dict[str, object]]] = []
    requests: list[list[dict[str, object]]] = []

    def chunk(*, content=None, tool_calls=None):
        delta = SimpleNamespace(content=content, tool_calls=tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

    def tool_fragment(index, *, id=None, name=None, arguments=None):
        function = SimpleNamespace(name=name, arguments=arguments)
        return SimpleNamespace(index=index, id=id, function=function)

    streams = [
        [
            chunk(tool_calls=[tool_fragment(0, id="call-1", name="apps_", arguments="{")]),
            chunk(tool_calls=[tool_fragment(0, name="list", arguments="}")]),
        ],
        [chunk(content="Firefox is installed." )],
    ]

    class Stream:
        def __init__(self, chunks):
            self._chunks = iter(chunks)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._chunks)
            except StopIteration as error:
                raise StopAsyncIteration from error

    class FakeLiteLLM:
        async def acompletion(self, **kwargs):
            requests.append(kwargs["messages"])
            return Stream(streams.pop(0))

    async def run_tool(name, args):
        calls.append((name, args))
        return "firefox 128"

    monkeypatch.setattr(remote, "import_module", lambda _name: FakeLiteLLM())
    answer = "".join([
        part async for part in remote.run(
            "is Firefox installed?", model="fake", api_base=None,
            tools=remote.build_tools(_modules()), run_tool=run_tool,
        )
    ])

    assert answer == "Firefox is installed."
    assert calls == [("apps_list", {})]
    assert requests[1][-1] == {
        "role": "tool", "tool_call_id": "call-1", "content": "firefox 128",
    }
