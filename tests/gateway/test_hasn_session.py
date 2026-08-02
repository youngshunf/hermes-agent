"""唤星会话绑定载体（提问卡修复）+ 工作会话轨（设计 02 §4.3）+ 项目绑定载体（PJ U5b）单测。

覆盖 ``gateway.hasn_session``：
- ``set/get/reset`` ContextVar 任务局部、空值归一（会话、工作会话、项目三条轨各一套）；
- ``stamp_session_arg`` / ``stamp_work_session_arg`` / ``stamp_project_arg`` 纯函数：
  对唤星自有服务（``hasn`` 本地 + ``cloud`` 云端平台工具）打标、覆盖式注入、无值时
  strip 掉 LLM 误填的保留参数、第三方服务原样、不修改入参；
- 三条轨链式调用互不干扰（各增删自己的保留键）；
- 会话轴分流契约：``_hasn_session_id`` 恒为运行时/逻辑会话语义，
  ``_hasn_work_session_id`` 仅真实工作会话派发非空，IM 主会话派发被 strip。
"""

import pytest

from gateway.hasn_session import (
    HASN_CLOUD_MCP_SERVER,
    HASN_LOCAL_MCP_SERVER,
    HASN_STAMPED_MCP_SERVERS,
    RESERVED_PROJECT_ARG,
    RESERVED_SESSION_ARG,
    RESERVED_WORKING_DIRECTORY_ARG,
    RESERVED_WORK_SESSION_ARG,
    get_hasn_project_id,
    get_hasn_session_id,
    get_hasn_working_directory,
    get_hasn_work_session_id,
    reset_hasn_project_id,
    reset_hasn_session_id,
    reset_hasn_working_directory,
    reset_hasn_work_session_id,
    set_hasn_project_id,
    set_hasn_session_id,
    set_hasn_working_directory,
    set_hasn_work_session_id,
    stamp_project_arg,
    stamp_session_arg,
    stamp_working_directory_arg,
    stamp_work_session_arg,
)


@pytest.fixture(autouse=True)
def _clear_session():
    """每个用例前后归零 ContextVar，避免跨用例串味。"""
    token = set_hasn_session_id(None)
    try:
        yield
    finally:
        reset_hasn_session_id(token)


def test_reserved_arg_matches_rust_contract():
    # 与 hasn-mcp(Rust) auth.rs::RESERVED_SESSION_ARG 严格一致。
    assert RESERVED_SESSION_ARG == "_hasn_session_id"
    assert HASN_LOCAL_MCP_SERVER == "hasn"
    # 云端平台工具直连服务名与 profile_config.py mcp_servers["cloud"] 一致。
    assert HASN_CLOUD_MCP_SERVER == "cloud"
    assert HASN_STAMPED_MCP_SERVERS == {"hasn", "cloud"}


def test_set_get_reset_roundtrip():
    assert get_hasn_session_id() is None
    token = set_hasn_session_id("work-session-1")
    assert get_hasn_session_id() == "work-session-1"
    reset_hasn_session_id(token)
    assert get_hasn_session_id() is None


def test_blank_session_id_normalizes_to_none():
    token = set_hasn_session_id("   ")
    try:
        assert get_hasn_session_id() is None
    finally:
        reset_hasn_session_id(token)


def test_stamp_injects_session_id_for_hasn_server():
    token = set_hasn_session_id("work-session-7")
    try:
        # hasn 本地 + cloud 云端平台工具两个自有服务都打标（云端 register-on-write
        # 依赖它把 deck 等产物登记进工作会话资源栏）。
        for server in ("hasn", "cloud"):
            original = {"question": "继续吗？"}
            out = stamp_session_arg(server, original)
            assert out[RESERVED_SESSION_ARG] == "work-session-7"
            assert out["question"] == "继续吗？"
            # 不修改入参（immutable）。
            assert RESERVED_SESSION_ARG not in original
    finally:
        reset_hasn_session_id(token)


def test_stamp_overwrites_llm_provided_session_id():
    # 分身不允许自填——系统值覆盖 LLM 值。
    token = set_hasn_session_id("authoritative-session")
    try:
        out = stamp_session_arg("hasn", {RESERVED_SESSION_ARG: "llm-faked"})
        assert out[RESERVED_SESSION_ARG] == "authoritative-session"
    finally:
        reset_hasn_session_id(token)


