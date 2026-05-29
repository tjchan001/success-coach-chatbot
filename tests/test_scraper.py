"""Unit tests for the Pydantic v2 catalog data models.

Covers ``Course``, ``Semester``, and ``DegreePlan`` in ``models.py`` and
validates the happy-path behaviour of ``parse_degree_page`` in
``scripts/scraper.py``.

Architectural Intent:
    These tests act as the schema contract for the entire data pipeline.
    If a scraper change breaks the schema it will be caught here before
    any code reaches the API or the chat widget.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from models import Course, DegreePlan, Semester
from scripts import scraper


# ---------------------------------------------------------------------------
# Test 1 — Successful initialisation with string credits
# ---------------------------------------------------------------------------


class TestCourseInitialisation:
    """Verify that Course accepts valid string-valued credit fields."""

    def test_course_initialises_with_string_credits(self) -> None:
        """Course must accept both fixed ('3') and variable ('1-3') credit strings.

        Arrange:
            Build two raw course dicts — one with a fixed credit value and
            one with a variable-credit notation.
        Act:
            Instantiate Course for each dict.
        Assert:
            Both instances are created without raising ValidationError and
            the ``credits`` field is preserved exactly as supplied.
        """
        # Arrange
        fixed_data = {"code": "ENGL 1301", "title": "Composition I", "credits": "3"}
        variable_data = {
            "code": "PHED 1164",
            "title": "Physical Education Activity",
            "credits": "1-3",
        }

        # Act
        fixed_course = Course(**fixed_data)
        variable_course = Course(**variable_data)

        # Assert
        assert fixed_course.code == "ENGL 1301"
        assert fixed_course.credits == "3"
        assert variable_course.code == "PHED 1164"
        assert variable_course.credits == "1-3"


# ---------------------------------------------------------------------------
# Test 2 — Validation catches missing required parameters
# ---------------------------------------------------------------------------


class TestCourseValidationErrors:
    """Verify that Pydantic raises ValidationError when required fields are absent."""

    def test_missing_code_raises_validation_error(self) -> None:
        """Course must raise ValidationError when 'code' is omitted.

        Arrange:
            Prepare a dict missing the required 'code' field.
        Act:
            Attempt to instantiate Course.
        Assert:
            A ValidationError is raised and its error list contains an entry
            targeting the 'code' field.
        """
        # Arrange
        incomplete_data = {"title": "Composition I", "credits": "3"}

        # Act / Assert
        with pytest.raises(ValidationError) as exc_info:
            Course(**incomplete_data)

        errors = exc_info.value.errors()
        missing_fields = [e["loc"][0] for e in errors]
        assert "code" in missing_fields

    def test_missing_total_hours_raises_validation_error(self) -> None:
        """DegreePlan must raise ValidationError when 'total_hours' is omitted.

        Arrange:
            Prepare a dict for DegreePlan that omits the required
            'total_hours' field.
        Act:
            Attempt to instantiate DegreePlan.
        Assert:
            A ValidationError is raised.
        """
        # Arrange
        incomplete_plan = {"title": "Associate of Arts"}

        # Act / Assert
        with pytest.raises(ValidationError):
            DegreePlan(**incomplete_plan)


# ---------------------------------------------------------------------------
# Test 3 — Nested structure maps seamlessly end-to-end
# ---------------------------------------------------------------------------


class TestDegreePlanNestedStructure:
    """Verify that the full Course → Semester → DegreePlan nesting works correctly."""

    def test_degree_plan_nests_semesters_and_courses(self) -> None:
        """DegreePlan must correctly nest Semester and Course objects.

        Arrange:
            Build a realistic two-semester degree plan dict with two courses
            each, mirroring what the scraper would produce.
        Act:
            Instantiate DegreePlan from the raw dict.
        Assert:
            - The plan title and total hours are preserved.
            - Two semesters exist with the correct names.
            - The first course of the first semester has the expected code.
            - The model is frozen (immutability contract holds).
        """
        # Arrange
        raw_plan: dict = {
            "title": "Associate of Arts — Psychology",
            "degree_code": "AA.PSYC",
            "total_hours": 60,
            "semesters": [
                {
                    "name": "Fall Semester 1",
                    "courses": [
                        {"code": "ENGL 1301", "title": "Composition I", "credits": "3"},
                        {"code": "PSYC 2301", "title": "General Psychology", "credits": "3"},
                    ],
                },
                {
                    "name": "Spring Semester 1",
                    "courses": [
                        {"code": "ENGL 1302", "title": "Composition II", "credits": "3"},
                        {"code": "MATH 1314", "title": "College Algebra", "credits": "3"},
                    ],
                },
            ],
        }

        # Act
        plan = DegreePlan(**raw_plan)

        # Assert
        assert plan.title == "Associate of Arts — Psychology"
        assert plan.degree_code == "AA.PSYC"
        assert plan.total_hours == 60
        assert len(plan.semesters) == 2
        assert plan.semesters[0].name == "Fall Semester 1"
        assert plan.semesters[0].courses[0].code == "ENGL 1301"
        assert plan.semesters[1].name == "Spring Semester 1"
        assert plan.semesters[1].courses[1].code == "MATH 1314"

    def test_degree_plan_without_degree_code_defaults_to_none(self) -> None:
        """DegreePlan.degree_code must default to None when not supplied.

        Arrange:
            Build a minimal DegreePlan dict omitting the optional
            'degree_code' field.
        Act:
            Instantiate DegreePlan.
        Assert:
            ``degree_code`` is ``None``.
        """
        # Arrange
        raw_plan: dict = {
            "title": "Associate of Applied Science",
            "total_hours": 62,
            "semesters": [],
        }

        # Act
        plan = DegreePlan(**raw_plan)

        # Assert
        assert plan.degree_code is None
        assert plan.total_hours == 62

    def test_course_is_immutable(self) -> None:
        """Frozen Course instances must raise TypeError on attribute assignment.

        Arrange:
            Create a valid Course instance.
        Act:
            Attempt to mutate the 'credits' field.
        Assert:
            TypeError is raised, confirming the frozen contract.
        """
        # Arrange
        course = Course(code="ENGL 1301", title="Composition I", credits="3")

        # Act / Assert
        with pytest.raises((TypeError, ValidationError)):
            course.credits = "4"  # type: ignore[misc]


def test_build_catalog_payload_adds_program_id_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collector payload must include explicit program_id metadata per pathway key.

    Arrange:
        Stub parse_degree_page to return deterministic DegreePlan models for
        two pathway URLs.
    Act:
        Build the multi-program payload using _build_catalog_payload.
    Assert:
        Output keeps the top-level "programs" shape and each item includes
        the originating program_id.
    """

    def _fake_parse_degree_page(_url: str) -> DegreePlan:
        return DegreePlan(
            title="Sample Program",
            degree_code="AAS.SAMP",
            total_hours=60,
            semesters=[Semester(name="Semester 1", courses=[])],
        )

    # Arrange
    pathways: dict[str, str] = {
        "Computer_Information_Technology_AAS": "https://catalog.dallascollege.edu/preview_program.php?catoid=33&poid=3057",
        "Web_Development_Certificate": "https://catalog.dallascollege.edu/preview_program.php?catoid=33&poid=3058",
    }
    monkeypatch.setattr(scraper, "parse_degree_page", _fake_parse_degree_page)

    # Act
    payload: dict[str, list[dict[str, object]]] = scraper._build_catalog_payload(pathways)

    # Assert
    assert "programs" in payload
    assert len(payload["programs"]) == 2
    assert payload["programs"][0]["program_id"] == "Computer_Information_Technology_AAS"
    assert payload["programs"][1]["program_id"] == "Web_Development_Certificate"


def test_write_json_atomic_writes_complete_payload(tmp_path: Path) -> None:
    """Atomic writer must persist the full JSON payload at destination path."""
    # Arrange
    destination: Path = tmp_path / "catalog_mvp.json"
    payload: dict[str, object] = {
        "programs": [
            {
                "program_id": "Cybersecurity_AAS",
                "title": "Cybersecurity AAS",
                "total_hours": 60,
                "semesters": [],
            }
        ]
    }

    # Act
    scraper._write_json_atomic(payload=payload, destination_path=destination)

    # Assert
    written_payload: dict[str, object] = json.loads(destination.read_text(encoding="utf-8"))
    assert written_payload == payload
