"""tests/test_detector.py — tests for detector.py"""
import time
import pytest
from unittest.mock import patch

from claudebud.detector import Detector, DebouncedDetector, EventType

PROMPT_PATTERNS = [r"\(Y/n\)", r"Allow", r"Do you want to", r"Press Enter"]
COMPLETE_PATTERNS = [r"✓ Completed", r"Task complete", r"Done\.", r"All done"]


@pytest.fixture()
def detector():
    return Detector(PROMPT_PATTERNS, COMPLETE_PATTERNS)


@pytest.fixture()
def debounced(detector):
    return DebouncedDetector(detector, debounce_seconds=2.0)


# --- Detector ---

def test_prompt_yn(detector):
    assert detector.detect("Do you want to continue? (Y/n)") == EventType.PROMPT


def test_prompt_allow(detector):
    assert detector.detect("Allow this tool to run?") == EventType.PROMPT


def test_prompt_press_enter(detector):
    assert detector.detect("Press Enter to continue") == EventType.PROMPT


def test_complete_checkmark(detector):
    assert detector.detect("✓ Completed successfully") == EventType.COMPLETE


def test_complete_task_complete(detector):
    assert detector.detect("Task complete. 3 files changed.") == EventType.COMPLETE


def test_complete_done(detector):
    assert detector.detect("Done. All tests passed.") == EventType.COMPLETE


def test_normal_output(detector):
    assert detector.detect("Writing file src/main.py...") == EventType.NORMAL


def test_normal_empty(detector):
    assert detector.detect("") == EventType.NORMAL


def test_prompt_takes_priority_over_complete(detector):
    # A line that matches both — PROMPT should win (checked first)
    assert detector.detect("Allow — Done.") == EventType.PROMPT


# --- DebouncedDetector ---

def test_debounced_first_event_fires(debounced):
    result = debounced.detect("Allow this tool?")
    assert result == EventType.PROMPT


def test_debounced_suppresses_repeat_within_window(debounced):
    debounced.detect("Allow this tool?")
    result = debounced.detect("Allow again?")
    assert result is None


def test_debounced_allows_after_window(debounced):
    debounced.detect("Allow this tool?")
    # Simulate time passing beyond debounce window
    debounced._last_event_time -= 3.0
    result = debounced.detect("Allow again?")
    assert result == EventType.PROMPT


def test_debounced_different_event_types_not_suppressed(debounced):
    debounced.detect("Allow this tool?")           # PROMPT
    result = debounced.detect("✓ Completed")       # COMPLETE — different type, should fire
    assert result == EventType.COMPLETE


def test_debounced_normal_never_suppressed(debounced):
    for _ in range(5):
        result = debounced.detect("writing file...")
        assert result == EventType.NORMAL