def test_stamp_strips_llm_injected_arg_when_no_session():
    # 无 run 会话时，绝不让 LLM 误填的保留参数漏到 daemon。
    assert get_hasn_session_id() is None
    out = stamp_session_arg("hasn", {RESERVED_SESSION_ARG: "llm-faked", "q": 1})
    assert RESERVED_SESSION_ARG not in out
    assert out["q"] == 1


def test_stamp_noop_for_third_party_server():
    # 第三方 MCP 绝不注入（它们不 strip 该保留参数，注入即污染入参 schema）。
    token = set_hasn_session_id("work-session-7")
    try:
        original = {"x": 1}
        for server in ("qcc", "filesystem", "slack"):
            out = stamp_session_arg(server, original)
            assert out is original
            assert RESERVED_SESSION_ARG not in out
    finally:
        reset_hasn_session_id(token)


def test_stamp_tolerates_non_mapping_args():
    token = set_hasn_session_id("s1")
    try:
        assert stamp_session_arg("hasn", None) is None
        assert stamp_session_arg("hasn", "raw") == "raw"
    finally:
        reset_hasn_session_id(token)


# ---------------------------------------------------------------------------
# PJ U5b：项目绑定轨（与会话轨同规约、独立键）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_project():
    """每个用例前后归零项目 ContextVar，避免跨用例串味。"""
    token = set_hasn_project_id(None)
    try:
        yield
    finally:
        reset_hasn_project_id(token)


def test_project_reserved_arg_matches_rust_contract():
    # 与 hasn-mcp(Rust) auth.rs::RESERVED_PROJECT_ARG + 云端 _project_id_var 提取键严格一致。
    assert RESERVED_PROJECT_ARG == "_hasn_project_id"


def test_project_set_get_reset_roundtrip():
    assert get_hasn_project_id() is None
    token = set_hasn_project_id("proj-1")
    assert get_hasn_project_id() == "proj-1"
    reset_hasn_project_id(token)
    assert get_hasn_project_id() is None


def test_blank_project_id_normalizes_to_none():
    token = set_hasn_project_id("   ")
    try:
        assert get_hasn_project_id() is None
    finally:
        reset_hasn_project_id(token)


def test_stamp_injects_project_id_for_hasn_and_cloud():
    token = set_hasn_project_id("proj-7")
    try:
        for server in ("hasn", "cloud"):
            original = {"title": "季度复盘"}
            out = stamp_project_arg(server, original)
            assert out[RESERVED_PROJECT_ARG] == "proj-7"
            assert out["title"] == "季度复盘"
            # 不修改入参（immutable）。
            assert RESERVED_PROJECT_ARG not in original
    finally:
        reset_hasn_project_id(token)


def test_stamp_overwrites_llm_provided_project_id():
    # 分身不允许自填——系统值覆盖 LLM 值。
    token = set_hasn_project_id("authoritative-project")
    try:
        out = stamp_project_arg("hasn", {RESERVED_PROJECT_ARG: "llm-faked"})
        assert out[RESERVED_PROJECT_ARG] == "authoritative-project"
    finally:
        reset_hasn_project_id(token)


def test_stamp_strips_llm_injected_project_arg_when_no_project():
    # 无 run 项目时（非项目派发），绝不让 LLM 误填的保留参数漏到 daemon/云端。
    assert get_hasn_project_id() is None
    out = stamp_project_arg("hasn", {RESERVED_PROJECT_ARG: "llm-faked", "q": 1})
    assert RESERVED_PROJECT_ARG not in out
    assert out["q"] == 1


def test_stamp_project_noop_for_third_party_server():
    # 第三方 MCP 绝不注入（它们不 strip 该保留参数，注入即污染入参 schema）。
    token = set_hasn_project_id("proj-7")
    try:
        original = {"x": 1}
        for server in ("qcc", "filesystem", "slack"):
            out = stamp_project_arg(server, original)
            assert out is original
            assert RESERVED_PROJECT_ARG not in out
    finally:
        reset_hasn_project_id(token)


