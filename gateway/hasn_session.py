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

只对唤星自有的两个 MCP 服务打标（``hasn`` 本地 daemon + ``cloud`` 云端平台工具直连）：
两者都会在 server 侧 strip 掉该保留参数（hasn-mcp ``server.rs`` / 云端 ``server.call_tool``
的 register-on-write 提取点，ARTREG-2）；第三方 MCP 不 strip，注入会污染入参 schema，绝不打标。

**会话轴分流（设计 02 §4.3）**

daemon 透传的 ``session_id`` 恒为**运行时/逻辑会话**语义（哪个 session 发就回哪个 session），
继续盖 ``_hasn_session_id``；``work_session_id`` 是**工作会话**语义（仅任务/appcollab 派发
非空，IM 主会话派发恒 None），盖 ``_hasn_work_session_id``——云端据它把产物落工作会话列。
两键分轨绑定、分轨打标，互不干扰。
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from typing import Any, Mapping, Optional, Sequence

# 与 hasn-mcp（Rust）auth.rs::RESERVED_SESSION_ARG 严格一致。
# 设计 02 §4.3 会话轴分流后，daemon 透传的 ``session_id`` 恒为**运行时/逻辑会话**语义
# （IM 续接路由、message.send 回灌定位），本键继续承载它。
RESERVED_SESSION_ARG = "_hasn_session_id"

# 与 hasn-mcp（Rust）auth.rs::RESERVED_WORK_SESSION_ARG + 云端
# trust_gate.py::RESERVED_WORK_SESSION_ID 逐字一致（设计 02 §4.3 会话轴分流）。
# 本次派发所属的**工作会话** id：仅真实工作会话派发（任务/appcollab）时非空，
# IM 主会话派发恒 None。云端据它把产物落 ``hasn_artifacts.work_session_id`` 列
# （工作会话资源栏）；分身（LLM）不允许、也不需要自己填。
RESERVED_WORK_SESSION_ARG = "_hasn_work_session_id"

# 与 hasn-mcp（Rust）auth.rs::RESERVED_PROJECT_ARG + 云端 _project_id_var 提取键
# 严格一致（PJ U5b）。本次派发所属的平台项目 id，随会话身份一同系统注入，供云端
# register-on-write 把产出资源打到项目名下；分身（LLM）不允许、也不需要自己填。
RESERVED_PROJECT_ARG = "_hasn_project_id"

# 与 hasn-mcp（Rust）auth.rs::RESERVED_WORKING_DIRECTORY_ARG 严格一致。
# 本机绝对路径只允许发送给本地 ``hasn`` MCP 服务；云端 ``cloud`` 必须剥离，
# 防止把某台设备的磁盘信息写入云端请求、日志或持久化数据。
RESERVED_WORKING_DIRECTORY_ARG = "_hasn_working_directory"

# 与 hasn-mcp（Rust）auth.rs::RESERVED_CONTEXT_CAPABILITY_ARG 严格一致。
# daemon 为本次项目 run 签发的本地能力凭据；只盖给本地 ``hasn`` MCP，cloud/第三方
# 绝不接收。MCP server 用它核对身份、项目、会话、目录和工具白名单。
RESERVED_CONTEXT_CAPABILITY_ARG = "_hasn_context_capability"

# 与 hasn-mcp（Rust）auth.rs::RESERVED_ALLOWED_TOOL_NAMES_ARG 及云端
# trust_gate.py::RESERVED_ALLOWED_TOOL_NAMES 严格一致。它承载本次工作会话可调用的
# canonical 工具名；由 Runtime 在模型输出后覆盖式注入，分身不可自填。
RESERVED_ALLOWED_TOOL_NAMES_ARG = "_hasn_allowed_tool_names"

# 本地 daemon MCP 服务在 Hermes 配置里的固定名字（见 huanxing_hermes_runtime
# profile_config.py：``mcp_servers["hasn"]``）。
HASN_LOCAL_MCP_SERVER = "hasn"

