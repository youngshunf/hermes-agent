"""reconnect_tools 原生工具单测（唤星 HuanXing 新增）。

覆盖：
  - 注册进 registry（名字 / toolset / schema / check_fn 就位）
  - 无 MCP → no_mcp
  - 全在线 → already_online（不触发任何重连）
  - 只重连掉线那条（健康通道不动）→ recovered
  - 重连后仍不可达 → still_unreachable
  - 定向重连不存在的通道 → unknown_channel
  - check_fn：有配置→暴露，无配置/异常→不暴露

全程零 LLM、零真实 MCP：用注入到 ``sys.modules['tools.mcp_tool']`` 的假模块驱动
``get_mcp_status`` / ``reconnect_mcp_servers``（handler 内是懒 import，故可替换）。
"""
import sys
import types

import pytest

import tools.reconnect_tools as rt
from tools.registry import registry


def _fake_mcp_module(status_snapshots, reconnect_calls):
    """构造假的 tools.mcp_tool 模块。

    Args:
        status_snapshots: 可变列表，get_mcp_status() 每次返回其**当前**首元素；
            reconnect_mcp_servers 被调用时弹出首元素，模拟状态随重连推进。
        reconnect_calls: 记录 reconnect_mcp_servers 收到的 name 参数。
    """
    mod = types.ModuleType("tools.mcp_tool")

    def get_mcp_status():
        return status_snapshots[0]

    def reconnect_mcp_servers(name=None):
        reconnect_calls.append(name)
        # 模拟重连推进：若还有后续快照，切到下一张
        if len(status_snapshots) > 1:
            status_snapshots.pop(0)
        return status_snapshots[0]

    mod.get_mcp_status = get_mcp_status
    mod.reconnect_mcp_servers = reconnect_mcp_servers
    return mod


@pytest.fixture
def fast_poll(monkeypatch):
    """把重连等待缩到极短，保证测试秒过。"""
    monkeypatch.setattr(rt, "_RECONNECT_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(rt, "_RECONNECT_POLL_INTERVAL", 0.01)


def test_registered_in_registry():
    """工具已按 fork 约定注册进 registry。"""
    entry = registry.get("reconnect_tools") if hasattr(registry, "get") else None
    # registry 无 get 时回退到内部表
    tools_map = getattr(registry, "_tools", {})
    assert "reconnect_tools" in tools_map
    reg = tools_map["reconnect_tools"]
    assert reg.toolset == "reconnect_tools"
    assert reg.schema["name"] == "reconnect_tools"
    assert reg.check_fn is rt.check_reconnect_requirements


def test_no_mcp_when_status_empty(monkeypatch):
    """没有任何工具通道时诚实返回 no_mcp。"""
    calls = []
    fake = _fake_mcp_module([[]], calls)
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", fake)

    result = rt.reconnect_tools()

    assert result["status"] == "no_mcp"
    assert calls == []  # 未触发任何重连


def test_already_online_skips_reconnect(monkeypatch):
    """两条通道都在线时不重连，返回 already_online。"""
    calls = []
    status = [
        [
            {"name": "hasn", "connected": True, "status": "connected"},
            {"name": "cloud", "connected": True, "status": "connected"},
        ]
    ]
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", _fake_mcp_module(status, calls))

    result = rt.reconnect_tools()

    assert result["status"] == "already_online"
    assert calls == []


def test_reconnects_only_disconnected_channel(monkeypatch, fast_poll):
    """只重连掉线的云端通道，健康的本地通道不动，恢复后 recovered。"""
    calls = []
    # 快照1：cloud 掉线；重连后 → 快照2：cloud 恢复
    snapshots = [
        [
            {"name": "hasn", "connected": True, "status": "connected"},
            {"name": "cloud", "connected": False, "status": "failed", "error": "connect refused"},
        ],
        [
            {"name": "hasn", "connected": True, "status": "connected"},
            {"name": "cloud", "connected": True, "status": "connected"},
        ],
    ]
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", _fake_mcp_module(snapshots, calls))

    result = rt.reconnect_tools()

    assert result["status"] == "recovered"
    # 关键：只重连 cloud，不碰健康的 hasn
    assert calls == ["cloud"]
    assert "云端工具通道" in result["message"]


def test_still_unreachable_when_reconnect_fails(monkeypatch, fast_poll):
    """重连后云端仍不可达 → still_unreachable，诚实告知。"""
    calls = []
    down = [
        [
            {"name": "hasn", "connected": True, "status": "connected"},
            {"name": "cloud", "connected": False, "status": "failed", "error": "timeout"},
        ]
    ]
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", _fake_mcp_module(down, calls))

    result = rt.reconnect_tools()

    assert result["status"] == "still_unreachable"
    assert calls == ["cloud"]


def test_targeted_unknown_channel(monkeypatch):
    """定向重连一个不存在的通道名 → unknown_channel。"""
    calls = []
    status = [[{"name": "hasn", "connected": True, "status": "connected"}]]
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", _fake_mcp_module(status, calls))

    result = rt.reconnect_tools(server="does-not-exist")

    assert result["status"] == "unknown_channel"
    assert calls == []


def test_targeted_reconnect_specific_channel(monkeypatch, fast_poll):
    """定向重连指定通道（即使它当前在线也照重连）。"""
    calls = []
    snapshots = [
        [{"name": "cloud", "connected": True, "status": "connected"}],
        [{"name": "cloud", "connected": True, "status": "connected"}],
    ]
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", _fake_mcp_module(snapshots, calls))

    result = rt.reconnect_tools(server="cloud")

    assert result["status"] == "recovered"
    assert calls == ["cloud"]


def test_check_fn_exposes_only_when_configured(monkeypatch):
    """check_fn：有配置→True 暴露；无配置→False；异常→False。"""
    # 有配置
    mod_yes = types.ModuleType("tools.mcp_tool")
    mod_yes._load_mcp_config = lambda: {"hasn": {}, "cloud": {}}
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", mod_yes)
    assert rt.check_reconnect_requirements() is True

    # 无配置
    mod_no = types.ModuleType("tools.mcp_tool")
    mod_no._load_mcp_config = lambda: {}
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", mod_no)
    assert rt.check_reconnect_requirements() is False

    # 加载异常 → 不暴露
    mod_boom = types.ModuleType("tools.mcp_tool")

    def _boom():
        raise RuntimeError("mcp 子系统缺失")

    mod_boom._load_mcp_config = _boom
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", mod_boom)
    assert rt.check_reconnect_requirements() is False