def test_stamp_project_tolerates_non_mapping_args():
    token = set_hasn_project_id("p1")
    try:
        assert stamp_project_arg("hasn", None) is None
        assert stamp_project_arg("hasn", "raw") == "raw"
    finally:
        reset_hasn_project_id(token)


def test_session_and_project_stamps_chain_independently():
    # 两条轨链式调用（mcp_tool.py 里 session 先、project 后），各写各的键、互不干扰。
    s_token = set_hasn_session_id("sess-9")
    p_token = set_hasn_project_id("proj-9")
    try:
        args = {"body": "内容"}
        args = stamp_session_arg("hasn", args)
        args = stamp_project_arg("hasn", args)
        assert args[RESERVED_SESSION_ARG] == "sess-9"
        assert args[RESERVED_PROJECT_ARG] == "proj-9"
        assert args["body"] == "内容"
    finally:
        reset_hasn_project_id(p_token)
        reset_hasn_session_id(s_token)


def test_project_stamp_alone_when_no_session():
    # 会话轨为空、项目轨有值：project 键在、session 键被 strip（若 LLM 误填）。
    p_token = set_hasn_project_id("proj-only")
    try:
        args = {RESERVED_SESSION_ARG: "llm-faked-session"}
        args = stamp_session_arg("hasn", args)  # 无会话 → strip
        args = stamp_project_arg("hasn", args)  # 有项目 → 注入
        assert RESERVED_SESSION_ARG not in args
        assert args[RESERVED_PROJECT_ARG] == "proj-only"
    finally:
        reset_hasn_project_id(p_token)


# ── 本机工作目录轨（仅本地 hasn MCP，绝不发往云端）──────────────────────────


def test_working_directory_reserved_arg_matches_rust_contract():
    assert RESERVED_WORKING_DIRECTORY_ARG == "_hasn_working_directory"


def test_working_directory_set_get_reset_roundtrip(tmp_path):
    token = set_hasn_working_directory(str(tmp_path))
    assert get_hasn_working_directory() == str(tmp_path)
    reset_hasn_working_directory(token)
    assert get_hasn_working_directory() is None


def test_working_directory_only_stamps_local_hasn(tmp_path):
    token = set_hasn_working_directory(str(tmp_path))
    try:
        local_args = stamp_working_directory_arg("hasn", {"path": "报告.md"})
        assert local_args[RESERVED_WORKING_DIRECTORY_ARG] == str(tmp_path)

        cloud_args = stamp_working_directory_arg(
            "cloud",
            {RESERVED_WORKING_DIRECTORY_ARG: str(tmp_path), "title": "报告"},
        )
        assert RESERVED_WORKING_DIRECTORY_ARG not in cloud_args
        assert cloud_args["title"] == "报告"

        third_party = {"path": "报告.md"}
        assert stamp_working_directory_arg("filesystem", third_party) is third_party
    finally:
        reset_hasn_working_directory(token)


def test_working_directory_stamp_strips_untrusted_value_without_context():
    args = stamp_working_directory_arg(
        "hasn",
        {RESERVED_WORKING_DIRECTORY_ARG: "/tmp/伪造目录", "path": "报告.md"},
    )
    assert RESERVED_WORKING_DIRECTORY_ARG not in args
    assert args["path"] == "报告.md"


# ── 工作会话轨（设计 02 §4.3 会话轴分流）────────────────────────────────────


def test_work_session_reserved_arg_matches_rust_and_cloud_contract():
    # 与 hasn-mcp(Rust) auth.rs::RESERVED_WORK_SESSION_ARG + 云端
    # trust_gate.py::RESERVED_WORK_SESSION_ID 逐字一致——错一个字符整条产物登记链静默断。
    assert RESERVED_WORK_SESSION_ARG == "_hasn_work_session_id"


def test_work_session_set_get_reset_roundtrip():
    assert get_hasn_work_session_id() is None
    token = set_hasn_work_session_id("ws-1")
    assert get_hasn_work_session_id() == "ws-1"
    reset_hasn_work_session_id(token)
    assert get_hasn_work_session_id() is None


