"""唤星派发级能力域收紧（doc20-tools D-2）的 Hermes 入口契约测试。

跨仓契约：hasn-node 在 run payload 里下发 ``scope_overrides``，Hermes 把它盖成每次
出站 MCP 调用的保留参数 ``_hasn_scope_overrides``，daemon 的 G5 门据此在 Agent 三态
之上再收一层。**线格式必须与 Rust 侧逐字节对齐**，故本文件把键名与取值集合钉死。
"""

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import tools.mcp_tool as mcp_tool
from gateway.config import PlatformConfig
from gateway.hasn_session import (
    HASN_SCOPE_OVERRIDE_MODES,
    RESERVED_SCOPE_OVERRIDES_ARG,
    get_hasn_scope_overrides,
    parse_scope_overrides,
    reset_hasn_scope_overrides,
    set_hasn_scope_overrides,
    stamp_scope_overrides_arg,
)
from gateway.platforms.api_server import APIServerAdapter


@pytest.fixture(autouse=True)
def _clear_scope_overrides():
    """每个用例前后清空任务局部收紧，避免线程上下文串味。"""
    token = set_hasn_scope_overrides(None)
    try:
        yield
    finally:
        reset_hasn_scope_overrides(token)


# ── 线格式对齐（与 hasn-node 逐字节核对）────────────────────────────────────
def test_wire_contract_matches_hasn_node():
    """键名与取值集合是跨仓契约，改一个字节两侧就合不上。

    对照事实源：``crates/hasn-mcp/src/auth.rs::RESERVED_SCOPE_OVERRIDES_ARG``
    与 ``crates/hasn-node/src/session_scope.rs::mode_wire_value``。
    """
    assert RESERVED_SCOPE_OVERRIDES_ARG == "_hasn_scope_overrides"
    assert HASN_SCOPE_OVERRIDE_MODES == frozenset({"ask", "deny"})


# ── 解析（非法必须拒绝，绝不静默忽略）────────────────────────────────────────
def test_parse_scope_overrides_accepts_ask_and_deny():
    assert parse_scope_overrides({}) == {}
    assert parse_scope_overrides(
        {"task:run": "deny", "media:generate": "ask"}
    ) == {"task:run": "deny", "media:generate": "ask"}


