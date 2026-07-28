"""Tests for /v1/runs endpoints: start, status, events, and stop.

Covers:
- POST /v1/runs — start a run (202)
- GET /v1/runs/{run_id} — poll run status
- GET /v1/runs/{run_id}/events — SSE event stream
- POST /v1/runs/{run_id}/stop — interrupt a running agent
- Auth, error handling, and cleanup
"""

import asyncio
import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    _approval_event_choices,
    cors_middleware,
    security_headers_middleware,
)
from tools import approval as approval_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("smart_denied", "allow_permanent", "expected"),
    [
        (False, True, ["once", "session", "always", "deny"]),
        (False, False, ["once", "session", "deny"]),
        (True, True, ["once", "deny"]),
        (True, False, ["once", "deny"]),
    ],
)
def test_approval_event_choices_follow_backend_capabilities(
    smart_denied, allow_permanent, expected
):
    assert _approval_event_choices(
        smart_denied=smart_denied,
        allow_permanent=allow_permanent,
    ) == expected


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    """Create an adapter with optional API key."""
    extra = {}
    if api_key:
        extra["key"] = api_key
    config = PlatformConfig(enabled=True, extra=extra)
    adapter = APIServerAdapter(config)
    return adapter


def _create_runs_app(adapter: APIServerAdapter) -> web.Application:
    """Create an aiohttp app with /v1/runs routes registered."""
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
    app.router.add_post("/v1/runs/{run_id}/approval", adapter._handle_run_approval)
    app.router.add_post("/v1/runs/{run_id}/stop", adapter._handle_stop_run)
    return app


def _make_slow_agent(**kwargs):
    """Create a mock agent that blocks in run_conversation until interrupted.

    Returns (mock_agent, agent_ready_event, interrupt_event) where
    agent_ready_event is set once run_conversation starts, and
    interrupt_event is set when interrupt() is called.
    """
    ready = threading.Event()
    interrupted = threading.Event()

    mock_agent = MagicMock()

    def _do_interrupt(message=None):
        interrupted.set()

    mock_agent.interrupt = MagicMock(side_effect=_do_interrupt)

    def _slow_run(user_message=None, conversation_history=None, task_id=None):
        ready.set()
        # Block until interrupt() is called
        interrupted.wait(timeout=10)
        return {"final_response": "interrupted"}

    mock_agent.run_conversation.side_effect = _slow_run
    mock_agent.session_prompt_tokens = 0
    mock_agent.session_completion_tokens = 0
    mock_agent.session_total_tokens = 0

    return mock_agent, ready, interrupted


@pytest.fixture
def adapter():
    return _make_adapter()


@pytest.fixture
def auth_adapter():
    return _make_adapter(api_key="sk-secret")


# ---------------------------------------------------------------------------
# POST /v1/runs — start a run
# ---------------------------------------------------------------------------


