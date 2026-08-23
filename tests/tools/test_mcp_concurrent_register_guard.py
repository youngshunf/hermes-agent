"""并发 `register_mcp_servers` 不得为同一个 server 起两条 supervisor（唤星补丁）。

`register_mcp_servers` 的锁临界区只覆盖「算出 new_servers」，而 `_servers[k] = server`
发生在锁外的异步 `_discover_and_register_server`。两个调用者——每轮对话的
`discover_mcp_tools`，和 daemon 自愈触发的 `reconnect_mcp_servers`——落在那段窗口里
就会各自算出「这个 server 还没连」，各起一条重连退避循环，互相把对方刚建好的会话拆掉。

生产实测（2026-08-21，云端 `cloud` server）：同一秒内三条
`connection lost (attempt 2/5)` 各带 16s / 2s / 60s 三个不同退避——单个 server 对象的
`_reconnect_retries` 不可能同时是三个值，那就是三条独立循环。

去重判据因此必须同时排掉 `_server_connecting`（「已有人在建、还没写进 `_servers`」）。
而一旦它进了判据，标记泄漏就从「状态卡多显示一会儿 connecting」升级成「这台 server
再也不会被重建」，所以清理兜底与判据是同一改动的两半，两个用例各钉一半。
"""

import pytest


@pytest.mark.no_isolate
def test_register_skips_server_already_being_connected(monkeypatch, tmp_path):
    """`_server_connecting` 里已有的名字不得再起一条连接。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool

    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)

    loop_started: list[bool] = []
    monkeypatch.setattr(
        mcp_tool, "_ensure_mcp_loop", lambda: loop_started.append(True)
    )

    # 模拟「另一个调用者已经认领了 cloud，但还没写进 _servers」的窗口。
    mcp_tool._server_connecting.add("cloud")
    try:
        mcp_tool.register_mcp_servers({"cloud": {"url": "http://127.0.0.1:9/mcp"}})
        # 判据生效 → new_servers 为空 → 提前返回，连 MCP loop 都不会起。
        assert loop_started == [], (
            "cloud 已在 _server_connecting 里，不得再起第二条 supervisor"
        )
        # 且不能把别人的认领标记顺手清掉（那会让下一个调用者又建一条）。
        assert "cloud" in mcp_tool._server_connecting
    finally:
        mcp_tool._server_connecting.discard("cloud")


@pytest.mark.no_isolate
def test_register_clears_connecting_marker_when_discovery_raises(monkeypatch, tmp_path):
    """discovery 超时/抛异常时，本次认领的标记必须被摘掉。

    `_discover_all` 内部对每个结果都 discard 过，但那段代码在
    `_run_on_mcp_loop(..., timeout=120)` 超时或抛异常时根本走不到。标记残留 +
    判据里认它 = 那台 server 永久不可重建——比原来的并发 supervisor 更糟。
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool

    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    monkeypatch.setattr(mcp_tool, "_ensure_mcp_loop", lambda: None)

    def _boom(*_args, **_kwargs):
        raise TimeoutError("discovery timed out")

    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop", _boom)

    assert "cloud" not in mcp_tool._server_connecting
    with pytest.raises(TimeoutError):
        mcp_tool.register_mcp_servers({"cloud": {"url": "http://127.0.0.1:9/mcp"}})

    assert "cloud" not in mcp_tool._server_connecting, (
        "discovery 异常退出后 connecting 标记必须清掉，否则这台 server 永久不可重建"
    )
