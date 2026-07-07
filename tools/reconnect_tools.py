#!/usr/bin/env python3
"""原生「重连工具通道」工具（唤星 HuanXing 新增）。

分身通过两条 MCP 工具通道调用能力：**本地通道**（hermes → 本机 daemon 的 hasn-mcp）
与**云端通道**（hermes → 云端 backend 的 MCP）。这两条本质上是 hermes-agent 自己持有的
MCP **客户端**连接；断网 / 云端暂时不可达时客户端会掉线，掉线后即使网络恢复，空闲的
客户端也可能停泊干等、不自动重连 —— 表现为分身「调不动工具」。

本工具是分身的**自助恢复**手段：它是一个**原生 hermes 工具**（不走 MCP 通道），所以即使
两条 MCP 通道都掉线，分身依然能调用它。它进程内直接调用
``tools.mcp_tool.reconnect_mcp_servers`` —— 拆掉掉线的客户端、**从磁盘 config 重读凭据重建**、
重置熔断器（与 webui「重新连接」按钮、daemon 健康自愈走的是同一条重建路径），无需经过
daemon 或云端中转。

设计要点：
- **只重连掉线的通道**（``connected=False`` 且非 ``disabled``）——健康的通道完全不动，避免
  无谓打断在跑的连接。也支持传 ``server`` 定向重连某一条。
- **有界短轮询**确认连接是否真正建立后再返回，给出确定结果（已恢复 / 仍不可达），而不是
  含糊地「已触发、请自行重试」。
- **纯 hermes 侧、非破坏**：只重建连接、不重铸凭据、不重启网关。
"""

import logging
import time

logger = logging.getLogger(__name__)

# 通道名 → 面向分身的中文标签（与 webui「MCP 连接状态」卡片口径一致）。未知名回落原名。
_CHANNEL_LABELS = {
    "hasn": "本地工具通道",
    "cloud": "云端工具通道",
}

# 重连后等待连接建立的最长时间（秒）。本地通道是 loopback、near-instant；云端通道取决于
# 网络/后端是否已恢复。有界等待让工具能给出「已恢复」还是「仍不可达」的确定结论。
_RECONNECT_WAIT_SECONDS = 6.0
# 轮询间隔（秒）。
_RECONNECT_POLL_INTERVAL = 0.5


def _channel_label(name: str) -> str:
    """通道名 → 中文标签（未知名回落原名，零 fake）。"""
    return _CHANNEL_LABELS.get(name, name)


def _summarize_channels(status_list) -> list:
    """把 get_mcp_status() 的原始状态收敛成面向分身的精简通道列表。"""
    channels = []
    for server in status_list or []:
        name = server.get("name", "unknown")
        channels.append(
            {
                "name": name,
                "label": _channel_label(name),
                "connected": bool(server.get("connected", False)),
                "status": server.get("status"),
                "error": server.get("error"),
            }
        )
    return channels