class TestStartRun:
    @pytest.mark.asyncio
    async def test_start_returns_202(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 10
                mock_agent.session_completion_tokens = 5
                mock_agent.session_total_tokens = 15
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                assert data["status"] == "started"
                assert data["run_id"].startswith("run_")

                status_resp = await cli.get(f"/v1/runs/{data['run_id']}")
                assert status_resp.status == 200
                status = await status_resp.json()
                assert status["run_id"] == data["run_id"]
                assert status["status"] in {"queued", "running", "completed"}
                assert status["object"] == "hermes.run"

    @pytest.mark.asyncio
    async def test_start_invalid_json_returns_400(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/runs",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_start_missing_input_returns_400(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs", json={"model": "test"})
            assert resp.status == 400
            data = await resp.json()
            assert "input" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_start_empty_input_returns_400(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs", json={"input": ""})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_start_disabled_tool_execution_passes_empty_toolsets(self, adapter):
        """只读 run 必须在创建 Agent 时机械传入空工具集。"""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "仅汇报结果", "tool_execution": "disabled"},
                )
                assert resp.status == 202
                run_id = (await resp.json())["run_id"]
                for _ in range(20):
                    status = await (await cli.get(f"/v1/runs/{run_id}")).json()
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

                mock_create.assert_called_once()
                assert mock_create.call_args.kwargs["enabled_toolsets_override"] == []

    @pytest.mark.asyncio
    async def test_start_default_tool_execution_keeps_configured_toolsets(
        self, adapter
    ):
        """未声明硬策略时必须保持现有平台工具集解析行为。"""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "正常任务"})
                assert resp.status == 202
                run_id = (await resp.json())["run_id"]
                for _ in range(20):
                    status = await (await cli.get(f"/v1/runs/{run_id}")).json()
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

                mock_create.assert_called_once()
                assert mock_create.call_args.kwargs["enabled_toolsets_override"] is None

    @pytest.mark.asyncio
    async def test_start_invalid_tool_execution_returns_400_before_allocating_run(
        self, adapter
    ):
        """非法工具执行策略不得静默回落到启用工具。"""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/runs",
                json={"input": "hello", "tool_execution": "read_only"},
            )
        assert resp.status == 400
        assert adapter._run_streams == {}
        assert adapter._run_statuses == {}

    @pytest.mark.asyncio
    async def test_start_same_dispatch_id_reuses_run_without_duplicate_agent(
        self, adapter
    ):
        """同一稳定派发重复 POST 只能返回原 run，不能再次调用模型。"""
        app = _create_runs_app(adapter)
        adapter._max_concurrent_runs = 1
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, ready, interrupted = _make_slow_agent()
                mock_create.return_value = mock_agent
                body = {
                    "input": "仅汇报结果",
                    "dispatch_id": "finance_cb_run_1_v1",
                    "tool_execution": "disabled",
                }

                first = await cli.post("/v1/runs", json=body)
                assert ready.wait(timeout=3.0)
                second = await cli.post("/v1/runs", json=body)
                assert first.status == 202
                assert second.status == 202
                first_data = await first.json()
                second_data = await second.json()
                assert first_data["run_id"] == second_data["run_id"]
                assert first_data["run_id"].startswith("run_d_")
                assert second_data["status"] == "already_exists"

                interrupted.set()
                await (await cli.get(
                    f"/v1/runs/{first_data['run_id']}/events"
                )).text()
                mock_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_same_dispatch_id_with_changed_payload_returns_409(
        self, adapter
    ):
        """幂等键复用但正文漂移必须显式冲突，不能误认成同一次派发。"""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, ready, interrupted = _make_slow_agent()
                mock_create.return_value = mock_agent
                first = await cli.post(
                    "/v1/runs",
                    json={"input": "原始正文", "dispatch_id": "dispatch_same"},
                )
                assert first.status == 202
                assert ready.wait(timeout=3.0)

                conflict = await cli.post(
                    "/v1/runs",
                    json={"input": "被篡改正文", "dispatch_id": "dispatch_same"},
                )
                assert conflict.status == 409
                data = await conflict.json()
                assert data["error"]["code"] == "idempotency_conflict"
                mock_create.assert_called_once()
                interrupted.set()

    @pytest.mark.asyncio
    async def test_start_invalid_dispatch_id_returns_400_before_allocating_run(
        self, adapter
    ):
        """派发幂等键只接受受限 ASCII，避免路由、日志和内存键污染。"""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/runs",
                json={"input": "hello", "dispatch_id": "含 空格"},
            )
        assert resp.status == 400
        assert adapter._run_streams == {}
        assert adapter._run_statuses == {}

    @pytest.mark.asyncio
    async def test_start_invalid_history_does_not_allocate_run(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/runs",
                json={"input": "hello", "conversation_history": {"role": "user"}},
            )
        assert resp.status == 400
        assert adapter._run_streams == {}
        assert adapter._run_statuses == {}

    @pytest.mark.asyncio
    async def test_runs_layers_body_system_prompt_into_ephemeral(self, adapter):
        """HASN 对话派发契约：body.system_prompt（daemon prompt_assembly 组装的
        IdentityPeer 等会话稳定片段）必须到达 agent 的 ephemeral system prompt。
        回归背景：此前 runs 路径只读 instructions，system_prompt 被静默丢弃，
        分身从不知道当前对话方是谁、把好友当主人称呼。"""
        app = _create_runs_app(adapter)
        identity_block = "【当前对话对象 ★必读】你正在与「菌子」对话，对方是好友，绝不是你的主人本人。"
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create, patch.object(
                adapter, "_ensure_session_db", return_value=None
            ):
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "ok"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post(
                    "/v1/runs",
                    json={
                        "input": "在吗",
                        "session_id": "conv_x__peer_h_friend",
                        "system_prompt": identity_block,
                    },
                )
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]
                for _ in range(20):
                    status_resp = await cli.get(f"/v1/runs/{run_id}")
                    status = await status_resp.json()
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

                mock_create.assert_called_once()
                assert (
                    mock_create.call_args.kwargs["ephemeral_system_prompt"]
                    == identity_block
                )

    @pytest.mark.asyncio
    async def test_runs_combines_system_prompt_before_instructions(self, adapter):
        """system_prompt（对话身份框定）在前、instructions（任务级）在后拼接。"""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create, patch.object(
                adapter, "_ensure_session_db", return_value=None
            ):
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "ok"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post(
                    "/v1/runs",
                    json={
                        "input": "hi",
                        "system_prompt": "身份框定",
                        "instructions": "任务指令",
                    },
                )
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]
                for _ in range(20):
                    status_resp = await cli.get(f"/v1/runs/{run_id}")
                    status = await status_resp.json()
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

                assert (
                    mock_create.call_args.kwargs["ephemeral_system_prompt"]
                    == "身份框定\n\n任务指令"
                )

    @pytest.mark.asyncio
    async def test_runs_rejects_non_string_system_prompt(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/runs",
                json={"input": "hi", "system_prompt": 123},
            )
        assert resp.status == 400
        assert adapter._run_streams == {}

    @pytest.mark.asyncio
    async def test_start_requires_auth(self, auth_adapter):
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs", json={"input": "hello"})
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_start_with_valid_auth(self, auth_adapter):
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(auth_adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "ok"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hello"},
                    headers={"Authorization": "Bearer sk-secret"},
                )
                assert resp.status == 202


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id} — poll run status
# ---------------------------------------------------------------------------


