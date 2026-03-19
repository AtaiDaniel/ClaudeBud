"""
detector.py — classify terminal output chunks as PROMPT, COMPLETE, or NORMAL.
"""
import re
import time
from enum import Enum
from typing import List, Optional


class EventType(str, Enum):
    PROMPT = "prompt"
    COMPLETE = "complete"
    NORMAL = "normal"


class Detector:
    """Stateless pattern matcher."""

    def __init__(self, prompt_patterns: List[str], completion_patterns: List[str]):
        self._prompt_re = [re.compile(p) for p in prompt_patterns]
        self._complete_re = [re.compile(p) for p in completion_patterns]

    def detect(self, text: str) -> EventType:
        for pattern in self._prompt_re:
            if pattern.search(text):
                return EventType.PROMPT
        for pattern in self._complete_re:
            if pattern.search(text):
                return EventType.COMPLETE
        return EventType.NORMAL


class DebouncedDetector:
    """Wraps Detector and suppresses repeated identical events within a time window."""

    def __init__(self, detector: Detector, debounce_seconds: float = 5.0):
        self._detector = detector
        self._debounce_seconds = debounce_seconds
        self._last_event: Optional[EventType] = None
        self._last_event_time: float = 0.0

    def detect(self, text: str) -> Optional[EventType]:
        """
        Returns an EventType if it should fire, or None if suppressed by debounce.
        NORMAL events are never suppressed (they don't trigger notifications).
        """
        event = self._detector.detect(text)

        if event == EventType.NORMAL:
            return event

        now = time.monotonic()
        if (
            event == self._last_event
            and (now - self._last_event_time) < self._debounce_seconds
        ):
            return None  # suppressed

        self._last_event = event
        self._last_event_time = now
        return event