def test_blank_work_session_id_normalizes_to_none():
    token = set_hasn_work_session_id("   ")
    try:
        assert get_hasn_work_session_id() is None
    finally:
        reset_hasn_work_session_id(token)


def test_stamp_injects_work_session_id_for_hasn_and_cloud():
    token = set_hasn_work_session_id("ws-7")
    try:
        for server in ("hasn", "cloud"):
            original = {"title": "季度复盘"}
            out = stamp_work_session_arg(server, original)
            assert out[RESERVED_WORK_SESSION_ARG] == "ws-7"
            assert out["title"] == "季度复盘"
            # 不修改入参（immutable）。
            assert RESERVED_WORK_SESSION_ARG not in original
    finally:
        reset_hasn_work_session_id(token)


def test_stamp_overwrites_llm_provided_work_session_id():
    # 分身不允许自填——系统值覆盖 LLM 值。
    token = set_hasn_work_session_id("authoritative-ws")
    try:
        out = stamp_work_session_arg("hasn", {RESERVED_WORK_SESSION_ARG: "llm-faked"})
        assert out[RESERVED_WORK_SESSION_ARG] == "authoritative-ws"
    finally:
        reset_hasn_work_session_id(token)


def test_stamp_strips_llm_injected_work_session_arg_when_no_work_session():
    # IM 主会话派发（无工作会话绑定）时，绝不让 LLM 误填的保留参数漏到 daemon/云端
    # 污染工作会话轴。
    assert get_hasn_work_session_id() is None
    out = stamp_work_session_arg("hasn", {RESERVED_WORK_SESSION_ARG: "llm-faked", "q": 1})
    assert RESERVED_WORK_SESSION_ARG not in out
    assert out["q"] == 1


def test_stamp_work_session_noop_for_third_party_server():
    # 第三方 MCP 绝不注入（它们不 strip 该保留参数，注入即污染入参 schema）。
    token = set_hasn_work_session_id("ws-7")
    try:
        original = {"x": 1}
        for server in ("qcc", "filesystem", "slack"):
            out = stamp_work_session_arg(server, original)
            assert out is original
            assert RESERVED_WORK_SESSION_ARG not in out
    finally:
        reset_hasn_work_session_id(token)


def test_stamp_work_session_tolerates_non_mapping_args():
    token = set_hasn_work_session_id("ws-1")
    try:
        assert stamp_work_session_arg("hasn", None) is None
        assert stamp_work_session_arg("hasn", "raw") == "raw"
    finally:
        reset_hasn_work_session_id(token)


def test_session_and_work_session_stamps_chain_independently():
    # 会话轴分流的核心契约：两轨链式调用（mcp_tool.py 里 session 先、work_session 后），
    # 各写各的键、互不干扰——runtime 值与工作会话值同时落在一次调用上。
    s_token = set_hasn_session_id("rt-sess-9")
    w_token = set_hasn_work_session_id("ws-9")
    try:
        args = {"body": "内容"}
        args = stamp_session_arg("hasn", args)
        args = stamp_work_session_arg("hasn", args)
        assert args[RESERVED_SESSION_ARG] == "rt-sess-9"
        assert args[RESERVED_WORK_SESSION_ARG] == "ws-9"
        assert args["body"] == "内容"
    finally:
        reset_hasn_work_session_id(w_token)
        reset_hasn_session_id(s_token)


def test_work_session_stamp_alone_when_im_main_session():
    # IM 主会话派发形态：runtime 轨有值、工作会话轨空——session 键注入、
    # work_session 键被 strip（若 LLM 误填），产物不进任何工作会话资源栏。
    s_token = set_hasn_session_id("rt-main-1")
    try:
        args = {RESERVED_WORK_SESSION_ARG: "llm-faked-ws"}
        args = stamp_session_arg("hasn", args)  # 有 runtime 会话 → 注入
        args = stamp_work_session_arg("hasn", args)  # 无工作会话 → strip
        assert args[RESERVED_SESSION_ARG] == "rt-main-1"
        assert RESERVED_WORK_SESSION_ARG not in args
    finally:
        reset_hasn_session_id(s_token)
