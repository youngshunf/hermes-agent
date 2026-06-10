"""HuanXing MCP resilience patches for tools/mcp_tool.py.

Covers the three behaviours added so an agent's cloud/local MCP servers no
longer go permanently dead after a transient drop:

  - ``MCPServerTask._sleep_or_wake`` — interruptible reconnect backoff.
  - ``get_mcp_status`` — surfaces *why* a server is disconnected and whether
    its background task is still retrying.
  - ``reconnect_mcp_servers`` — manual/daemon-driven teardown + rebuild from
    fresh on-disk config (picks up rotated credentials), resets the breaker.
"""
import asyncio
import time

import pytest

from tools import mcp_tool
from tools.mcp_tool import MCPServerTask


# --------------------------------------------------------------------------
# _sleep_or_wake — interruptible backoff
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sleep_or_wake_times_out_returns_false():
    task = MCPServerTask("t")
    start = time.monotonic()
    woken = await task._sleep_or_wake(0.05)
    assert woken is False
    assert time.monotonic() - start >= 0.04


@pytest.mark.asyncio
async def test_sleep_or_wake_wakes_on_reconnect_event():
    task = MCPServerTask("t")
    task._reconnect_event.set()
    start = time.monotonic()
    woken = await task._sleep_or_wake(5.0)
    assert woken is True
    # Consumed so the next backoff cycle starts from a clean signal.
    assert not task._reconnect_event.is_set()
    # Returned promptly instead of waiting out the 5s window.
    assert time.monotonic() - start < 1.0


@pytest.mark.asyncio
async def test_sleep_or_wake_shutdown_returns_promptly_not_woken():
    task = MCPServerTask("t")
    task._shutdown_event.set()
    start = time.monotonic()
    woken = await task._sleep_or_wake(5.0)
    # Shutdown is not a manual reconnect, so the caller must not reset backoff,
    # but teardown must still be prompt (not blocked for the full window).
    assert woken is False
    assert time.monotonic() - start < 1.0


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
