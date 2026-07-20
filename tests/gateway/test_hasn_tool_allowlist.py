"""唤星会话级工具白名单的 Hermes 入口契约测试。"""

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.hasn_session import (
    RESERVED_ALLOWED_TOOL_NAMES_ARG,
    is_hasn_tool_allowed,
    parse_allowed_tool_names,
    reset_hasn_allowed_tool_names,
    set_hasn_allowed_tool_names,
    stamp_allowed_tool_names_arg,
)
from gateway.platforms.api_server import (
    APIServerAdapter,
    restrict_agent_tools_for_hasn_session,
)
from tools.mcp_tool import _make_tool_handler, mcp_prefixed_tool_name


@pytest.fixture(autouse=True)
def _clear_allowed_tool_names():
    """每个用例前后清空任务局部白名单，避免线程上下文串味。"""
    token = set_hasn_allowed_tool_names(None)
    try:
        yield
    finally:
        reset_hasn_allowed_tool_names(token)


def _tool(name: str) -> dict:
    """构造与真实 OpenAI function tool 契约一致的最小工具定义。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_parse_allowed_tool_names_preserves_exact_order_and_empty_list():
    assert parse_allowed_tool_names([]) == ()
    assert parse_allowed_tool_names(
        ["hasn.session.ask", "hasn.project.create"]
    ) == ("hasn.session.ask", "hasn.project.create")


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (None, "array"),
        ("hasn.session.ask", "array"),
        (["hasn.session.ask", "hasn.session.ask"], "duplicate"),
        ([""], "canonical"),
        (["Hasn.session.ask"], "canonical"),
        (["hasn/session/ask"], "canonical"),
    ],
)
def test_parse_allowed_tool_names_rejects_invalid_policy(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_allowed_tool_names(raw)


def test_allowed_tool_names_are_authoritatively_stamped_for_hasn_servers():
    token = set_hasn_allowed_tool_names(
        ("hasn.session.ask", "hasn.project.create")
    )
    try:
        original = {RESERVED_ALLOWED_TOOL_NAMES_ARG: ["forged.tool"]}
        for server_name in ("hasn", "cloud"):
            stamped = stamp_allowed_tool_names_arg(server_name, original)
            assert stamped[RESERVED_ALLOWED_TOOL_NAMES_ARG] == [
                "hasn.session.ask",
                "hasn.project.create",
            ]
        assert original[RESERVED_ALLOWED_TOOL_NAMES_ARG] == ["forged.tool"]
    finally:
        reset_hasn_allowed_tool_names(token)


def test_missing_policy_strips_forged_reserved_arg_only_for_hasn_servers():
    forged = {RESERVED_ALLOWED_TOOL_NAMES_ARG: ["forged.tool"], "value": 1}

    local = stamp_allowed_tool_names_arg("hasn", forged)
    assert RESERVED_ALLOWED_TOOL_NAMES_ARG not in local
    assert local["value"] == 1

    assert stamp_allowed_tool_names_arg("third-party", forged) is forged


def test_restricted_session_only_allows_wrappers_and_listed_business_tools():
    token = set_hasn_allowed_tool_names(
        ("hasn.session.ask", "hasn.project.create")
    )
    try:
        assert is_hasn_tool_allowed("hasn", "hasn.local.tool.search")
        assert is_hasn_tool_allowed("cloud", "hasn.cloud.tool.call")
        assert is_hasn_tool_allowed("hasn", "hasn.session.ask")
        assert is_hasn_tool_allowed("cloud", "hasn.project.create")
        assert not is_hasn_tool_allowed("hasn", "hasn.asset.delete")
        assert not is_hasn_tool_allowed("third-party", "filesystem.read")
    finally:
        reset_hasn_allowed_tool_names(token)


def test_agent_tool_surface_is_reduced_to_wrappers_and_listed_business_tools():
    allowed = ("hasn.session.ask", "hasn.project.create")
    kept_names = {
        mcp_prefixed_tool_name("hasn", "hasn.local.tool.search"),
        mcp_prefixed_tool_name("hasn", "hasn.local.tool.call"),
        mcp_prefixed_tool_name("cloud", "hasn.cloud.tool.search"),
        mcp_prefixed_tool_name("cloud", "hasn.cloud.tool.call"),
        mcp_prefixed_tool_name("hasn", "hasn.session.ask"),
        mcp_prefixed_tool_name("cloud", "hasn.project.create"),
    }
    removed_names = {
        "terminal",
        mcp_prefixed_tool_name("hasn", "hasn.asset.delete"),
        mcp_prefixed_tool_name("third-party", "filesystem.read"),
    }
    agent = SimpleNamespace(
        tools=[_tool(name) for name in sorted(kept_names | removed_names)],
        valid_tool_names=kept_names | removed_names,
    )

    restrict_agent_tools_for_hasn_session(agent, allowed)

    assert agent.valid_tool_names == kept_names
    assert {
        tool["function"]["name"] for tool in agent.tools
    } == kept_names
    assert agent._session_tool_name_allowlist >= frozenset(kept_names)
    assert agent._session_tool_name_allowlist.isdisjoint(removed_names)


def test_mcp_handler_rejects_unlisted_target_before_network_access():
    token = set_hasn_allowed_tool_names(("hasn.session.ask",))
    try:
        handler = _make_tool_handler("hasn", "hasn.asset.delete", 1.0)
        result = json.loads(handler({"asset_id": "asset-1"}))
    finally:
        reset_hasn_allowed_tool_names(token)

    assert result == {
        "code": "MCP_9206",
        "error": "ToolNotAllowed",
        "tool_name": "hasn.asset.delete",
    }


@pytest.mark.asyncio
async def test_runs_rejects_invalid_allowlist_before_creating_agent():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    app = web.Application()
    app.router.add_post("/v1/runs", adapter._handle_runs)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/runs",
            json={
                "input": "创建平台项目",
                "allowed_tool_names": ["third.party"],
            },
        )
        payload = await response.json()

    assert response.status == 400
    assert payload["error"]["code"] == "invalid_allowed_tool_names"