class TestRunStatus:
    @pytest.mark.asyncio
    async def test_status_completed_run_includes_output_and_usage(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 4
                mock_agent.session_completion_tokens = 2
                mock_agent.session_total_tokens = 6
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "hello"})
                data = await resp.json()
                run_id = data["run_id"]

                for _ in range(20):
                    status_resp = await cli.get(f"/v1/runs/{run_id}")
                    assert status_resp.status == 200
                    status = await status_resp.json()
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

                assert status["status"] == "completed"
                assert status["output"] == "done"
                assert status["usage"]["total_tokens"] == 6
                assert status["last_event"] == "run.completed"

    @pytest.mark.asyncio
    async def test_status_reflects_explicit_session_id(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hello", "session_id": "space-session"},
                )
                data = await resp.json()
                run_id = data["run_id"]

                for _ in range(20):
                    status_resp = await cli.get(f"/v1/runs/{run_id}")
                    status = await status_resp.json()
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

                mock_agent.run_conversation.assert_called_once()
                assert mock_agent.run_conversation.call_args.kwargs["task_id"] == "space-session"
                assert status["session_id"] == "space-session"

    @pytest.mark.asyncio
    async def test_runs_reloads_history_from_db_for_explicit_session(self, adapter):
        """策略 A：给定 body.session_id 且无 conversation_history 时，从 state.db
        按 session_id 回载历史喂给 run_conversation，实现跨消息会话续接。"""
        app = _create_runs_app(adapter)
        prior_history = [
            {"role": "user", "content": "我叫小智"},
            {"role": "assistant", "content": "你好小智"},
        ]
        mock_db = MagicMock()
        mock_db.get_messages_as_conversation.return_value = prior_history
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create, patch.object(
                adapter, "_ensure_session_db", return_value=mock_db
            ):
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "记得"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "我叫什么", "session_id": "sess_continuity"},
                )
                data = await resp.json()
                run_id = data["run_id"]
                for _ in range(20):
                    status_resp = await cli.get(f"/v1/runs/{run_id}")
                    status = await status_resp.json()
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

                mock_db.get_messages_as_conversation.assert_called_once_with("sess_continuity")
                mock_agent.run_conversation.assert_called_once()
                assert (
                    mock_agent.run_conversation.call_args.kwargs["conversation_history"]
                    == prior_history
                )

    @pytest.mark.asyncio
    async def test_runs_skips_db_reload_when_body_history_present(self, adapter):
        """body 已带 conversation_history 时不从 db 覆盖（优先调用方显式历史）。"""
        app = _create_runs_app(adapter)
        explicit = [{"role": "user", "content": "显式历史"}]
        mock_db = MagicMock()
        mock_db.get_messages_as_conversation.return_value = [
            {"role": "user", "content": "DB历史"}
        ]
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create, patch.object(
                adapter, "_ensure_session_db", return_value=mock_db
            ):
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "ok"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post(
                    "/v1/runs",
                    json={
                        "input": "hi",
                        "session_id": "sess_x",
                        "conversation_history": explicit,
                    },
                )
                data = await resp.json()
                run_id = data["run_id"]
                for _ in range(20):
                    status_resp = await cli.get(f"/v1/runs/{run_id}")
                    status = await status_resp.json()
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

                mock_db.get_messages_as_conversation.assert_not_called()
                assert (
                    mock_agent.run_conversation.call_args.kwargs["conversation_history"]
                    == explicit
                )

    @pytest.mark.asyncio
    async def test_status_not_found_returns_404(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/v1/runs/run_nonexistent")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_status_requires_auth(self, auth_adapter):
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/v1/runs/run_any")
        assert resp.status == 401


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id}/events — SSE event stream
# ---------------------------------------------------------------------------