def test_parse_scope_overrides_rejects_allow_as_widening():
    """``allow`` 是「放宽」，而放宽在本模型里不存在——必须是契约错误。"""
    with pytest.raises(ValueError, match="narrow"):
        parse_scope_overrides({"task:run": "allow"})


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (None, "object"),
        ([("task:run", "deny")], "object"),
        ("task:run=deny", "object"),
        ({"": "deny"}, "malformed"),
        ({"   ": "deny"}, "malformed"),
        ({"task run": "deny"}, "malformed"),
        ({"task:run\n": "deny"}, "malformed"),
        ({1: "deny"}, "malformed"),
        ({"task:run": "Deny"}, "ask"),
        ({"task:run": " deny"}, "ask"),
        ({"task:run": "maybe"}, "ask"),
        ({"task:run": True}, "ask"),
        ({"task:run": None}, "ask"),
    ],
)
def test_parse_scope_overrides_rejects_invalid_wire(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_scope_overrides(raw)


# ── 绑定（并发 run 互不串扰、调用方改不动绑定值）──────────────────────────────
def test_binding_returns_defensive_copy_and_collapses_empty():
    assert get_hasn_scope_overrides() is None

    token = set_hasn_scope_overrides({"task:run": "deny"})
    try:
        bound = get_hasn_scope_overrides()
        assert bound == {"task:run": "deny"}
        bound["task:run"] = "ask"
        assert get_hasn_scope_overrides() == {"task:run": "deny"}
    finally:
        reset_hasn_scope_overrides(token)

    # 空覆盖与「未下达」同义（hasn-node 侧空表本就省略该键）。
    empty_token = set_hasn_scope_overrides({})
    try:
        assert get_hasn_scope_overrides() is None
    finally:
        reset_hasn_scope_overrides(empty_token)


# ── 盖章（本地 hasn + 云端 cloud 都盖；伪造值一律剥离）────────────────────────
@pytest.mark.parametrize("server_name", ["hasn", "cloud"])
def test_scope_overrides_are_authoritatively_stamped_for_hasn_servers(server_name):
    """两个唤星自有服务都必须盖章。

    云端 ``hasn.task.run_now``（声明 ``task:run``）本就是派发触发点，只盖本地等于工作会话
    经 cloud MCP 仍能触发派发。云端消费方见
    ``backend/app/mcp/trust_gate.py::pop_scope_overrides``。
    """
    token = set_hasn_scope_overrides({"task:run": "deny", "media:generate": "ask"})
    try:
        original = {RESERVED_SCOPE_OVERRIDES_ARG: {"task:run": "allow"}, "value": 1}
        stamped = stamp_scope_overrides_arg(server_name, original)

        assert stamped[RESERVED_SCOPE_OVERRIDES_ARG] == {
            "task:run": "deny",
            "media:generate": "ask",
        }
        assert stamped["value"] == 1
        # 纯函数：不修改入参，模型伪造的 allow 原样留在它自己那份里、并未生效。
        assert original[RESERVED_SCOPE_OVERRIDES_ARG] == {"task:run": "allow"}
    finally:
        reset_hasn_scope_overrides(token)


def test_missing_overrides_strip_forged_reserved_arg():
    forged = {RESERVED_SCOPE_OVERRIDES_ARG: {"task:run": "allow"}, "value": 1}

    # 本次未下达收紧 → 两个唤星自有服务都剥离模型伪造值（分身自填注入无效）。
    for server_name in ("hasn", "cloud"):
        stripped = stamp_scope_overrides_arg(server_name, forged)
        assert RESERVED_SCOPE_OVERRIDES_ARG not in stripped
        assert stripped["value"] == 1

    # 第三方 MCP 原样返回（注入即污染入参 schema）。
    assert stamp_scope_overrides_arg("third-party", forged) is forged


def test_no_overrides_leaves_clean_args_byte_identical():
    """缺省零回归：未下达收紧且入参干净时，出站参数必须是原对象本身。"""
    clean = {"task_id": "t-1"}
    for server_name in ("hasn", "cloud", "third-party"):
        assert stamp_scope_overrides_arg(server_name, clean) is clean


# ── 调用链（盖章早于任何网络访问）──────────────────────────────────────────
def test_mcp_handler_stamps_scope_overrides_before_connecting(monkeypatch):
    """MCP handler 真的调用了打标器，且盖章发生在连接查找之前。"""
    captured = {}
    real_stamp = mcp_tool._stamp_hasn_scope_overrides_arg
    assert real_stamp is stamp_scope_overrides_arg  # 条件 import 真的接上了

    def _spy(server_name, args):
        stamped = real_stamp(server_name, args)
        captured["server_name"] = server_name
        captured["args"] = stamped
        return stamped

    monkeypatch.setattr(mcp_tool, "_stamp_hasn_scope_overrides_arg", _spy)

    token = set_hasn_scope_overrides({"task:run": "deny"})
    try:
        handler = mcp_tool._make_tool_handler("hasn", "hasn.task.run_now", 1.0)
        result = json.loads(handler({"task_id": "t-1"}))
    finally:
        reset_hasn_scope_overrides(token)
        mcp_tool._server_error_counts.pop("hasn", None)
        mcp_tool._server_breaker_opened_at.pop("hasn", None)

    assert captured["server_name"] == "hasn"
    assert captured["args"][RESERVED_SCOPE_OVERRIDES_ARG] == {"task:run": "deny"}
    # 测试进程里没有连上的 MCP server：这一步在盖章之后才发生，证明盖章不依赖网络。
    assert "not connected" in result["error"]


def test_mcp_handler_stamps_scope_overrides_for_cloud_dispatch_tool(monkeypatch):
    """云端派发触发工具走的正是 ``cloud`` 服务——盖章必须覆盖它，否则云端那一半门禁不存在。

    ``hasn.task.run_now`` 是云端 MCP 平台工具（``backend/app/mcp/tools/task.py``，声明
    ``task:run``）；云端在 ``server.call_tool`` 里剥离本保留参数并在 G5 与 Agent 三态取更严者。
    """
    captured = {}
    real_stamp = mcp_tool._stamp_hasn_scope_overrides_arg

    def _spy(server_name, args):
        stamped = real_stamp(server_name, args)
        captured["args"] = stamped
        return stamped

    monkeypatch.setattr(mcp_tool, "_stamp_hasn_scope_overrides_arg", _spy)

    token = set_hasn_scope_overrides({"task:run": "deny"})
    try:
        handler = mcp_tool._make_tool_handler("cloud", "hasn.task.run_now", 1.0)
        result = json.loads(handler({"task_id": "t-1"}))
    finally:
        reset_hasn_scope_overrides(token)
        mcp_tool._server_error_counts.pop("cloud", None)
        mcp_tool._server_breaker_opened_at.pop("cloud", None)

    assert captured["args"][RESERVED_SCOPE_OVERRIDES_ARG] == {"task:run": "deny"}
    assert "not connected" in result["error"]


def test_mcp_handler_fails_closed_when_stamping_raises(monkeypatch):
    """收紧盖章异常必须失败关闭——静默放行等于本次派发的能力域门禁整条失效。"""

    def _boom(server_name, args):
        raise RuntimeError("stamp exploded")

    monkeypatch.setattr(mcp_tool, "_stamp_hasn_scope_overrides_arg", _boom)

    token = set_hasn_scope_overrides({"task:run": "deny"})
    try:
        handler = mcp_tool._make_tool_handler("hasn", "hasn.task.run_now", 1.0)
        result = json.loads(handler({"task_id": "t-1"}))
    finally:
        reset_hasn_scope_overrides(token)

    assert result == {
        "code": "MCP_9206",
        "error": "ToolNotAllowed",
        "tool_name": "hasn.task.run_now",
    }


def test_mcp_handler_without_overrides_survives_stamping_failure(monkeypatch):
    """无收紧的 run（standalone 独立模式）不受影响：只剥离伪造值，照常继续。"""

    def _boom(server_name, args):
        raise RuntimeError("stamp exploded")

    monkeypatch.setattr(mcp_tool, "_stamp_hasn_scope_overrides_arg", _boom)

    handler = mcp_tool._make_tool_handler("hasn", "hasn.task.run_now", 1.0)
    try:
        result = json.loads(
            handler({"task_id": "t-1", RESERVED_SCOPE_OVERRIDES_ARG: {"task:run": "allow"}})
        )
    finally:
        mcp_tool._server_error_counts.pop("hasn", None)
        mcp_tool._server_breaker_opened_at.pop("hasn", None)

    assert "not connected" in result["error"]


# ── 入口（非法收紧在创建 agent 之前就 400）───────────────────────────────────
@pytest.mark.asyncio
async def test_runs_rejects_invalid_scope_overrides_before_creating_agent():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    app = web.Application()
    app.router.add_post("/v1/runs", adapter._handle_runs)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/runs",
            json={
                "input": "去办这件事",
                "scope_overrides": {"task:run": "allow"},
            },
        )
        payload = await response.json()

    assert response.status == 400
    assert payload["error"]["code"] == "invalid_scope_overrides"
