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

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def _adapter():
    # No API key configured → _check_auth passes (auth itself is covered by the
    # adapter's _check_auth tests); we exercise the MCP handler logic only.
    return APIServerAdapter(PlatformConfig(enabled=True))


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