class TestRunEvents:
    @pytest.mark.asyncio
    async def test_events_stream_returns_completed(self, adapter):
        """Events stream should receive run.completed when agent finishes."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "Hello!"}
                mock_agent.session_prompt_tokens = 10
                mock_agent.session_completion_tokens = 5
                mock_agent.session_total_tokens = 15
                mock_create.return_value = mock_agent

                # Start run
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                # Subscribe to events
                events_resp = await cli.get(f"/v1/runs/{run_id}/events")
                assert events_resp.status == 200
                body = await events_resp.text()

                # Should contain run.completed
                assert "run.completed" in body
                assert "Hello!" in body

    @pytest.mark.asyncio
    async def test_events_replays_terminal_status_after_transport_was_removed(
        self, adapter
    ):
        """客户端丢失原 SSE 后，终态必须能从保留状态确定性重放。"""
        app = _create_runs_app(adapter)
        run_id = "run_d_terminal_replay"
        adapter._set_run_status(
            run_id,
            "completed",
            output="已完成",
            usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            last_event="run.completed",
        )

        async with TestClient(TestServer(app)) as cli:
            response = await cli.get(f"/v1/runs/{run_id}/events")
            assert response.status == 200
            body = await response.text()

        assert "run.completed" in body
        data_line = next(line for line in body.splitlines() if line.startswith("data: "))
        assert json.loads(data_line.removeprefix("data: "))["output"] == "已完成"

    @pytest.mark.asyncio
    async def test_events_reconnects_to_active_run_after_transport_was_removed(
        self, adapter
    ):
        """活跃 run 的新订阅必须接管后续终态，不能因旧队列丢失而永久 404。"""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, ready, interrupted = _make_slow_agent()
                mock_create.return_value = mock_agent
                started = await cli.post(
                    "/v1/runs",
                    json={"input": "hello", "dispatch_id": "dispatch_reconnect"},
                )
                run_id = (await started.json())["run_id"]
                assert ready.wait(timeout=3.0)
                adapter._run_streams.pop(run_id)
                adapter._run_streams_created.pop(run_id)

                events_task = asyncio.create_task(
                    cli.get(f"/v1/runs/{run_id}/events")
                )
                await asyncio.sleep(0)
                interrupted.set()
                response = await asyncio.wait_for(events_task, timeout=5.0)
                assert response.status == 200
                body = await asyncio.wait_for(response.text(), timeout=5.0)

        assert "run.completed" in body


    @pytest.mark.asyncio
    async def test_approval_response_without_pending_returns_409(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "hello"})
                data = await resp.json()
                run_id = data["run_id"]

                approval_resp = await cli.post(
                    f"/v1/runs/{run_id}/approval",
                    json={"choice": "once"},
                )
                assert approval_resp.status == 409
                approval_data = await approval_resp.json()
                assert approval_data["error"]["code"] in {
                    "approval_not_active",
                    "approval_not_pending",
                }

    @pytest.mark.asyncio
    async def test_approval_string_false_does_not_resolve_all(self, adapter):
        """Quoted false must not fan out approval resolution across the queue."""
        app = _create_runs_app(adapter)
        run_id = "run_bool_parse"
        adapter._run_statuses[run_id] = {"run_id": run_id, "status": "running"}
        adapter._run_approval_sessions[run_id] = "session-123"

        async with TestClient(TestServer(app)) as cli:
            with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
                approval_resp = await cli.post(
                    f"/v1/runs/{run_id}/approval",
                    json={"choice": "once", "all": "false"},
                )

        assert approval_resp.status == 200
        mock_resolve.assert_called_once_with(
            "session-123",
            "once",
            resolve_all=False,
        )

    @pytest.mark.asyncio
    async def test_empty_agent_response_is_failed_run(self, adapter):
        """Hermes must not report its sentinel '(empty)' as a successful run."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "(empty)"}
                mock_agent.session_prompt_tokens = 10
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 10
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                events_resp = await cli.get(f"/v1/runs/{run_id}/events")
                assert events_resp.status == 200
                body = await events_resp.text()

                assert "run.failed" in body
                assert "empty response" in body

    async def _run_and_capture_hasn_session(self, adapter, cli, body):
        """跑一个 run，回收 run_conversation 执行线程里看到的 _hasn_session_id。"""
        from gateway.hasn_session import get_hasn_session_id, get_hasn_work_session_id

        seen = {}

        def _capture(**_kwargs):
            # 分身真正干活的线程——本地 hasn MCP 工具就是在这里读会话戳的
            seen["session_id"] = get_hasn_session_id()
            # 会话轴分流（设计 02 §4.3）：工作会话轨同点回收，两轨互不串扰才成立。
            seen["work_session_id"] = get_hasn_work_session_id()
            return {"final_response": "ok"}

        with patch.object(adapter, "_create_agent") as mock_create:
            mock_agent = MagicMock()
            mock_agent.run_conversation.side_effect = _capture
            mock_agent.session_prompt_tokens = 0
            mock_agent.session_completion_tokens = 0
            mock_agent.session_total_tokens = 0
            mock_create.return_value = mock_agent

            resp = await cli.post("/v1/runs", json=body)
            assert resp.status == 202
            run_id = (await resp.json())["run_id"]
            # 拉完事件流 = 等 run 真的跑完，之后 seen 才有值
            await (await cli.get(f"/v1/runs/{run_id}/events")).text()
        return seen

    @pytest.mark.asyncio
    async def test_hasn_session_id_bound_to_run_thread(self, adapter):
        """唤星会话绑定：daemon 透传的 session_id 必须绑到 run 的执行线程。

        断链后果：本地 hasn MCP 工具拿不到 _hasn_session_id，分身的提问卡/产物
        回不到发起它的那个 session。上游把 run 包进 _profile_scope 时正好撞这段
        （2026-07-16 合并冲突 #2/#3），故钉死。
        """
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            seen = await self._run_and_capture_hasn_session(
                adapter, cli, {"input": "hi", "session_id": "conv_x__peer_h_friend"}
            )
        assert seen["session_id"] == "conv_x__peer_h_friend"

    @pytest.mark.asyncio
    async def test_hasn_session_id_not_bound_without_explicit_session(self, adapter):
        """没送 session_id 就不绑——run_id 兜底不是真实会话，绑了反而误导分身。"""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            seen = await self._run_and_capture_hasn_session(adapter, cli, {"input": "hi"})
        assert seen["session_id"] is None

    @pytest.mark.asyncio
    async def test_hasn_session_id_reset_after_run(self, adapter, monkeypatch):
        """finally 里必须 reset——线程池会复用线程，不 reset 会把上一个会话的
        戳串给下一个 run（发错 session 的经典形状）。"""
        import gateway.hasn_session as hasn_session

        resets = []
        real_reset = hasn_session.reset_hasn_session_id
        monkeypatch.setattr(
            hasn_session,
            "reset_hasn_session_id",
            lambda token: (resets.append(token), real_reset(token))[1],
        )

        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            await self._run_and_capture_hasn_session(
                adapter, cli, {"input": "hi", "session_id": "conv_reset_me"}
            )
        assert len(resets) == 1

    @pytest.mark.asyncio
    async def test_hasn_work_session_id_bound_to_run_thread(self, adapter):
        """会话轴分流（设计 02 §4.3）：daemon 透传的 work_session_id 必须绑到 run 的
        执行线程，与 session_id（运行时轴）分轨共存。

        断链后果：工作会话派发的工具调用盖不上 _hasn_work_session_id，云端产物
        落不进工作会话列——主人在会话资源栏看不到分身刚干的活。
        """
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            seen = await self._run_and_capture_hasn_session(
                adapter,
                cli,
                {
                    "input": "hi",
                    "session_id": "rt_conv_1",
                    "work_session_id": "ws_task_1",
                },
            )
        # 两轨分立：runtime 值落 session 轨、工作会话值落 work_session 轨。
        assert seen["session_id"] == "rt_conv_1"
        assert seen["work_session_id"] == "ws_task_1"

    @pytest.mark.asyncio
    async def test_hasn_work_session_id_not_bound_for_im_main_session(self, adapter):
        """IM 主会话派发形态（body 无 work_session_id）：工作会话轨必须保持 None——
        否则 stamp 会把主会话产物错误盖进某个工作会话资源栏。"""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            seen = await self._run_and_capture_hasn_session(
                adapter, cli, {"input": "hi", "session_id": "rt_conv_main"}
            )
        assert seen["session_id"] == "rt_conv_main"
        assert seen["work_session_id"] is None

    @pytest.mark.asyncio
    async def test_hasn_work_session_id_reset_after_run(self, adapter, monkeypatch):
        """工作会话轨同样必须 finally reset——线程池复用线程，不 reset 会把上一个
        工作会话的戳串给下一个 run（主会话产物被错挂进旧工作会话资源栏）。"""
        import gateway.hasn_session as hasn_session

        resets = []
        real_reset = hasn_session.reset_hasn_work_session_id
        monkeypatch.setattr(
            hasn_session,
            "reset_hasn_work_session_id",
            lambda token: (resets.append(token), real_reset(token))[1],
        )

        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            await self._run_and_capture_hasn_session(
                adapter,
                cli,
                {"input": "hi", "work_session_id": "ws_reset_me"},
            )
        assert len(resets) == 1

    @pytest.mark.asyncio
    async def test_approval_resolve_all_is_scoped_to_target_run(self, auth_adapter):
        """Same client session_id must not let one run approve another run's queue."""
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(auth_adapter, "_create_agent") as mock_create:
                victim_agent, victim_ready, victim_interrupted = _make_slow_agent()
                attacker_agent, attacker_ready, attacker_interrupted = _make_slow_agent()
                mock_create.side_effect = [victim_agent, attacker_agent]

                victim_resp = await cli.post(
                    "/v1/runs",
                    json={"input": "victim", "session_id": "shared-project"},
                    headers={"Authorization": "Bearer sk-secret"},
                )
                attacker_resp = await cli.post(
                    "/v1/runs",
                    json={"input": "attacker", "session_id": "shared-project"},
                    headers={"Authorization": "Bearer sk-secret"},
                )
                assert victim_resp.status == 202
                assert attacker_resp.status == 202
                victim_run = (await victim_resp.json())["run_id"]
                attacker_run = (await attacker_resp.json())["run_id"]

                victim_ready.wait(timeout=3.0)
                attacker_ready.wait(timeout=3.0)
                assert auth_adapter._run_approval_sessions[victim_run] == victim_run
                assert auth_adapter._run_approval_sessions[attacker_run] == attacker_run
                assert auth_adapter._run_approval_sessions[victim_run] != auth_adapter._run_approval_sessions[attacker_run]

                victim_entry = approval_mod._ApprovalEntry({
                    "command": "bash -c victim-danger",
                    "description": "victim approval",
                    "pattern_keys": ["shell-c"],
                })
                attacker_entry = approval_mod._ApprovalEntry({
                    "command": "bash -c attacker-danger",
                    "description": "attacker approval",
                    "pattern_keys": ["shell-c"],
                })
                with approval_mod._lock:
                    approval_mod._gateway_queues[victim_run] = [victim_entry]
                    approval_mod._gateway_queues[attacker_run] = [attacker_entry]

                approval_resp = await cli.post(
                    f"/v1/runs/{attacker_run}/approval",
                    json={"choice": "always", "resolve_all": True},
                    headers={"Authorization": "Bearer sk-secret"},
                )
                approval_data = await approval_resp.json()

                assert approval_resp.status == 200
                assert approval_data["resolved"] == 1
                assert attacker_entry.result == "always"
                assert attacker_entry.event.is_set()
                assert victim_entry.result is None
                assert not victim_entry.event.is_set()
                with approval_mod._lock:
                    assert approval_mod._gateway_queues[victim_run] == [victim_entry]
                    assert victim_run in approval_mod._gateway_queues
                    assert attacker_run not in approval_mod._gateway_queues

                # Clean up the synthetic pending victim approval and unblock the
                # slow test agents so their background run tasks can finish.
                with approval_mod._lock:
                    approval_mod._gateway_queues.pop(victim_run, None)
                victim_interrupted.set()
                attacker_interrupted.set()

    @pytest.mark.asyncio
    async def test_events_not_found_returns_404(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/v1/runs/run_nonexistent/events")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_events_requires_auth(self, auth_adapter):
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/v1/runs/run_any/events")
        assert resp.status == 401


# ---------------------------------------------------------------------------
# Run lifecycle TTL sweeping
# ---------------------------------------------------------------------------


class TestRunLifecycleSweep:
    def test_sweep_keeps_transport_with_active_subscriber(self, adapter):
        run_id = "run_subscribed"
        queue = asyncio.Queue()
        adapter._run_streams[run_id] = queue
        adapter._run_streams_created[run_id] = 0
        adapter._run_stream_subscribers.add(run_id)

        adapter._sweep_orphaned_runs_once(time.time())

        assert adapter._run_streams[run_id] is queue
        assert run_id in adapter._run_streams_created

    @pytest.mark.asyncio
    async def test_expired_live_run_drops_transport_but_keeps_control_state(self, adapter):
        """Stream TTL bounds buffering without detaching a live run."""
        app = _create_runs_app(adapter)
        adapter._max_concurrent_runs = 1

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, _ = _make_slow_agent()
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert start_resp.status == 202
                run_id = (await start_resp.json())["run_id"]
                assert agent_ready.wait(timeout=3.0)

                task = adapter._active_run_tasks[run_id]
                assert isinstance(task, asyncio.Task)
                assert not task.done()

                pending = approval_mod._ApprovalEntry({
                    "command": "bash -c long-running",
                    "description": "approval after stream TTL",
                    "pattern_keys": ["shell-c"],
                })
                with approval_mod._lock:
                    approval_mod._gateway_queues[run_id] = [pending]

                adapter._run_streams_created[run_id] -= adapter._RUN_STREAM_TTL + 1
                # Exercise one real sweeper iteration without waiting 60 seconds.
                with patch(
                    "gateway.platforms.api_server.asyncio.sleep",
                    side_effect=[None, asyncio.CancelledError()],
                ):
                    with pytest.raises(asyncio.CancelledError):
                        await adapter._sweep_orphaned_runs()

                assert adapter._active_run_tasks[run_id] is task
                assert adapter._active_run_agents[run_id] is mock_agent
                assert run_id not in adapter._run_streams
                assert run_id not in adapter._run_streams_created
                assert adapter._run_approval_sessions[run_id] == run_id

                limited = adapter._concurrency_limited_response()
                assert limited is not None
                assert limited.status == 429

                approval_resp = await cli.post(
                    f"/v1/runs/{run_id}/approval",
                    json={"choice": "once"},
                )
                assert approval_resp.status == 200
                assert pending.event.is_set()
                assert pending.result == "once"

                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200
                mock_agent.interrupt.assert_called_once_with("Stop requested via API")

    @pytest.mark.asyncio
    async def test_expired_transport_stops_buffering_new_deltas(self, adapter):
        """An unconsumed expired queue must not grow for the rest of a live run."""
        app = _create_runs_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, _ = _make_slow_agent()
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await start_resp.json())["run_id"]
                assert agent_ready.wait(timeout=3.0)
                expired_queue = adapter._run_streams[run_id]
                stream_delta = mock_create.call_args.kwargs["stream_delta_callback"]

                adapter._run_streams_created[run_id] -= adapter._RUN_STREAM_TTL + 1
                adapter._sweep_orphaned_runs_once(time.time())
                before = expired_queue.qsize()
                stream_delta("must-not-buffer")
                mock_agent.interrupt("finish test")
                for _ in range(40):
                    if run_id not in adapter._active_run_tasks:
                        break
                    await asyncio.sleep(0.05)

                assert expired_queue.qsize() == before

    @pytest.mark.asyncio
    async def test_expired_orphan_run_state_is_reaped(self, adapter):
        run_id = "run_expired_orphan"
        adapter._run_streams[run_id] = asyncio.Queue()
        adapter._run_streams_created[run_id] = 0
        adapter._run_approval_sessions[run_id] = run_id

        pending = approval_mod._ApprovalEntry({
            "command": "bash -c orphaned",
            "description": "orphaned approval",
            "pattern_keys": ["shell-c"],
        })
        with approval_mod._lock:
            approval_mod._gateway_queues[run_id] = [pending]

        with patch(
            "gateway.platforms.api_server.asyncio.sleep",
            side_effect=[None, asyncio.CancelledError()],
        ):
            with pytest.raises(asyncio.CancelledError):
                await adapter._sweep_orphaned_runs()

        assert run_id not in adapter._run_streams
        assert run_id not in adapter._run_streams_created
        assert run_id not in adapter._run_approval_sessions
        assert pending.event.is_set()
        with approval_mod._lock:
            assert run_id not in approval_mod._gateway_queues


