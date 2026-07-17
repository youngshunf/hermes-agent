"""HuanXing: GET /v1/mcp/status + POST /v1/mcp/reconnect gateway handlers.

Exercises the two api_server handlers that the daemon proxies for the agent
detail page's "MCP 连接状态" card and "重新连接" button. The underlying
``tools.mcp_tool`` functions are covered by
``tests/tools/test_mcp_huanxing_resilience.py``; here we only verify the
handlers wire auth → mcp_tool → JSON response correctly.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def _adapter():
    # No API key configured → _check_auth passes (auth itself is covered by the
    # adapter's _check_auth tests); we exercise the MCP handler logic only.
    return APIServerAdapter(PlatformConfig(enabled=True))


def _multiplex_adapter():
    """带 gateway_runner 的 adapter——路由表的 /p/{profile} 镜像需要它。"""
    adapter = APIServerAdapter(PlatformConfig(enabled=True))

    class _Runner:
        config = GatewayConfig(multiplex_profiles=True)

    adapter.gateway_runner = _Runner()
    return adapter


class TestMcpRoutesAreRegistered:
    """路由注册守卫（唤星·合并回归闸）。

    上面的 handler 测试只证明「函数还在、逻辑对」，**证明不了这两条路由还挂在
    路由表上**。上游 2026-07 把路由注册重构成 ``_http_route_table()``，正好撞上
    我们嫁接的这两条——若下次合并只留 handler、漏了嫁接，handler 测试照样全绿，
    但 daemon 的 ``/api/v1/agents/{id}/mcp-status|reconnect`` 会 404、
    分身详情页的「MCP 连接状态」卡与「重新连接」按钮直接哑掉。故单独钉死注册。
    """

    def test_route_table_includes_mcp_status_and_reconnect(self):
        adapter = _multiplex_adapter()
        table = adapter._http_route_table()
        assert ("GET", "/v1/mcp/status") in {(m, p) for m, p, _h in table}
        assert ("POST", "/v1/mcp/reconnect") in {(m, p) for m, p, _h in table}

    def test_route_table_binds_our_handlers(self):
        """路由指向的必须是唤星的 handler，不是同名占位。"""
        adapter = _multiplex_adapter()
        bound = {(m, p): h for m, p, h in adapter._http_route_table()}
        assert bound[("GET", "/v1/mcp/status")] == adapter._handle_mcp_status
        assert bound[("POST", "/v1/mcp/reconnect")] == adapter._handle_mcp_reconnect

    def test_mcp_routes_get_profile_mirrors(self):
        """connect() 给路由表里每条路径都镜像一份 /p/{profile}/… —— 两条 MCP
        路由进表后白拿多 profile 支持，这里钉死它不被 drop。"""
        adapter = _multiplex_adapter()
        mirrored = {f"/p/{{profile}}{path}" for _m, path, _h in adapter._http_route_table()}
        assert "/p/{profile}/v1/mcp/status" in mirrored
        assert "/p/{profile}/v1/mcp/reconnect" in mirrored


@pytest.mark.asyncio
async def test_handle_mcp_status_returns_servers(monkeypatch):
    import tools.mcp_tool as mcp_tool

    fake = [{
        "name": "cloud", "transport": "http", "tools": 0, "connected": False,
        "retrying": True, "circuit_open": False, "error": "boom",
    }]
    monkeypatch.setattr(mcp_tool, "get_mcp_status", lambda: fake)

    resp = await _adapter()._handle_mcp_status(MagicMock())
    assert resp.status == 200
    payload = json.loads(resp.body)
    assert payload["servers"] == fake


@pytest.mark.asyncio
async def test_handle_mcp_reconnect_all_when_no_body(monkeypatch):
    import tools.mcp_tool as mcp_tool

    calls = {}

    def fake_reconnect(name=None):
        calls["name"] = name
        return [{"name": "cloud", "connected": True}]

    monkeypatch.setattr(mcp_tool, "reconnect_mcp_servers", fake_reconnect)

    req = MagicMock()
    req.can_read_body = False
    resp = await _adapter()._handle_mcp_reconnect(req)
    assert resp.status == 200
    payload = json.loads(resp.body)
    assert calls["name"] is None
    assert payload["reconnected"] == "all"
    assert payload["servers"][0]["connected"] is True


@pytest.mark.asyncio
async def test_handle_mcp_reconnect_targets_named_server(monkeypatch):
    import tools.mcp_tool as mcp_tool

    calls = {}

    def fake_reconnect(name=None):
        calls["name"] = name
        return []

    monkeypatch.setattr(mcp_tool, "reconnect_mcp_servers", fake_reconnect)

    req = MagicMock()
    req.can_read_body = True
    req.json = AsyncMock(return_value={"server": "cloud"})
    resp = await _adapter()._handle_mcp_reconnect(req)
    assert resp.status == 200
    payload = json.loads(resp.body)
    assert calls["name"] == "cloud"
    assert payload["reconnected"] == "cloud"
