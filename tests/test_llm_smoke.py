"""Smoke tests for LLM backends — pure helpers only (no network)."""

from __future__ import annotations

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
