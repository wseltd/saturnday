"""Tests for saturnday.ticket_splitter."""

from saturnday._types import TicketScope, TicketSpec
from saturnday.ticket_splitter import (
    _parse_split_response,
    should_split,
)


class TestShouldSplit:
    def test_short_goal_no_split(self) -> None:
        ticket = TicketSpec(ticket_id="T001", goal="Create scaffold.")
        assert should_split(ticket) is False

    def test_long_goal_with_multiple_files_triggers(self) -> None:
        goal = (
            "Create src/support_copilot/config.py and tests/test_config.py "
            "and src/support_copilot/models.py with exact signatures. "
            + "x " * 100  # pad to exceed min length
        )
        ticket = TicketSpec(ticket_id="T003", goal=goal)
        assert should_split(ticket) is True

    def test_many_must_clauses_triggers(self) -> None:
        goal = (
            "The module MUST handle X. It MUST validate Y. "
            "It MUST return Z. Tests MUST cover edge cases. "
            "It MUST support W. " + "padding " * 50
        )
        ticket = TicketSpec(ticket_id="T005", goal=goal)
        assert should_split(ticket) is True

    def test_large_scope_triggers(self) -> None:
        scope = TicketScope(max_files_changed=5)
        goal = "Create all the route files for the API with full endpoint handlers and tests. " + "x " * 150
        ticket = TicketSpec(ticket_id="T010", goal=goal, scope=scope)
        assert should_split(ticket) is True

    def test_moderate_goal_no_split(self) -> None:
        goal = "Create src/config.py with Settings dataclass and get_settings function. " + "x " * 60
        scope = TicketScope(max_total_diff_lines=30, max_files_changed=2)
        ticket = TicketSpec(ticket_id="T002", goal=goal, scope=scope)
        assert should_split(ticket) is False


class TestParseSplitResponse:
    def test_no_split(self) -> None:
        original = TicketSpec(ticket_id="T005", goal="simple")
        result = _parse_split_response('{"split": false}', original)
        assert result is None

    def test_valid_split(self) -> None:
        original = TicketSpec(ticket_id="T005", goal="complex thing")
        response = '''{
            "split": true,
            "sub_tickets": [
                {"id": "a", "goal": "Create knowledge.py with build_knowledge_store", "files": ["src/knowledge.py"]},
                {"id": "b", "goal": "Create test_knowledge.py", "files": ["tests/test_knowledge.py"], "depends_on": ["a"]}
            ]
        }'''
        result = _parse_split_response(response, original)
        assert result is not None
        assert len(result) == 2
        assert result[0].ticket_id == "T005.a"
        assert result[1].ticket_id == "T005.b"
        assert "T005.a" in result[1].dependencies

    def test_invalid_json_returns_none(self) -> None:
        original = TicketSpec(ticket_id="T005", goal="test")
        result = _parse_split_response("not json at all", original)
        assert result is None

    def test_single_sub_ticket_returns_none(self) -> None:
        original = TicketSpec(ticket_id="T005", goal="test")
        response = '{"split": true, "sub_tickets": [{"id": "a", "goal": "only one"}]}'
        result = _parse_split_response(response, original)
        assert result is None

    def test_json_embedded_in_text(self) -> None:
        original = TicketSpec(ticket_id="T005", goal="test")
        response = 'Here is my analysis:\n{"split": false}\nDone.'
        result = _parse_split_response(response, original)
        assert result is None