# 云端平台工具 MCP 服务（profile_config.py：``mcp_servers["cloud"]``，直连云端
# /api/v1/mcp/streamable）。云端 server.call_tool 同样剥离保留参数并落
# AgentContext.work_session_id（register-on-write），故一并打标——否则 deck 等
# 云端平台工具的产物登记不带工作会话 id，会话资源栏看不到本次产出（血泪 bug）。
HASN_CLOUD_MCP_SERVER = "cloud"

# 会被系统打标 ``_hasn_session_id`` 的唤星自有 MCP 服务全集（server 侧都会 strip）。
HASN_STAMPED_MCP_SERVERS = frozenset({HASN_LOCAL_MCP_SERVER, HASN_CLOUD_MCP_SERVER})

# 受限会话仍需保留的渐进式暴露传输壳；真正业务目标会在本地/云端 wrapper 内再次判权。
HASN_SESSION_TRANSPORT_TOOLS = frozenset(
    {
        "hasn.local.tool.search",
        "hasn.local.tool.call",
        "hasn.cloud.tool.search",
        "hasn.cloud.tool.call",
        "hasn.tool.search",
    }
)

_CANONICAL_TOOL_NAME = re.compile(r"^hasn(?:\.[A-Za-z0-9_-]+)+$")

# 任务局部的当前 run 会话 id（daemon 透传的 ``session_id``）。并发会话互不串扰。
_HASN_SESSION_ID: "ContextVar[Optional[str]]" = ContextVar(
    "HUANXING_HASN_SESSION_ID", default=None
)

# 任务局部的当前 run **工作会话** id（daemon 透传的 ``work_session_id``，设计 02 §4.3）。
# 与 ``_HASN_SESSION_ID``（运行时轴）严格分工、同轨绑定；IM 主会话派发时为 ``None``
# （不注入、并 strip 掉 LLM 误填的保留参数），并发派发互不串扰。
_HASN_WORK_SESSION_ID: "ContextVar[Optional[str]]" = ContextVar(
    "HUANXING_HASN_WORK_SESSION_ID", default=None
)

# 任务局部的当前 run 平台项目 id（daemon 透传的 ``project_id``）。与会话 id 同轨、
# 并发派发互不串扰；非项目派发时为 ``None``（云端 register-on-write 不打标，零影响面）。
_HASN_PROJECT_ID: "ContextVar[Optional[str]]" = ContextVar(
    "HUANXING_HASN_PROJECT_ID", default=None
)

# 任务局部的可信本机工作目录。该值来自 daemon 的 project_workspaces 映射，
# 只用于本地工具根目录，不进入 cloud MCP 调用。
_HASN_WORKING_DIRECTORY: "ContextVar[Optional[str]]" = ContextVar(
    "HUANXING_HASN_WORKING_DIRECTORY", default=None
)

_HASN_CONTEXT_CAPABILITY: "ContextVar[Optional[str]]" = ContextVar(
    "HUANXING_HASN_CONTEXT_CAPABILITY", default=None
)

# 任务局部的当前 run 精确工具白名单。``None`` 表示兼容旧调用、不限制；空 tuple
# 表示明确拒绝全部业务工具。保留 tuple 是为了原样传到下游，不打乱调用方声明顺序。
_HASN_ALLOWED_TOOL_NAMES: "ContextVar[Optional[tuple[str, ...]]]" = ContextVar(
    "HUANXING_HASN_ALLOWED_TOOL_NAMES", default=None
)


def parse_allowed_tool_names(raw: Any) -> tuple[str, ...]:
    """严格解析工作会话工具白名单，保持原始顺序并拒绝重复项。

    只接受 canonical 工具名字符串数组；字段缺失与 ``None`` 的兼容语义由 API
    调用方处理，传到这里的值必须是数组。
    """
    if not isinstance(raw, list):
        raise ValueError("allowed_tool_names must be an array")
    names: list[str] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, str) or not _CANONICAL_TOOL_NAME.fullmatch(value):
            raise ValueError("allowed_tool_names contains a non-canonical tool name")
        if value in seen:
            raise ValueError(f"allowed_tool_names contains duplicate tool name: {value}")
        seen.add(value)
        names.append(value)
    return tuple(names)


