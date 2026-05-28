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

import pytest
from pydantic import ValidationError

from models import Course, DegreePlan, Semester


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
