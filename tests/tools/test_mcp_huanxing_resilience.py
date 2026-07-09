"""HuanXing MCP resilience patches for tools/mcp_tool.py.

上游（NousResearch/hermes-agent）已独立收敛出等价的 MCP 韧性机制：掉线后
不永久放弃、停泊自探（``_wait_for_reconnect_or_shutdown`` 带 timeout 自愈）。
本测试仅保留唤星**独有**的两块适配面：

  - ``get_mcp_status`` — 在服务器掉线时如实告诉主人「为什么断了」以及后台
    任务是否仍在重试（``error`` / ``retrying`` / ``circuit_open`` 字段）。
  - ``reconnect_mcp_servers`` — 手动 / 守护进程驱动的拆除 + 从磁盘最新配置
    重建（拾取轮换后的凭据），并重置断路器。

上游停泊自探统一返回 ``"reconnect"``（不再是唤星旧补丁的 ``"reprobe"``），
且移除了 ``_sleep_or_wake``；相关测试已按上游契约对齐 / 删除。
"""
import asyncio
import time

import pytest

from tools import mcp_tool
from tools.mcp_tool import MCPServerTask


# --------------------------------------------------------------------------
# _wait_for_reconnect_or_shutdown — parked self-probe (idle network-recovery)
# --------------------------------------------------------------------------
#
# A server that dropped on a network outage / cloud-unreachable and then
# exhausted its reconnect budget parks here. Without a timeout it waits
# forever for an *external* signal (breaker half-open probe, OAuth recovery,
# manual /v1/mcp/reconnect) — but an idle disconnect has none of those, so the
# server stayed permanently wedged even after the network recovered. The
# timeout returns "reconnect" so the run loop rebuilds the transport on its own
# (上游统一命名为 "reconnect"，语义等同唤星旧补丁的 "reprobe" 自探).

@pytest.mark.asyncio
async def test_parked_wait_reprobes_on_timeout_with_no_signal():
    """Timeout with neither event set → "reconnect" (self-heal, no external signal)."""
    task = MCPServerTask("t")
    start = time.monotonic()
    result = await task._wait_for_reconnect_or_shutdown(timeout=0.05)
    assert result == "reconnect"
    assert time.monotonic() - start >= 0.04


@pytest.mark.asyncio
async def test_parked_wait_reconnect_signal_beats_timeout():
    """An explicit reconnect signal returns "reconnect" and clears the event,
    even with a timeout armed — the timeout is only a fallback self-probe."""
    task = MCPServerTask("t")
    task._reconnect_event.set()
    start = time.monotonic()
    result = await task._wait_for_reconnect_or_shutdown(timeout=5.0)
    assert result == "reconnect"
    assert not task._reconnect_event.is_set()
    assert time.monotonic() - start < 1.0


@pytest.mark.asyncio
async def test_parked_wait_shutdown_takes_precedence():
    """Shutdown wins over both timeout and reconnect so teardown is prompt."""
    task = MCPServerTask("t")
    task._shutdown_event.set()
    start = time.monotonic()
    result = await task._wait_for_reconnect_or_shutdown(timeout=5.0)
    assert result == "shutdown"
    assert time.monotonic() - start < 1.0


@pytest.mark.asyncio
async def test_parked_wait_without_timeout_still_blocks_for_signal():
    """Backward-compat: with no timeout the wait blocks until a signal, then
    returns "reconnect" (never "reprobe") — the legacy park-forever contract."""
    task = MCPServerTask("t")

    async def signal_later():
        await asyncio.sleep(0.05)
        task._reconnect_event.set()

    signaller = asyncio.ensure_future(signal_later())
    result = await task._wait_for_reconnect_or_shutdown()
    await signaller
    assert result == "reconnect"


# --------------------------------------------------------------------------
# get_mcp_status — error / retrying surfacing
# --------------------------------------------------------------------------

class _FakeTask:
    def __init__(self, done: bool = False):
        self._done = done

    def done(self) -> bool:
        return self._done


class _FakeServer:
    def __init__(self, session=None, alive: bool = True):
        self.session = session
        self._task = _FakeTask(done=not alive)
        self._registered_tool_names: list = []
        self._tools: list = []
        self._sampling = None
        self.shutdown_called = False

    async def shutdown(self):
        self.shutdown_called = True