def set_hasn_allowed_tool_names(
    allowed_tool_names: Optional[Sequence[str]],
) -> Token:
    """绑定当前 run 的精确工具白名单，返回供 ``finally`` 复原的 token。"""
    normalized = (
        tuple(allowed_tool_names) if allowed_tool_names is not None else None
    )
    return _HASN_ALLOWED_TOOL_NAMES.set(normalized)


def reset_hasn_allowed_tool_names(token: Token) -> None:
    """复原当前 run 的工具白名单绑定。"""
    _HASN_ALLOWED_TOOL_NAMES.reset(token)


def get_hasn_allowed_tool_names() -> Optional[tuple[str, ...]]:
    """读取当前 run 的工具白名单；``None`` 表示旧会话不限制。"""
    return _HASN_ALLOWED_TOOL_NAMES.get()


def is_hasn_tool_allowed(server_name: str, tool_name: str) -> bool:
    """判断当前 run 是否允许执行指定 MCP 目标。

    受限会话只允许唤星自有服务上的传输壳与白名单业务工具；第三方 MCP 和
    Hermes 内置工具均不在此通道放行。
    """
    allowed_tool_names = get_hasn_allowed_tool_names()
    if allowed_tool_names is None:
        return True
    if server_name not in HASN_STAMPED_MCP_SERVERS:
        return False
    return (
        tool_name in HASN_SESSION_TRANSPORT_TOOLS
        or tool_name in allowed_tool_names
    )


def stamp_allowed_tool_names_arg(server_name: str, args: Any) -> Any:
    """把当前 run 的精确工具白名单盖章到唤星自有 MCP 调用。

    有白名单时覆盖模型同名入参；无白名单时剥离模型伪造值；第三方服务原样返回。
    """
    if server_name not in HASN_STAMPED_MCP_SERVERS:
        return args
    if not isinstance(args, Mapping):
        return args
    allowed_tool_names = get_hasn_allowed_tool_names()
    if allowed_tool_names is not None:
        return {
            **args,
            RESERVED_ALLOWED_TOOL_NAMES_ARG: list(allowed_tool_names),
        }
    if RESERVED_ALLOWED_TOOL_NAMES_ARG in args:
        return {
            key: value
            for key, value in args.items()
            if key != RESERVED_ALLOWED_TOOL_NAMES_ARG
        }
    return args


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


def set_hasn_work_session_id(work_session_id: Optional[str]) -> Token:
    """绑定当前 run 的**工作会话** id，返回 reset token（``finally`` 里 ``reset_hasn_work_session_id``）。

    与 ``set_hasn_session_id`` 同轨（设计 02 §4.3 会话轴分流）：传入空/None 表示
    「本次派发不属于任何工作会话」（IM 主会话派发即此形态），此时
    ``get_hasn_work_session_id`` 返回 ``None``，工具处理器不注入、并 strip 掉 LLM
    误填的保留参数。
    """
    normalized = work_session_id.strip() if isinstance(work_session_id, str) else None
    return _HASN_WORK_SESSION_ID.set(normalized or None)


def reset_hasn_work_session_id(token: Token) -> None:
    """复原 ``set_hasn_work_session_id`` 的绑定（run 结束时调用，保持任务局部不外泄）。"""
    _HASN_WORK_SESSION_ID.reset(token)


def get_hasn_work_session_id() -> Optional[str]:
    """读取当前 run 的工作会话 id；未绑定/为空返回 ``None``。"""
    return _HASN_WORK_SESSION_ID.get()


