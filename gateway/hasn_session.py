"""唤星（HuanXing）会话绑定载体 —— 提问卡「哪个 session 发就回哪个 session」修复。

**背景**

本地 hasn MCP 工具里有「向主人提问」一类工具（如 ``hasn.session.ask``）。它需要知道
*当前这次派发属于哪个会话*，才能把提问/答复路由回正确的会话：

- 主会话闲聊 → 答复回主会话（daemon 侧识别为「非工作会话」→ 引导分身就地内联提问）；
- 工作会话任务 → 答复回那个工作会话。

**铁律：分身（LLM）不允许、也不需要自己填 ``session_id``。** 它必须由系统按「这次派发是
哪个 session 发起的」自动注入。同一个内置 Hermes 进程会并发跑多个会话（主会话在聊天，
多个工作会话各跑任务），所以 session 身份必须 *任务局部*（ContextVar），由每次 run 在自己
的执行线程里绑定。

**载体**

CLI runtime 走「每次派发铸造的本地 MCP key（会话绑定）」承载身份；内置 Hermes 走本模块：
api_server 的 ``_handle_runs`` 在跑 agent 的执行线程里 ``set_hasn_session_id(daemon 的
session_id)``，``tools/mcp_tool.py`` 的工具处理器在把参数交给 ``session.call_tool`` 之前，
对 **本地 ``hasn`` MCP 服务**的调用注入保留参数 ``_hasn_session_id``。daemon 侧 hasn-mcp
``server.rs`` 会 strip 掉该保留参数，并在「无 auth 绑定会话」（内置 Hermes 的本地 key 是
per-(owner,agent) 会话无关）时采纳它作为本次调用的会话身份。

只对 ``hasn`` 这个本地 daemon MCP 服务打标：cloud 平台工具服务（``cloud``）与第三方 MCP
不会 strip 该参数，注入会污染它们的入参 schema。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Mapping, Optional

# 与 hasn-mcp（Rust）auth.rs::RESERVED_SESSION_ARG 严格一致。
RESERVED_SESSION_ARG = "_hasn_session_id"

# 本地 daemon MCP 服务在 Hermes 配置里的固定名字（见 huanxing_hermes_runtime
# profile_config.py：``mcp_servers["hasn"]``）。只有它会 strip 保留参数。
HASN_LOCAL_MCP_SERVER = "hasn"

# 任务局部的当前 run 会话 id（daemon 透传的 ``session_id``）。并发会话互不串扰。
_HASN_SESSION_ID: "ContextVar[Optional[str]]" = ContextVar(
    "HUANXING_HASN_SESSION_ID", default=None
)


def set_hasn_session_id(session_id: Optional[str]) -> Token:
    """绑定当前 run 的会话 id，返回 reset token（在 ``finally`` 里 ``reset_hasn_session_id``）。

    传入空/None 表示「本次派发没有可路由的会话」（例如缺省兜底），此时
    ``get_hasn_session_id`` 返回 ``None``，工具处理器不会注入、且会 strip 掉 LLM 误填的
    保留参数。
    """
    normalized = session_id.strip() if isinstance(session_id, str) else None
    return _HASN_SESSION_ID.set(normalized or None)


def reset_hasn_session_id(token: Token) -> None:
    """复原 ``set_hasn_session_id`` 的绑定（run 结束时调用，保持任务局部不外泄）。"""
    _HASN_SESSION_ID.reset(token)


def get_hasn_session_id() -> Optional[str]:
    """读取当前 run 的会话 id；未绑定/为空返回 ``None``。"""
    return _HASN_SESSION_ID.get()


def stamp_session_arg(server_name: str, args: Any) -> Any:
    """对**本地 hasn MCP 服务**的出站调用参数注入系统侧 ``_hasn_session_id``。

    纯函数，不修改入参（immutable：命中时返回新 dict）。规则：

    - 非 ``hasn`` 本地服务（cloud 平台工具 / 第三方 MCP）→ 原样返回，绝不注入；
    - 当前 run 有会话 id → 覆盖式写入 ``_hasn_session_id``（分身无法控制它）；
    - 当前 run 无会话 id → strip 掉 LLM 误填的 ``_hasn_session_id``（杜绝分身自填注入）。
    """
    if server_name != HASN_LOCAL_MCP_SERVER:
        return args
    if not isinstance(args, Mapping):
        return args
    session_id = get_hasn_session_id()
    if session_id:
        return {**args, RESERVED_SESSION_ARG: session_id}
    if RESERVED_SESSION_ARG in args:
        return {key: value for key, value in args.items() if key != RESERVED_SESSION_ARG}
    return args
