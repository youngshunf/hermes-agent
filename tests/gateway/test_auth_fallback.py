"""Test that AuthError triggers fallback provider resolution (#7230)."""

from unittest.mock import patch

import pytest


class TestResolveRuntimeAgentKwargsAuthFallback:
    """_resolve_runtime_agent_kwargs should try fallback on AuthError."""

    def test_auth_error_tries_fallback(self, tmp_path, monkeypatch):
        """When primary provider raises AuthError, fallback is attempted."""
        from hermes_cli.auth import AuthError

        # Create a config with fallback
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "model:\n  provider: openai-codex\n"
            "fallback_model:\n  provider: openrouter\n"
            "  model: meta-llama/llama-4-maverick\n"
        )

        monkeypatch.setattr("gateway.run._hermes_home", tmp_path)

        call_count = {"n": 0}

        def _mock_resolve(**kwargs):
            call_count["n"] += 1
            # First call = primary path (gateway reads model.provider from
            # config.yaml internally; we simulate the auth failure here).
            # Second call = fallback path with explicit_api_key + explicit_base_url
            # supplied by gateway from fallback_model config.
            if call_count["n"] == 1:
                raise AuthError("Codex token refresh failed with status 401")
            return {
                "api_key": "fallback-key",
                "base_url": "https://openrouter.ai/api/v1",
                "provider": "openrouter",
                "api_mode": "openai_chat",
                "command": None,
                "args": None,
                "credential_pool": None,
            }

        with patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            side_effect=_mock_resolve,
        ):
            from gateway.run import _resolve_runtime_agent_kwargs
            result = _resolve_runtime_agent_kwargs()

        assert result["provider"] == "openrouter"
        assert result["api_key"] == "fallback-key"
        # Should have been called at least twice (primary + fallback)
        assert call_count["n"] >= 2

    def test_auth_error_no_fallback_raises(self, tmp_path, monkeypatch):
        """When primary fails and no fallback configured, RuntimeError is raised."""
        from hermes_cli.auth import AuthError

        config_path = tmp_path / "config.yaml"
        config_path.write_text("model:\n  provider: openai-codex\n")

        monkeypatch.setattr("gateway.run._hermes_home", tmp_path)

        with patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            side_effect=AuthError("token expired"),
        ):
            from gateway.run import _resolve_runtime_agent_kwargs
            with pytest.raises(RuntimeError):
                _resolve_runtime_agent_kwargs()

    def test_legacy_fallback_is_appended_after_fallback_providers(self, tmp_path, monkeypatch):
        """When both keys exist, the legacy entry still participates in resolution."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "fallback_providers:\n"
            "  - provider: openrouter\n"
            "    model: anthropic/claude-sonnet-4.6\n"
            "fallback_model:\n"
            "  provider: nous\n"
            "  model: Hermes-4\n"
        )

        monkeypatch.setattr("gateway.run._hermes_home", tmp_path)

        calls = []

        def _mock_resolve(**kwargs):
            requested = kwargs.get("requested")
            calls.append(requested)
            if requested == "openrouter":
                raise RuntimeError("openrouter unavailable")
            return {
                "api_key": "nous-key",
                "base_url": "https://portal.nousresearch.com/v1",
                "provider": "nous",
                "api_mode": "chat_completions",
                "command": None,
                "args": None,
                "credential_pool": None,
            }

        with patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            side_effect=_mock_resolve,
        ):
            from gateway.run import _try_resolve_fallback_provider

            result = _try_resolve_fallback_provider()

        assert calls == ["openrouter", "nous"]
        assert result["provider"] == "nous"
        assert result["model"] == "Hermes-4"

    def test_fallback_api_key_env_uses_profile_secret_scope(self, tmp_path, monkeypatch):
        """Fallback provider key_env/api_key_env must use the routed profile secret."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "fallback_providers:\n"
            "  - provider: custom\n"
            "    model: qwen3.7-plus\n"
            "    base_url: https://llm.dcfuture.cn/v1\n"
            "    api_key_env: OPENAI_API_KEY\n",
            encoding="utf-8",
        )

        monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        from agent.secret_scope import reset_secret_scope, set_multiplex_active, set_secret_scope

        token = set_secret_scope({"OPENAI_API_KEY": "sk-profile-owner"})
        set_multiplex_active(True)
        try:
            def _mock_resolve(**kwargs):
                assert kwargs["requested"] == "custom"
                assert kwargs["explicit_base_url"] == "https://llm.dcfuture.cn/v1"
                assert kwargs["explicit_api_key"] == "sk-profile-owner"
                return {
                    "api_key": kwargs["explicit_api_key"],
                    "base_url": kwargs["explicit_base_url"],
                    "provider": "custom",
                    "api_mode": "chat_completions",
                    "command": None,
                    "args": None,
                    "credential_pool": None,
                }

            with patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                side_effect=_mock_resolve,
            ):
                from gateway.run import _try_resolve_fallback_provider

                result = _try_resolve_fallback_provider()
        finally:
            reset_secret_scope(token)
            set_multiplex_active(False)

        assert result["api_key"] == "sk-profile-owner"

    def test_fallback_literal_env_template_uses_profile_secret_scope(self, tmp_path, monkeypatch):
        """Existing profiles with api_key: ${OPENAI_API_KEY} must not pass the literal token."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "fallback_providers:\n"
            "  - provider: custom\n"
            "    model: qwen3.7-plus\n"
            "    base_url: https://llm.dcfuture.cn/v1\n"
            "    api_key: ${OPENAI_API_KEY}\n",
            encoding="utf-8",
        )

        monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        from agent.secret_scope import reset_secret_scope, set_multiplex_active, set_secret_scope

        token = set_secret_scope({"OPENAI_API_KEY": "sk-profile-owner"})
        set_multiplex_active(True)
        try:
            def _mock_resolve(**kwargs):
                assert kwargs["explicit_api_key"] == "sk-profile-owner"
                return {
                    "api_key": kwargs["explicit_api_key"],
                    "base_url": kwargs["explicit_base_url"],
                    "provider": "custom",
                    "api_mode": "chat_completions",
                    "command": None,
                    "args": None,
                    "credential_pool": None,
                }

            with patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                side_effect=_mock_resolve,
            ):
                from gateway.run import _try_resolve_fallback_provider

                result = _try_resolve_fallback_provider()
        finally:
            reset_secret_scope(token)
            set_multiplex_active(False)

        assert result["api_key"] == "sk-profile-owner"