def set_hasn_project_id(project_id: Optional[str]) -> Token:
    """绑定当前 run 的平台项目 id，返回 reset token（在 ``finally`` 里 ``reset_hasn_project_id``）。

    与 ``set_hasn_session_id`` 同轨：传入空/None 表示「本次派发不属于任何项目」，此时
    ``get_hasn_project_id`` 返回 ``None``，工具处理器不注入、并 strip 掉 LLM 误填的保留参数。
    """
    normalized = project_id.strip() if isinstance(project_id, str) else None
    return _HASN_PROJECT_ID.set(normalized or None)


def reset_hasn_project_id(token: Token) -> None:
    """复原 ``set_hasn_project_id`` 的绑定（run 结束时调用，保持任务局部不外泄）。"""
    _HASN_PROJECT_ID.reset(token)


def get_hasn_project_id() -> Optional[str]:
    """读取当前 run 的平台项目 id；未绑定/为空返回 ``None``。"""
    return _HASN_PROJECT_ID.get()


def set_hasn_working_directory(working_directory: Optional[str]) -> Token:
    """绑定当前 run 的可信本机工作目录，返回供 ``finally`` 复原的 token。"""
    normalized = (
        working_directory.strip()
        if isinstance(working_directory, str)
        else None
    )
    return _HASN_WORKING_DIRECTORY.set(normalized or None)


def reset_hasn_working_directory(token: Token) -> None:
    """复原当前 run 的本机工作目录绑定。"""
    _HASN_WORKING_DIRECTORY.reset(token)


def get_hasn_working_directory() -> Optional[str]:
    """读取当前 run 的可信本机工作目录；未绑定时返回 ``None``。"""
    return _HASN_WORKING_DIRECTORY.get()


def set_hasn_context_capability(capability: Optional[str]) -> Token:
    """绑定当前项目 run 的 daemon 能力凭据，返回供 ``finally`` 复原的 token。"""
    normalized = capability.strip() if isinstance(capability, str) else None
    return _HASN_CONTEXT_CAPABILITY.set(normalized or None)


def reset_hasn_context_capability(token: Token) -> None:
    """复原当前 run 的项目上下文能力凭据。"""
    _HASN_CONTEXT_CAPABILITY.reset(token)


def get_hasn_context_capability() -> Optional[str]:
    """读取当前 run 的项目上下文能力凭据；未绑定时返回 ``None``。"""
    return _HASN_CONTEXT_CAPABILITY.get()


def stamp_session_arg(server_name: str, args: Any) -> Any:
    """对**唤星自有 MCP 服务**（``hasn`` 本地 / ``cloud`` 云端平台工具）的出站调用参数
    注入系统侧 ``_hasn_session_id``。

    纯函数，不修改入参（immutable：命中时返回新 dict）。规则：

    - 非唤星自有服务（第三方 MCP）→ 原样返回，绝不注入（它们不 strip，注入即污染）；
    - 当前 run 有会话 id → 覆盖式写入 ``_hasn_session_id``（分身无法控制它）；
    - 当前 run 无会话 id → strip 掉 LLM 误填的 ``_hasn_session_id``（杜绝分身自填注入）。
    """
    if server_name not in HASN_STAMPED_MCP_SERVERS:
        return args
    if not isinstance(args, Mapping):
        return args
    session_id = get_hasn_session_id()
    if session_id:
        return {**args, RESERVED_SESSION_ARG: session_id}
    if RESERVED_SESSION_ARG in args:
        return {key: value for key, value in args.items() if key != RESERVED_SESSION_ARG}
    return args


