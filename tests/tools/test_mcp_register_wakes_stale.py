"""New sessions must wake parked/stale cached MCP servers immediately.

Regression for #50170: after a keepalive failure parks a server, its tools
are deregistered — so a NEW agent session starting up saw the tools silently
absent and had no way to trigger recovery until the next timed self-probe
(up to _PARKED_RETRY_INTERVAL later). register_mcp_servers now nudges any
cached entry whose session is None via _signal_reconnect.
"""

import threading
import time

import pytest


@pytest.mark.no_isolate
def test_register_wakes_stale_cached_server(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool

    woken: list[str] = []

    class _Event:
        def __init__(self, name):
            self._name = name

        def set(self):
            woken.append(self._name)

    class _Stale:
        session = None

        def __init__(self, name):
            self.name = name
            self._reconnect_event = _Event(name)
            self._registered_tool_names: list[str] = []

    class _Alive:
        session = object()

        def __init__(self, name):
            self.name = name
            self._reconnect_event = _Event(name)
            self._registered_tool_names = [f"{name}__tool"]

    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    # 唤醒之后还要等它就绪（见下一个用例）。这个用例的 stale server 永远不会醒，
    # 把等待上界钉成 0 秒，免得每次跑单测都白等一个真实的 `_STALE_REVIVAL_WAIT_SECONDS`。
    monkeypatch.setattr(mcp_tool, "_STALE_REVIVAL_WAIT_SECONDS", 0.0)
    stale = _Stale("parked-srv")
    alive = _Alive("healthy-srv")
    monkeypatch.setitem(mcp_tool._servers, "parked-srv", stale)
    monkeypatch.setitem(mcp_tool._servers, "healthy-srv", alive)

    try:
        result = mcp_tool.register_mcp_servers({
            "parked-srv": {"url": "http://127.0.0.1:9/mcp"},
            "healthy-srv": {"url": "http://127.0.0.1:9/mcp"},
        })
        # Both cached → no new connections attempted; existing names returned.
        assert "healthy-srv__tool" in result
        # The parked (session=None) entry got a reconnect nudge; the healthy
        # one was left alone.
        assert woken == ["parked-srv"]
    finally:
        mcp_tool._servers.pop("parked-srv", None)
        mcp_tool._servers.pop("healthy-srv", None)


@pytest.mark.no_isolate
def test_register_waits_for_woken_stale_server_before_returning(monkeypatch, tmp_path):
    """唤醒之后必须**等**它就绪，否则这一轮的工具表照样没有它的工具。

    工具是在一轮开头快照进 registry 的。此前 `register_mcp_servers` 只 set 了 reconnect
    event 就立即返回，重建落在快照之后——主人这一轮拿到的仍是缺工具的表，分身只能拿本地
    通道去猜云端工具名。本用例钉住「返回时该 server 已经就绪」。
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool

    class _Revivable:
        """被唤醒后**过一会儿**才拿到 session 的 server。

        重建必须是异步且有耗时的，否则这个用例证伪不了任何东西：`_signal_reconnect` 在
        没有 MCP loop 时会直接同步 `event.set()`，若 `set()` 里就把 session 装好，那么
        删掉等待那一行测试照样绿（假绿）。用后台线程延迟 0.3s 还原真实时序。
        """

        def __init__(self, name):
            self.name = name
            self.session = None
            self._registered_tool_names: list[str] = []
            self._reconnect_event = self
            self.woken = False

        def set(self):
            self.woken = True

            def _finish_rebuild():
                time.sleep(0.3)
                self.session = object()
                self._registered_tool_names = [f"{self.name}__tool"]

            threading.Thread(target=_finish_rebuild, daemon=True).start()

    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    monkeypatch.setattr(mcp_tool, "_STALE_REVIVAL_WAIT_SECONDS", 2.0)
    srv = _Revivable("parked-srv")
    monkeypatch.setitem(mcp_tool._servers, "parked-srv", srv)

    try:
        result = mcp_tool.register_mcp_servers({
            "parked-srv": {"url": "http://127.0.0.1:9/mcp"},
        })
        assert srv.woken, "stale server 必须被唤醒"
        # 关键断言：register 返回时 session 已就绪，且工具进了返回的名单——
        # 「只唤醒不等」时 session 仍是 None、工具不在名单里，本行即红。
        assert srv.session is not None
        assert "parked-srv__tool" in result
    finally:
        mcp_tool._servers.pop("parked-srv", None)
