"""Tests for _resolve_child_allowed_tools in MCP server."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.utils.orchestration import _resolve_child_allowed_tools


class TestResolveChildAllowedTools:
    """Tests for _resolve_child_allowed_tools function."""

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_child_wildcard_with_restricted_parent_returns_unrestricted(
        self, mock_load, mock_resolve
    ):
        """Issue #141: child with allowedTools=["*"] should NOT inherit parent restrictions."""
        mock_profile = MagicMock()
        mock_profile.allowedTools = ["*"]
        mock_profile.role = "developer"
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile
        mock_resolve.return_value = ["*"]

        result = _resolve_child_allowed_tools(
            parent_allowed_tools=["@cao-mcp-server", "fs_read", "fs_list"],
            child_profile_name="code-reviewer",
        )

        assert result is None  # unrestricted

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_child_none_inherits_parent_restrictions(self, mock_load, mock_resolve):
        """Child with no profile (FileNotFoundError) inherits parent's tools."""
        mock_load.side_effect = FileNotFoundError("not found")

        result = _resolve_child_allowed_tools(
            parent_allowed_tools=["fs_read", "fs_list"],
            child_profile_name="nonexistent",
        )

        assert result == "fs_read,fs_list"

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_unrestricted_parent_uses_child_tools(self, mock_load, mock_resolve):
        """Unrestricted parent lets child use its own tools."""
        mock_profile = MagicMock()
        mock_profile.allowedTools = ["fs_read", "execute_bash"]
        mock_profile.role = None
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile
        mock_resolve.return_value = ["fs_read", "execute_bash"]

        result = _resolve_child_allowed_tools(
            parent_allowed_tools=None,
            child_profile_name="developer",
        )

        assert result == "fs_read,execute_bash"

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_both_restricted_uses_child_tools(self, mock_load, mock_resolve):
        """Both parent and child restricted: child gets its own tools."""
        mock_profile = MagicMock()
        mock_profile.allowedTools = ["fs_read", "execute_bash"]
        mock_profile.role = None
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile
        mock_resolve.return_value = ["fs_read", "execute_bash"]

        result = _resolve_child_allowed_tools(
            parent_allowed_tools=["@cao-mcp-server", "fs_read"],
            child_profile_name="developer",
        )

        assert result == "fs_read,execute_bash"

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_parent_wildcard_child_wildcard_returns_unrestricted(self, mock_load, mock_resolve):
        """Both parent and child unrestricted: returns None (unrestricted)."""
        mock_profile = MagicMock()
        mock_profile.allowedTools = ["*"]
        mock_profile.role = None
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile
        mock_resolve.return_value = ["*"]

        result = _resolve_child_allowed_tools(
            parent_allowed_tools=["*"],
            child_profile_name="developer",
        )

        # Parent is unrestricted, child has ["*"] → child_allowed is truthy → joins it
        # But ["*"] joined is "*", which is fine
        assert result == "*"


def _unreadable(name="correctness_reviewer"):
    """The RuntimeError load_agent_profile raises when the store read hits an OSError."""
    try:
        try:
            raise PermissionError(13, "Permission denied", f"agent-store/{name}.md")
        except PermissionError as e:
            raise RuntimeError(f"Failed to load agent profile '{name}': {e}")
    except RuntimeError as wrapped:
        return wrapped


class TestUnreadableProfileFallsBackToServer:
    """A sandboxed cao-mcp-server can't read the agent store; cao-server can."""

    @patch("cli_agent_orchestrator.utils.orchestration._mcp_timeout", return_value=5.0)
    @patch("cli_agent_orchestrator.utils.orchestration.requests.get")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_permission_denied_uses_server_profile(self, mock_load, mock_get, _timeout):
        mock_load.side_effect = _unreadable()
        mock_get.return_value = MagicMock(
            json=lambda: {
                "name": "correctness_reviewer",
                "description": "d",
                "role": "reviewer",
                "allowedTools": ["fs_read", "@cao-mcp-server"],
            }
        )

        result = _resolve_child_allowed_tools(
            parent_allowed_tools=["@cao-mcp-server", "fs_read"],
            child_profile_name="correctness_reviewer",
        )

        assert mock_get.call_args.args[0].endswith("/agents/profiles/correctness_reviewer")
        # Same answer the local read would give: the child's own profile tools.
        assert result == "fs_read,@cao-mcp-server"

    @patch("cli_agent_orchestrator.utils.orchestration._mcp_timeout", return_value=5.0)
    @patch("cli_agent_orchestrator.utils.orchestration.requests.get")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_server_fallback_failure_raises_original_error(self, mock_load, mock_get, _timeout):
        original = _unreadable()
        mock_load.side_effect = original
        mock_get.side_effect = ConnectionError("cao-server down")

        with pytest.raises(RuntimeError) as excinfo:
            _resolve_child_allowed_tools(["fs_read"], "correctness_reviewer")

        assert excinfo.value is original

    @patch("cli_agent_orchestrator.utils.orchestration.requests.get")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_non_os_error_does_not_fall_back(self, mock_load, mock_get):
        mock_load.side_effect = RuntimeError("Failed to load agent profile 'x': bad yaml")

        with pytest.raises(RuntimeError):
            _resolve_child_allowed_tools(["fs_read"], "x")

        mock_get.assert_not_called()