def stamp_work_session_arg(server_name: str, args: Any) -> Any:
    """对**唤星自有 MCP 服务**（``hasn`` 本地 / ``cloud`` 云端平台工具）的出站调用参数
    注入系统侧 ``_hasn_work_session_id``（设计 02 §4.3 会话轴分流）。

    与 ``stamp_session_arg`` 同规则、同不变量（纯函数、不修改入参、只对唤星自有服务打标）：

    - 非唤星自有服务（第三方 MCP）→ 原样返回，绝不注入（它们不 strip，注入即污染）；
    - 当前 run 有工作会话 id → 覆盖式写入 ``_hasn_work_session_id``（分身无法控制它）；
    - 当前 run 无工作会话 id（IM 主会话派发）→ strip 掉 LLM 误填的
      ``_hasn_work_session_id``（杜绝分身自填污染工作会话轴）。

    应在 ``stamp_session_arg`` 之后链式调用（两者互不干扰，各自增删自己的保留键）。
    """
    if server_name not in HASN_STAMPED_MCP_SERVERS:
        return args
    if not isinstance(args, Mapping):
        return args
    work_session_id = get_hasn_work_session_id()
    if work_session_id:
        return {**args, RESERVED_WORK_SESSION_ARG: work_session_id}
    if RESERVED_WORK_SESSION_ARG in args:
        return {
            key: value
            for key, value in args.items()
            if key != RESERVED_WORK_SESSION_ARG
        }
    return args


def stamp_project_arg(server_name: str, args: Any) -> Any:
    """对**唤星自有 MCP 服务**（``hasn`` 本地 / ``cloud`` 云端平台工具）的出站调用参数
    注入系统侧 ``_hasn_project_id``（PJ U5b）。

    与 ``stamp_session_arg`` 同规则、同不变量（纯函数、不修改入参、只对唤星自有服务打标）：

    - 非唤星自有服务（第三方 MCP）→ 原样返回，绝不注入（它们不 strip，注入即污染 schema）；
    - 当前 run 有项目 id → 覆盖式写入 ``_hasn_project_id``（分身无法控制它）；
    - 当前 run 无项目 id → strip 掉 LLM 误填的 ``_hasn_project_id``（杜绝分身自填注入）。

    应在 ``stamp_session_arg`` 之后链式调用（两者互不干扰，各自增删自己的保留键）。
    """
    if server_name not in HASN_STAMPED_MCP_SERVERS:
        return args
    if not isinstance(args, Mapping):
        return args
    project_id = get_hasn_project_id()
    if project_id:
        return {**args, RESERVED_PROJECT_ARG: project_id}
    if RESERVED_PROJECT_ARG in args:
        return {key: value for key, value in args.items() if key != RESERVED_PROJECT_ARG}
    return args


def stamp_working_directory_arg(server_name: str, args: Any) -> Any:
    """只向本地 ``hasn`` MCP 调用注入可信工作目录。

    ``cloud`` 调用无条件剥离该保留参数，第三方 MCP 保持原样；当前 run 未绑定目录时，
    本地调用也会剥离模型伪造值。函数不修改原入参。
    """
    if server_name not in HASN_STAMPED_MCP_SERVERS:
        return args
    if not isinstance(args, Mapping):
        return args
    working_directory = get_hasn_working_directory()
    if server_name == HASN_LOCAL_MCP_SERVER and working_directory:
        return {**args, RESERVED_WORKING_DIRECTORY_ARG: working_directory}
    if RESERVED_WORKING_DIRECTORY_ARG in args:
        return {
            key: value
            for key, value in args.items()
            if key != RESERVED_WORKING_DIRECTORY_ARG
        }
    return args


def stamp_context_capability_arg(server_name: str, args: Any) -> Any:
    """只向本地 ``hasn`` MCP 调用盖入 daemon 签发能力，其余服务无条件剥离。"""
    if server_name not in HASN_STAMPED_MCP_SERVERS:
        return args
    if not isinstance(args, Mapping):
        return args
    capability = get_hasn_context_capability()
    if server_name == HASN_LOCAL_MCP_SERVER and capability:
        return {**args, RESERVED_CONTEXT_CAPABILITY_ARG: capability}
    if RESERVED_CONTEXT_CAPABILITY_ARG in args:
        return {
            key: value
            for key, value in args.items()
            if key != RESERVED_CONTEXT_CAPABILITY_ARG
        }
    return args
