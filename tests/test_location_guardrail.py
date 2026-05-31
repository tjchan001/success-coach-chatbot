"""Tests for strict location guardrail behavior."""

from __future__ import annotations

from api import chat


def test_no_inferred_delivery_mode() -> None:
    """Explicit campus locations must not trigger inferred delivery-mode wording."""
    context_text: str = "Campus Location: CVC, RLC\nCourse: Audio Engineering"

    response: str = chat.build_location_guardrail_message(context_text)

    assert "online" not in response.lower()


def test_correct_campus_extraction() -> None:
    """Explicit campus tokens must be reported exactly as written."""
    context_text: str = "Campus Location: CVC, RLC"

    response: str = chat.build_location_guardrail_message(context_text)

    assert "CVC" in response
    assert "RLC" in response


def test_missing_location_handling() -> None:
    """Missing location data must produce the explicit unavailable message."""
    context_text: str = "Course: Audio Engineering Fundamentals"

    response: str = chat.build_location_guardrail_message(context_text)

    assert "location information is not available" in response.lower()


def test_anti_inference_enforcement() -> None:
    """Location-only output must not fabricate delivery-mode language."""
    context_text: str = "CAMPUSES: CVC, RLC"

    response: str = chat.build_location_guardrail_message(context_text)

    assert "available online" not in response.lower()
    assert "usually offered" not in response.lower()
    assert "typically held" not in response.lower()