# ---------------------------------------------------------------------------
# POST /v1/runs/{run_id}/stop — interrupt a running agent
# ---------------------------------------------------------------------------


class TestStopRun:
    @pytest.mark.asyncio
    async def test_stop_before_agent_creation_prevents_run_start(self, adapter):
        """A stop accepted while queued must prevent agent construction."""
        app = _create_runs_app(adapter)
        original_create_task = asyncio.create_task
        task_started = asyncio.Event()
        allow_task = asyncio.Event()

        def _delayed_create_task(coro):
            async def _delayed():
                task_started.set()
                await allow_task.wait()
                return await coro

            return original_create_task(_delayed())

        with patch("gateway.platforms.api_server.asyncio.create_task", side_effect=_delayed_create_task), \
             patch.object(adapter, "_create_agent") as mock_create:
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await resp.json())["run_id"]
                await task_started.wait()

                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200
                allow_task.set()

                for _ in range(20):
                    if run_id not in adapter._active_run_tasks:
                        break
                    await asyncio.sleep(0.05)

                mock_create.assert_not_called()
                assert adapter._run_statuses[run_id]["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_stop_keeps_uncooperative_executor_tracked_until_exit(self, adapter):
        """Cancelling an asyncio wrapper must not hide its live executor thread."""
        app = _create_runs_app(adapter)
        run_can_finish = threading.Event()
        run_finished = threading.Event()

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                started = threading.Event()

                def _run_conversation(*_args, **_kwargs):
                    started.set()
                    run_can_finish.wait(timeout=5)
                    run_finished.set()
                    return {"final_response": "late result"}

                mock_agent.run_conversation.side_effect = _run_conversation
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await resp.json())["run_id"]
                assert started.wait(timeout=3)

                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200
                await asyncio.sleep(0.1)

                assert not run_finished.is_set()
                assert run_id in adapter._active_run_agents
                assert run_id in adapter._active_run_tasks
                assert adapter._run_statuses[run_id]["status"] == "stopping"

                run_can_finish.set()
                for _ in range(40):
                    if run_id not in adapter._active_run_tasks:
                        break
                    await asyncio.sleep(0.05)

                assert run_id not in adapter._active_run_agents
                assert run_id not in adapter._active_run_tasks
                assert adapter._run_statuses[run_id]["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_stop_running_agent(self, adapter):
        """Stop should interrupt the agent and cancel the task."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, _ = _make_slow_agent()
                mock_create.return_value = mock_agent

                # Start run
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                # Wait for agent to start running in the thread
                agent_ready.wait(timeout=3.0)
                await asyncio.sleep(0.1)

                # Verify agent ref is stored
                assert run_id in adapter._active_run_agents

                # Stop the run
                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200
                stop_data = await stop_resp.json()
                assert stop_data["run_id"] == run_id
                assert stop_data["status"] == "stopping"

                # Agent interrupt should have been called
                mock_agent.interrupt.assert_called_once_with("Stop requested via API")

                status_resp = await cli.get(f"/v1/runs/{run_id}")
                assert status_resp.status == 200
                status_data = await status_resp.json()
                assert status_data["status"] in {"stopping", "cancelled"}

                # Refs should be cleaned up
                await asyncio.sleep(0.5)
                assert run_id not in adapter._active_run_agents
                assert run_id not in adapter._active_run_tasks

    @pytest.mark.asyncio
    async def test_stop_nonexistent_run_returns_404(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_nonexistent/stop")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_stop_requires_auth(self, auth_adapter):
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_any/stop")
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_stop_already_completed_run_returns_404(self, adapter):
        """Stopping a run that already finished should return 404 (refs cleaned up)."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                # Start and wait for completion
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                await asyncio.sleep(0.3)

                # Run should be done, refs cleaned up
                assert run_id not in adapter._active_run_agents

                # Stop should return 404
                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 404

    @pytest.mark.asyncio
    async def test_stop_interrupt_exception_does_not_crash(self, adapter):
        """If agent.interrupt() raises, stop should still succeed."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, interrupted = _make_slow_agent()

                # Override the interrupt side_effect to raise. Still trip
                # ``interrupted`` so the slow_run thread unblocks at teardown
                # — without this the agent thread blocks the full 10s
                # timeout and the test teardown waits the same amount.
                def _raising_interrupt(message=None):
                    interrupted.set()
                    raise RuntimeError("interrupt failed")

                mock_agent.interrupt = MagicMock(side_effect=_raising_interrupt)
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                agent_ready.wait(timeout=3.0)
                await asyncio.sleep(0.1)

                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200
                stop_data = await stop_resp.json()
                assert stop_data["status"] == "stopping"

    @pytest.mark.asyncio
    async def test_stop_sends_sentinel_to_events_stream(self, adapter):
        """After stop, the events stream should close."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, _ = _make_slow_agent()
                mock_create.return_value = mock_agent

                # Start run
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                agent_ready.wait(timeout=3.0)
                await asyncio.sleep(0.1)

                # Subscribe to events in background
                events_task = asyncio.ensure_future(
                    cli.get(f"/v1/runs/{run_id}/events")
                )

                await asyncio.sleep(0.1)

                # Stop the run
                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200

                # Events stream should close
                events_resp = await asyncio.wait_for(events_task, timeout=5.0)
                assert events_resp.status == 200
                body = await events_resp.text()
                # Stream should have received run.failed and closed
                assert "run.failed" in body or "stream closed" in body
