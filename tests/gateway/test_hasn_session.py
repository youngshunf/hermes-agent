"""唤星会话绑定载体（提问卡修复）单测。

覆盖 ``gateway.hasn_session``：
- ``set/get/reset`` ContextVar 任务局部、空值归一；
- ``stamp_session_arg`` 纯函数：对唤星自有服务（``hasn`` 本地 + ``cloud`` 云端平台工具）
  打标、覆盖式注入、无会话时 strip 掉 LLM 误填的保留参数、第三方服务原样、不修改入参。
"""

import pytest

from gateway.hasn_session import (
    HASN_CLOUD_MCP_SERVER,
    HASN_LOCAL_MCP_SERVER,
    HASN_STAMPED_MCP_SERVERS,
    RESERVED_SESSION_ARG,
    get_hasn_session_id,
    reset_hasn_session_id,
    set_hasn_session_id,
    stamp_session_arg,
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
