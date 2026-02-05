# Unit Test Coverage Improvement Plan

## CLI Agent Orchestrator

*Last Updated: February 2025*

---

## Table of Contents

1. [Current State Analysis](#current-state-analysis)
2. [Recommended Improvement Plan](#recommended-improvement-plan)
3. [Implementation Recommendations](#implementation-recommendations)
4. [Quick Wins](#quick-wins)

---

## Current State Analysis

### Repository Overview

| Metric | Value |
|--------|-------|
| **Source Files** | 44 Python files |
| **Source LOC** | ~3,939 lines |
| **Test Files** | 10 test files |
| **Test LOC** | ~2,628 lines |
| **Test Framework** | pytest (7.4.0+) |
| **Coverage Tool** | pytest-cov (4.1.0+) |
| **Async Support** | pytest-asyncio (0.26.0+) |
| **Mocking** | pytest-mock (3.11.1+) |

### Test Structure

```
test/
├── __init__.py
├── api/
│   ├── __init__.py
│   ├── test_inbox_messages.py      # ✅ Comprehensive coverage
│   └── test_terminals.py           # ✅ API endpoint tests
├── cli/
│   ├── __init__.py
│   └── commands/
│       ├── __init__.py
│       └── test_launch.py          # ⚠️ Limited coverage
├── providers/
│   ├── __init__.py
│   ├── README.md
│   ├── fixtures/                   # Test fixture files
│   ├── test_codex_provider_unit.py     # ✅ Excellent coverage
│   ├── test_kiro_cli_unit.py           # ✅ Excellent coverage
│   ├── test_provider_manager_unit.py   # ✅ Basic coverage
│   ├── test_q_cli_integration.py       # ✅ Integration tests
│   ├── test_q_cli_unit.py              # ✅ 100% coverage
│   └── test_tmux_working_directory.py  # ✅ Focused tests
└── services/
    ├── __init__.py
    └── test_terminal_service.py    # ⚠️ Limited coverage
```

### Identified Gaps

#### Critical Gaps (High Priority)

| Module | Location | Gap Description |
|--------|----------|-----------------|
| **API Main** | `src/cli_agent_orchestrator/api/main.py` | Missing tests for session CRUD endpoints, flow daemon, lifespan events |
| **MCP Server** | `src/cli_agent_orchestrator/mcp_server/server.py` | No tests for handoff, assign, send_message tools |
| **Database Client** | `src/cli_agent_orchestrator/clients/database.py` | Missing tests for flow CRUD operations, terminal metadata operations |
| **Tmux Client** | `src/cli_agent_orchestrator/clients/tmux.py` | Limited tests for core tmux operations |

#### Moderate Gaps (Medium Priority)

| Module | Location | Gap Description |
|--------|----------|-----------------|
| **Flow Service** | `src/cli_agent_orchestrator/services/flow_service.py` | No dedicated tests (0% coverage) |
| **Session Service** | `src/cli_agent_orchestrator/services/session_service.py` | No dedicated tests (0% coverage) |
| **Inbox Service** | `src/cli_agent_orchestrator/services/inbox_service.py` | No dedicated tests (0% coverage) |
| **Cleanup Service** | `src/cli_agent_orchestrator/services/cleanup_service.py` | No dedicated tests (0% coverage) |
| **Terminal Service** | `src/cli_agent_orchestrator/services/terminal_service.py` | Minimal coverage |

#### Minor Gaps (Low Priority)

| Module | Location | Gap Description |
|--------|----------|-----------------|
| **CLI Commands** | `src/cli_agent_orchestrator/cli/commands/` | Only `launch.py` tested; flow.py, init.py, install.py, shutdown.py untested |
| **Utils** | `src/cli_agent_orchestrator/utils/` | Missing tests for agent_profiles.py, template.py, logging.py |
| **Models** | `src/cli_agent_orchestrator/models/` | Missing validation tests for Pydantic models |
| **Claude Code Provider** | `src/cli_agent_orchestrator/providers/claude_code.py` | No dedicated unit tests |

### Module Coverage Summary

| Module | Files | Estimated Coverage | Priority |
|--------|-------|-------------------|----------|
| `providers/` | 6 | ~85% | Low |
| `api/` | 2 | ~40% | **High** |
| `services/` | 5 | ~10% | **High** |
| `clients/` | 2 | ~20% | **High** |
| `mcp_server/` | 4 | ~0% | **High** |
| `cli/` | 7 | ~15% | Medium |
| `utils/` | 5 | ~20% | Medium |
| `models/` | 9 | ~5% | Low |

---

## Recommended Improvement Plan

### Phase 1: Core Services (Weeks 1-2)

**Goal:** Establish solid test coverage for business logic layer

#### 1.1 Flow Service (`flow_service.py`) - Priority: HIGH

Create `test/services/test_flow_service.py`:

```python
# Target Test Cases:
# - test_add_flow_success
# - test_add_flow_missing_required_field
# - test_add_flow_invalid_cron_expression
# - test_list_flows_empty
# - test_list_flows_multiple
# - test_get_flow_success
# - test_get_flow_not_found
# - test_remove_flow_success
# - test_remove_flow_not_found
# - test_enable_flow_success
# - test_disable_flow_success
# - test_execute_flow_with_script
# - test_execute_flow_without_script
# - test_execute_flow_script_returns_false
# - test_execute_flow_script_fails
# - test_get_flows_to_run
```

**Estimated Tests:** 16  
**Estimated LOC:** ~400

#### 1.2 Session Service (`session_service.py`) - Priority: HIGH

Create `test/services/test_session_service.py`:

```python
# Target Test Cases:
# - test_list_sessions_empty
# - test_list_sessions_with_cao_prefix_only
# - test_get_session_success
# - test_get_session_not_found
# - test_delete_session_success
# - test_delete_session_with_terminals
# - test_delete_session_not_found
# - test_list_sessions_error_handling
```

**Estimated Tests:** 8  
**Estimated LOC:** ~200

#### 1.3 Inbox Service (`inbox_service.py`) - Priority: HIGH

Create `test/services/test_inbox_service.py`:

```python
# Target Test Cases:
# - test_get_log_tail_success
# - test_get_log_tail_file_not_found
# - test_has_idle_pattern_true
# - test_has_idle_pattern_false
# - test_check_and_send_pending_messages_no_messages
# - test_check_and_send_pending_messages_terminal_not_ready
# - test_check_and_send_pending_messages_success
# - test_check_and_send_pending_messages_send_fails
# - test_log_file_handler_on_modified
# - test_log_file_handler_non_log_file
```

**Estimated Tests:** 10  
**Estimated LOC:** ~250

#### 1.4 Cleanup Service (`cleanup_service.py`) - Priority: MEDIUM

Create `test/services/test_cleanup_service.py`:

```python
# Target Test Cases:
# - test_cleanup_old_terminals
# - test_cleanup_old_inbox_messages
# - test_cleanup_old_terminal_logs
# - test_cleanup_old_server_logs
# - test_cleanup_no_old_data
# - test_cleanup_error_handling
```

**Estimated Tests:** 6  
**Estimated LOC:** ~150

#### 1.5 Terminal Service Enhancement (`terminal_service.py`) - Priority: HIGH

Expand `test/services/test_terminal_service.py`:

```python
# Additional Test Cases:
# - test_create_terminal_new_session
# - test_create_terminal_existing_session
# - test_create_terminal_session_already_exists
# - test_create_terminal_session_not_found
# - test_create_terminal_with_working_directory
# - test_get_terminal_success
# - test_get_terminal_not_found
# - test_get_working_directory_success
# - test_get_working_directory_terminal_not_found
# - test_send_input_success
# - test_send_input_terminal_not_found
# - test_get_output_full_mode
# - test_get_output_last_mode
# - test_delete_terminal_success
# - test_delete_terminal_stops_pipe_pane
```

**Estimated Additional Tests:** 15  
**Estimated Additional LOC:** ~350

### Phase 2: Client Layer (Weeks 3-4)

**Goal:** Ensure infrastructure layer reliability

#### 2.1 Database Client Enhancement (`database.py`) - Priority: HIGH

Create `test/clients/test_database.py`:

```python
# Target Test Cases:
# Terminal Operations:
# - test_create_terminal
# - test_get_terminal_metadata_success
# - test_get_terminal_metadata_not_found
# - test_list_terminals_by_session
# - test_update_last_active
# - test_delete_terminal
# - test_delete_terminals_by_session

# Inbox Operations:
# - test_create_inbox_message
# - test_get_pending_messages
# - test_get_inbox_messages_with_status_filter
# - test_get_inbox_messages_with_limit
# - test_update_message_status

# Flow Operations:
# - test_create_flow
# - test_get_flow_success
# - test_get_flow_not_found
# - test_list_flows
# - test_update_flow_run_times
# - test_update_flow_enabled
# - test_delete_flow
# - test_get_flows_to_run
```

**Estimated Tests:** 20  
**Estimated LOC:** ~500

#### 2.2 Tmux Client Enhancement (`tmux.py`) - Priority: HIGH

Create `test/clients/test_tmux.py`:

```python
# Target Test Cases:
# - test_resolve_and_validate_working_directory_default
# - test_resolve_and_validate_working_directory_custom
# - test_resolve_and_validate_working_directory_invalid
# - test_create_session_success
# - test_create_session_with_working_directory
# - test_create_window_success
# - test_create_window_session_not_found
# - test_send_keys_success
# - test_send_keys_chunking
# - test_get_history_success
# - test_get_history_with_tail_lines
# - test_list_sessions
# - test_get_session_windows
# - test_kill_session
# - test_session_exists
# - test_get_pane_working_directory
# - test_pipe_pane_success
# - test_stop_pipe_pane_success
```

**Estimated Tests:** 18  
**Estimated LOC:** ~450

### Phase 3: MCP Server & API (Weeks 5-6)

**Goal:** Complete coverage for external interfaces

#### 3.1 MCP Server (`mcp_server/server.py`) - Priority: HIGH

Create `test/mcp_server/test_server.py`:

```python
# Target Test Cases:
# - test_create_terminal_new_session
# - test_create_terminal_existing_session
# - test_create_terminal_inherits_working_directory
# - test_send_direct_input_success
# - test_send_to_inbox_success
# - test_send_to_inbox_no_terminal_id
# - test_handoff_success
# - test_handoff_idle_timeout
# - test_handoff_completion_timeout
# - test_handoff_with_working_directory
# - test_assign_success
# - test_assign_with_working_directory
# - test_assign_failure
# - test_send_message_success
# - test_send_message_failure
```

**Estimated Tests:** 15  
**Estimated LOC:** ~400

#### 3.2 API Main Enhancement (`api/main.py`) - Priority: HIGH

Expand `test/api/test_api_main.py`:

```python
# Target Test Cases:
# Session Endpoints:
# - test_create_session_success
# - test_create_session_invalid_provider
# - test_list_sessions_success
# - test_get_session_success
# - test_get_session_not_found
# - test_delete_session_success
# - test_delete_session_not_found

# Terminal Endpoints:
# - test_create_terminal_in_session_success
# - test_list_terminals_in_session
# - test_get_terminal_working_directory
# - test_exit_terminal_success
# - test_delete_terminal_success

# Lifespan & Background:
# - test_health_check
# - test_lifespan_startup
# - test_flow_daemon_execution
```

**Estimated Tests:** 15  
**Estimated LOC:** ~400

### Phase 4: CLI Commands & Models (Weeks 7-8)

**Goal:** Complete full test coverage

#### 4.1 CLI Commands - Priority: MEDIUM

Create test files for each command:

```
test/cli/commands/
├── test_flow.py      # ~8 tests
├── test_init.py      # ~5 tests
├── test_install.py   # ~6 tests
└── test_shutdown.py  # ~4 tests
```

**Estimated Tests:** 23  
**Estimated LOC:** ~500

#### 4.2 Utils - Priority: LOW

Create `test/utils/`:

```
test/utils/
├── test_agent_profiles.py  # ~4 tests
├── test_template.py        # ~5 tests
├── test_logging.py         # ~3 tests
└── test_terminal.py        # ~6 tests (expand existing coverage)
```

**Estimated Tests:** 18  
**Estimated LOC:** ~350

#### 4.3 Models Validation - Priority: LOW

Create `test/models/test_models.py`:

```python
# Target Test Cases:
# - test_terminal_model_validation
# - test_terminal_status_enum
# - test_provider_type_enum
# - test_flow_model_validation
# - test_inbox_message_model
# - test_agent_profile_model
# - test_session_model
```

**Estimated Tests:** 7  
**Estimated LOC:** ~150

#### 4.4 Claude Code Provider - Priority: LOW

Create `test/providers/test_claude_code_unit.py`:

```python
# Target Test Cases:
# - test_initialization_success
# - test_get_status_idle
# - test_get_status_completed
# - test_get_status_processing
# - test_get_status_error
# - test_extract_last_message
# - test_exit_cli
# - test_cleanup
```

**Estimated Tests:** 8  
**Estimated LOC:** ~200

---

## Implementation Recommendations

### Test Quality Guidelines

#### 1. Test Naming Convention

```python
def test_<function_name>_<scenario>():
    """Test <function> when <condition>."""
```

Example:
```python
def test_create_terminal_with_invalid_provider():
    """Test create_terminal when provider type is not supported."""
```

#### 2. Test Structure (AAA Pattern)

```python
def test_example():
    """Test description."""
    # Arrange
    mock_dependency.return_value = expected_data
    
    # Act
    result = function_under_test(param)
    
    # Assert
    assert result == expected_value
    mock_dependency.assert_called_once_with(expected_args)
```

#### 3. Fixture Usage

```python
@pytest.fixture
def mock_tmux_client():
    """Create mock tmux client."""
    with patch("cli_agent_orchestrator.clients.tmux.tmux_client") as mock:
        yield mock

@pytest.fixture
def sample_terminal():
    """Create sample terminal for testing."""
    return Terminal(
        id="test1234",
        name="test-window",
        provider=ProviderType.Q_CLI,
        session_name="cao-test-session",
        agent_profile="developer",
        status=TerminalStatus.IDLE,
        last_active=datetime.now(),
    )
```

#### 4. Async Test Pattern

```python
import pytest

@pytest.mark.asyncio
async def test_async_function():
    """Test async function."""
    result = await async_function_under_test()
    assert result == expected
```

### Coverage Targets

| Phase | Target Coverage | Deadline |
|-------|-----------------|----------|
| Phase 1 | 50% overall | Week 2 |
| Phase 2 | 65% overall | Week 4 |
| Phase 3 | 80% overall | Week 6 |
| Phase 4 | 90% overall | Week 8 |

### Test Execution Configuration

Update `pyproject.toml`:

```toml
[tool.pytest.ini_options]
markers = [
    "asyncio: marks tests that use asyncio",
    "integration: marks integration tests",
    "e2e: marks end-to-end tests",
    "slow: marks tests as slow (deselect with '-m \"not slow\"')",
    "unit: marks unit tests"
]
asyncio_mode = "strict"
testpaths = ["test"]
python_files = "test_*.py"
python_classes = "Test*"
python_functions = "test_*"
addopts = "--cov=src --cov-report=term-missing --cov-fail-under=80"
```

### CI/CD Integration

Recommended GitHub Actions workflow additions:

```yaml
# .github/workflows/test.yml
- name: Run Unit Tests with Coverage
  run: |
    uv run pytest test/ -m "not integration" \
      --cov=src \
      --cov-report=xml \
      --cov-report=term-missing \
      --cov-fail-under=80
      
- name: Upload Coverage to Codecov
  uses: codecov/codecov-action@v3
  with:
    files: ./coverage.xml
```

---

## Quick Wins

These tests can be implemented quickly for immediate coverage improvement:

### 1. Service Layer Quick Tests (~30 minutes each)

| Service | Quick Win Test | Impact |
|---------|---------------|--------|
| `session_service.py` | `test_list_sessions_empty` | +5% module coverage |
| `cleanup_service.py` | `test_cleanup_old_data_basic` | +10% module coverage |
| `flow_service.py` | `test_add_flow_success` | +5% module coverage |

### 2. Model Validation Tests (~15 minutes each)

```python
# Quick validation tests
def test_terminal_status_enum_values():
    assert TerminalStatus.IDLE.value == "idle"
    assert TerminalStatus.PROCESSING.value == "processing"
    assert TerminalStatus.COMPLETED.value == "completed"

def test_provider_type_enum_values():
    assert ProviderType.Q_CLI.value == "q_cli"
    assert ProviderType.KIRO_CLI.value == "kiro_cli"
    assert ProviderType.CODEX.value == "codex"
```

### 3. Utility Function Tests (~20 minutes each)

```python
# test/utils/test_terminal.py additions
def test_generate_session_name_format():
    name = generate_session_name()
    assert name.startswith("cao-")
    assert len(name) == 12  # "cao-" + 8 hex chars

def test_generate_terminal_id_format():
    tid = generate_terminal_id()
    assert len(tid) == 8
    assert all(c in "0123456789abcdef" for c in tid)
```

### 4. Error Handling Tests (~10 minutes each)

```python
# Quick error path tests
def test_get_terminal_not_found():
    with pytest.raises(ValueError, match="not found"):
        terminal_service.get_terminal("nonexistent")

def test_delete_session_not_found():
    with pytest.raises(ValueError, match="not found"):
        session_service.delete_session("nonexistent-session")
```

---

## Summary

### Estimated Total Effort

| Category | New Tests | Estimated LOC | Time (Hours) |
|----------|-----------|---------------|--------------|
| Services | 55 | ~1,350 | 20-25 |
| Clients | 38 | ~950 | 15-18 |
| MCP Server | 15 | ~400 | 8-10 |
| API | 15 | ~400 | 8-10 |
| CLI Commands | 23 | ~500 | 10-12 |
| Utils | 18 | ~350 | 6-8 |
| Models | 7 | ~150 | 3-4 |
| Providers | 8 | ~200 | 4-5 |
| **Total** | **179** | **~4,300** | **74-92** |

### Priority Order

1. **Immediate** (Week 1-2): Flow Service, Terminal Service, Session Service
2. **High** (Week 3-4): Database Client, Tmux Client, Inbox Service
3. **Medium** (Week 5-6): MCP Server, API Main, Cleanup Service
4. **Low** (Week 7-8): CLI Commands, Utils, Models, Claude Code Provider

### Success Metrics

- [ ] Overall test coverage ≥ 80%
- [ ] All critical paths have tests
- [ ] CI pipeline enforces coverage threshold
- [ ] No untested error handling paths
- [ ] All async functions have async tests
- [ ] Integration tests for provider interactions

---

*This plan should be reviewed and updated quarterly to reflect changes in the codebase.*