def test_get_mcp_status_surfaces_error_and_retrying(monkeypatch):
    monkeypatch.setattr(
        mcp_tool, "_load_mcp_config", lambda: {"cloud": {"url": "http://x/mcp"}}
    )
    monkeypatch.setattr(mcp_tool, "_server_error_counts", {}, raising=False)

    # No live server + a recorded error → disconnected, reason surfaced, and
    # NOT retrying (the task is gone, not in a retry loop).
    monkeypatch.setattr(mcp_tool, "_servers", {}, raising=False)
    monkeypatch.setattr(mcp_tool, "_server_last_error", {"cloud": "boom"}, raising=False)
    status = {e["name"]: e for e in mcp_tool.get_mcp_status()}
    assert status["cloud"]["connected"] is False
    assert status["cloud"]["error"] == "boom"
    assert status["cloud"]["retrying"] is False
    assert status["cloud"]["circuit_open"] is False

    # Live background task but no session → still retrying (self-heals soon).
    monkeypatch.setattr(
        mcp_tool, "_servers", {"cloud": _FakeServer(session=None, alive=True)},
        raising=False,
    )
    status = {e["name"]: e for e in mcp_tool.get_mcp_status()}
    assert status["cloud"]["connected"] is False
    assert status["cloud"]["retrying"] is True


def test_get_mcp_status_connected_clears_status_fields(monkeypatch):
    monkeypatch.setattr(
        mcp_tool, "_load_mcp_config", lambda: {"cloud": {"url": "http://x/mcp"}}
    )
    monkeypatch.setattr(mcp_tool, "_server_error_counts", {}, raising=False)
    monkeypatch.setattr(mcp_tool, "_server_last_error", {"cloud": "stale"}, raising=False)
    monkeypatch.setattr(
        mcp_tool, "_servers", {"cloud": _FakeServer(session=object())}, raising=False
    )
    status = {e["name"]: e for e in mcp_tool.get_mcp_status()}
    assert status["cloud"]["connected"] is True
    assert status["cloud"]["retrying"] is False
    assert status["cloud"]["error"] is None


# --------------------------------------------------------------------------
# reconnect_mcp_servers — teardown + rebuild from fresh config
# --------------------------------------------------------------------------

def test_reconnect_resets_breaker_and_reregisters(monkeypatch):
    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True, raising=False)
    cfg = {"url": "http://x/mcp"}
    monkeypatch.setattr(mcp_tool, "_load_mcp_config", lambda: {"cloud": cfg})
    monkeypatch.setattr(mcp_tool, "_ensure_mcp_loop", lambda: None)

    def fake_run_on_loop(factory, timeout=30):
        coro = factory() if callable(factory) else factory
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop", fake_run_on_loop)

    spy: dict = {}

    def fake_register(servers):
        spy["servers"] = servers
        return []

    monkeypatch.setattr(mcp_tool, "register_mcp_servers", fake_register)

    fake = _FakeServer(session=object(), alive=True)
    monkeypatch.setattr(mcp_tool, "_servers", {"cloud": fake}, raising=False)
    monkeypatch.setattr(mcp_tool, "_server_error_counts", {}, raising=False)
    monkeypatch.setattr(mcp_tool, "_server_breaker_opened_at", {}, raising=False)
    monkeypatch.setattr(mcp_tool, "_server_last_error", {}, raising=False)

    # Open the circuit breaker.
    for _ in range(mcp_tool._CIRCUIT_BREAKER_THRESHOLD):
        mcp_tool._bump_server_error("cloud")
    assert (
        mcp_tool._server_error_counts["cloud"]
        >= mcp_tool._CIRCUIT_BREAKER_THRESHOLD
    )

    mcp_tool.reconnect_mcp_servers("cloud")

    assert fake.shutdown_called is True                     # torn down
    assert "cloud" not in mcp_tool._servers                 # popped before rebuild
    assert mcp_tool._server_error_counts.get("cloud", 0) == 0  # breaker reset
    assert spy["servers"] == {"cloud": cfg}                 # rebuilt from fresh config


def test_reconnect_unknown_server_is_noop(monkeypatch):
    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True, raising=False)
    monkeypatch.setattr(mcp_tool, "_load_mcp_config", lambda: {"cloud": {"url": "u"}})
    monkeypatch.setattr(mcp_tool, "_servers", {}, raising=False)
    monkeypatch.setattr(mcp_tool, "_server_error_counts", {}, raising=False)
    monkeypatch.setattr(mcp_tool, "_server_last_error", {}, raising=False)
    called = {"register": False}
    monkeypatch.setattr(
        mcp_tool, "register_mcp_servers",
        lambda servers: called.__setitem__("register", True) or [],
    )
    # A name not in config → no register attempt, returns current status.
    out = mcp_tool.reconnect_mcp_servers("does-not-exist")
    assert called["register"] is False
    assert isinstance(out, list)
