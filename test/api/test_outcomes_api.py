"""Tests for POST/GET /outcomes (self-learning Phase 1 HTTP surface).

Mirrors test/api/test_memory_export_api.py: the learning gate is patched at
the settings seam; storage tests use a real OutcomeService on a tmp engine
patched into the service constructor's session factory.
"""

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base

LEARNING_TARGET = "cli_agent_orchestrator.services.settings_service.is_learning_enabled"


@pytest.fixture
def isolated_db(tmp_path):
    """Patch the global SessionLocal used by OutcomeService to a tmp engine."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'outcomes.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with patch("cli_agent_orchestrator.clients.database.SessionLocal", factory):
        yield engine


BODY = {
    "session_name": "ssis-batch-1",
    "task_label": "convert package CustomerETL",
    "success": False,
    "workflow_name": "ssis-migration",
    "agent_profile": "transformer",
    "score": 40,
    "friction_notes": "Lookup component with partial cache not mapped.",
}


class TestOutcomesGates:
    def test_post_disabled_is_404(self, client, isolated_db):
        with patch(LEARNING_TARGET, return_value=False):
            response = client.post("/outcomes", json=BODY)
        assert response.status_code == 404
        assert "disabled" in response.json()["detail"].lower()

    def test_get_disabled_is_404(self, client, isolated_db):
        with patch(LEARNING_TARGET, return_value=False):
            response = client.get("/outcomes")
        assert response.status_code == 404


class TestOutcomesRoundTrip:
    def test_post_then_get(self, client, isolated_db):
        with patch(LEARNING_TARGET, return_value=True):
            response = client.post("/outcomes", json=BODY)
            assert response.status_code == 200, response.text
            outcome = response.json()["outcome"]
            assert outcome["id"]
            assert outcome["success"] is False
            assert outcome["score"] == 40

            listed = client.get("/outcomes?session_name=ssis-batch-1")
            assert listed.status_code == 200
            data = listed.json()
            assert data["count"] == 1
            assert data["outcomes"][0]["task_label"] == "convert package CustomerETL"

    def test_get_filters_by_agent(self, client, isolated_db):
        with patch(LEARNING_TARGET, return_value=True):
            client.post("/outcomes", json=BODY)
            client.post(
                "/outcomes",
                json={**BODY, "agent_profile": "improver", "task_label": "patch backend"},
            )
            listed = client.get("/outcomes?agent_profile=improver")
            assert listed.json()["count"] == 1
            assert listed.json()["outcomes"][0]["agent_profile"] == "improver"

    def test_post_invalid_score_is_400(self, client, isolated_db):
        with patch(LEARNING_TARGET, return_value=True):
            response = client.post("/outcomes", json={**BODY, "score": 150})
        assert response.status_code == 400
        assert "score" in response.json()["detail"]

    def test_post_blank_task_label_is_400(self, client, isolated_db):
        with patch(LEARNING_TARGET, return_value=True):
            response = client.post("/outcomes", json={**BODY, "task_label": "  "})
        assert response.status_code == 400


class TestReportOutcomeMcpTool:
    """MCP tool surface — mirrors TestMcpToolsDisabledSurface in the U5 tests."""

    def _ctx(self):
        return {
            "terminal_id": "term-ro",
            "session_name": "sess-ro",
            "agent_profile": "transformer",
            "provider": "claude_code",
            "cwd": "/home/user/proj",
        }

    def test_disabled_returns_disabled_payload(self, isolated_db):
        import asyncio

        from cli_agent_orchestrator.mcp_server import server as srv
        from cli_agent_orchestrator.mcp_server.server import report_outcome

        with (
            patch(LEARNING_TARGET, return_value=False),
            patch.object(srv, "_get_terminal_context_from_env", return_value=self._ctx()),
        ):
            result = asyncio.run(report_outcome(task_label="t", success=True))

        assert result["success"] is False
        assert result["disabled"] is True

    def test_records_with_terminal_context_defaults(self, isolated_db):
        import asyncio

        from cli_agent_orchestrator.mcp_server import server as srv
        from cli_agent_orchestrator.mcp_server.server import report_outcome
        from cli_agent_orchestrator.services.outcome_service import OutcomeService

        with (
            patch(LEARNING_TARGET, return_value=True),
            patch.object(srv, "_get_terminal_context_from_env", return_value=self._ctx()),
        ):
            result = asyncio.run(
                report_outcome(
                    task_label="convert package X",
                    success=True,
                    workflow_name=None,
                    agent_profile=None,
                    score=90,
                    friction_notes="",
                )
            )
            assert result["success"] is True, result
            assert result["outcome_id"]

            rows = OutcomeService().list_outcomes(session_name="sess-ro")
        assert len(rows) == 1
        # agent_profile defaults from the calling terminal's context
        assert rows[0]["agent_profile"] == "transformer"
        assert rows[0]["source_terminal_id"] == "term-ro"

    def test_no_terminal_context_is_error(self, isolated_db):
        import asyncio

        from cli_agent_orchestrator.mcp_server import server as srv
        from cli_agent_orchestrator.mcp_server.server import report_outcome

        with (
            patch(LEARNING_TARGET, return_value=True),
            patch.object(srv, "_get_terminal_context_from_env", return_value=None),
        ):
            result = asyncio.run(report_outcome(task_label="t", success=True))

        assert result["success"] is False
        assert "terminal context" in result["error"]


class TestReportOutcomeErrorPaths:
    def test_service_exception_returns_error_payload(self, isolated_db):
        import asyncio

        from cli_agent_orchestrator.mcp_server import server as srv
        from cli_agent_orchestrator.mcp_server.server import report_outcome

        ctx = {
            "terminal_id": "t",
            "session_name": "s",
            "agent_profile": "a",
            "provider": "claude_code",
            "cwd": "/tmp",
        }
        with (
            patch(LEARNING_TARGET, return_value=True),
            patch.object(srv, "_get_terminal_context_from_env", return_value=ctx),
            patch(
                "cli_agent_orchestrator.services.outcome_service.OutcomeService.record_outcome",
                side_effect=RuntimeError("db locked"),
            ),
        ):
            result = asyncio.run(
                report_outcome(
                    task_label="t",
                    success=True,
                    workflow_name=None,
                    agent_profile=None,
                    score=None,
                    friction_notes="",
                )
            )
        assert result["success"] is False
        assert "db locked" in result["error"]

    def test_post_learning_disabled_error_from_service_is_404(self, client, isolated_db):
        """Race: gate passes but service raises LearningDisabledError."""
        from cli_agent_orchestrator.services.outcome_service import LearningDisabledError

        with (
            patch(LEARNING_TARGET, return_value=True),
            patch(
                "cli_agent_orchestrator.services.outcome_service.OutcomeService.record_outcome",
                side_effect=LearningDisabledError("disabled"),
            ),
        ):
            response = client.post("/outcomes", json=BODY)
        assert response.status_code == 404
