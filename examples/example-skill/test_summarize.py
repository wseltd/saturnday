"""Tests for the summarize skill."""

from summarize_skill import summarize


def test_basic_summarize():
    text = "First point. Second point. Third point."
    result = summarize(text, max_bullets=2)
    assert len(result) <= 2
    assert result[0] == "First point"


def test_short_text():
    result = summarize("Hi", min_length=10)
    assert result == ["Hi"]


def test_empty_returns_empty():
    result = summarize("")
    assert result == [""]