def reconnect_tools(server: str = None) -> dict:
    """重连掉线的工具通道（本地 / 云端 MCP），恢复分身的工具调用能力。

    Args:
        server: 可选，定向重连指定通道（如 ``"cloud"`` / ``"hasn"``）。缺省时自动只重连
            当前**已掉线**的通道。

    Returns:
        结构化结果 dict（``status`` / ``message`` / ``channels`` / ``action``）。
    """
    try:
        from tools.mcp_tool import get_mcp_status, reconnect_mcp_servers
    except Exception as exc:  # noqa: BLE001 — MCP 子系统缺失时诚实报错，不伪装成功
        logger.warning("reconnect_tools: 无法加载 MCP 子系统: %s", exc)
        return {
            "status": "no_mcp",
            "message": "当前运行时未启用 MCP 工具通道，无需也无法重连。",
            "channels": [],
        }

    before = get_mcp_status()
    if not before:
        return {
            "status": "no_mcp",
            "message": "当前没有配置任何工具通道（MCP server），无需重连。",
            "channels": [],
        }

    # 选定要重连的通道：定向传 server 则只连它；否则只连「掉线且非 disabled」的通道。
    if server:
        target = str(server).strip()
        targets = [s.get("name") for s in before if s.get("name") == target]
        if not targets:
            return {
                "status": "unknown_channel",
                "message": f"未找到名为「{target}」的工具通道。当前通道："
                + "、".join(_channel_label(s.get("name", "")) for s in before),
                "channels": _summarize_channels(before),
            }
    else:
        targets = [
            s.get("name")
            for s in before
            if not s.get("connected", False) and not s.get("disabled", False)
        ]

    if not targets:
        return {
            "status": "already_online",
            "message": "所有工具通道均在线，无需重连。",
            "channels": _summarize_channels(before),
        }

    target_labels = "、".join(_channel_label(name) for name in targets)
    logger.info("reconnect_tools: 触发重连掉线通道: %s", ", ".join(targets))

    # 逐条触发重连（拆 + 从磁盘 config 重建 + 重置熔断，非破坏；只动掉线的那条）。
    for name in targets:
        try:
            reconnect_mcp_servers(name)
        except Exception as exc:  # noqa: BLE001 — 单条失败不阻断其它通道，诚实记录
            logger.warning("reconnect_tools: 重连通道 %s 失败: %s", name, exc)

    # 有界短轮询，等连接真正建立（连接是异步建立的，刚重连完多半还是 connecting）。
    target_set = {name for name in targets}
    after = get_mcp_status()
    deadline = time.monotonic() + _RECONNECT_WAIT_SECONDS
    while time.monotonic() < deadline:
        after = get_mcp_status()
        pending = [
            s
            for s in after
            if s.get("name") in target_set
            and not s.get("connected", False)
            and not s.get("disabled", False)
        ]
        if not pending:
            break
        time.sleep(_RECONNECT_POLL_INTERVAL)

    still_down = [
        s
        for s in after
        if s.get("name") in target_set
        and not s.get("connected", False)
        and not s.get("disabled", False)
    ]

    if not still_down:
        return {
            "status": "recovered",
            "message": f"已重连{target_labels}，通道恢复在线。",
            "action": "请重试你刚才失败的工具调用。",
            "channels": _summarize_channels(after),
        }

    down_labels = "、".join(_channel_label(s.get("name", "")) for s in still_down)
    return {
        "status": "still_unreachable",
        "message": (
            f"已触发{target_labels}的重连，但{down_labels}仍不可达"
            "（多半是网络或对应服务端仍未恢复）。"
        ),
        "action": "可稍后再次调用本工具重试；若持续不可达，请如实告知主人该通道暂时中断。",
        "channels": _summarize_channels(after),
    }


def check_reconnect_requirements() -> bool:
    """仅当运行时确有配置的工具通道（MCP server）时才暴露本工具。

    读磁盘 config 判断是否配置了 MCP server；掉线的 server 仍在 config 里，故通道断开时
    本工具依然可见（这正是需要它的时刻）。加载失败一律视为「无 MCP」→ 不暴露。
    """
    try:
        from tools.mcp_tool import _load_mcp_config

        return bool(_load_mcp_config())
    except Exception:  # noqa: BLE001
        return False


RECONNECT_TOOLS_SCHEMA = {
    "name": "reconnect_tools",
    "description": (
        "重连你的工具通道（本地 / 云端 MCP），恢复被中断的工具调用能力。\n\n"
        "何时用：当你调用唤星工具（hasn.* 等本地或云端工具）时遇到「连接失败 / 不可达 / "
        "All connection attempts failed / connect call failed / 通道掉线 / 工具不可用」这类"
        "错误时——说明你的工具通道暂时断开了。**先调用本工具重连，再重试刚才失败的工具**。\n\n"
        "行为：从磁盘配置重建**已掉线**的通道并重置熔断器（健康的通道不动），然后等待并"
        "确认连接是否恢复。这是你的自助恢复手段——本工具是原生工具、不依赖 MCP 通道，所以"
        "两条通道都掉线时你依然能调用它，无需等待主人处理。\n\n"
        "参数：一般**无需传参**（自动只重连掉线的通道）；如需定向重连某一条，可传 "
        "`server`（`cloud`=云端通道 / `hasn`=本地通道）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
                "description": (
                    "可选。定向重连指定通道：`cloud`（云端工具通道）或 `hasn`（本地工具通道）。"
                    "缺省则自动只重连当前已掉线的通道。"
                ),
            },
        },
        "required": [],
    },
}


# --- Registry ---
from tools.registry import registry  # noqa: E402

registry.register(
    name="reconnect_tools",
    toolset="reconnect_tools",
    schema=RECONNECT_TOOLS_SCHEMA,
    handler=lambda args, **kw: reconnect_tools(server=(args or {}).get("server")),
    check_fn=check_reconnect_requirements,
    emoji="🔌",
)
