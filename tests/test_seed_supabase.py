"""Unit tests for Supabase HTTPS seeding helpers."""

from __future__ import annotations

from seed_supabase import build_program_pathways, chunk_rows, extract_unique_courses


def test_extract_unique_courses_deduplicates_across_programs() -> None:
    """Shared courses across majors must be inserted only once."""
    # Arrange
    payload: dict[str, object] = {
        "programs": [
            {
                "program_id": "Program_A",
                "title": "Program A",
                "source_url": "https://example.com/a",
                "semesters": [
                    {
                        "name": "Semester 1",
                        "courses": [
                            {"code": "ENGL 1301", "title": "Composition I", "credits": "3"},
                            {"code": "MATH 1314", "title": "College Algebra", "credits": "3"},
                        ],
                    }
                ],
            },
            {
                "program_id": "Program_B",
                "title": "Program B",
                "source_url": "https://example.com/b",
                "semesters": [
                    {
                        "name": "Semester 1",
                        "courses": [
                            {"code": "ENGL 1301", "title": "Composition I", "credits": "3"},
                            {"code": "BIOL 2401", "title": "Anatomy and Physiology I", "credits": "4"},
                        ],
                    }
                ],
            },
        ]
    }

    # Act
    courses = extract_unique_courses(payload)

    # Assert
    assert sorted(courses.keys()) == ["BIOL 2401", "ENGL 1301", "MATH 1314"]
    assert courses["ENGL 1301"]["title"] == "Composition I"
    assert courses["ENGL 1301"]["credits"] == "3"


def test_build_program_pathways_preserves_exact_schema_keys() -> None:
    """Pathway rows must map to production columns program_name/semester_name/content."""
    # Arrange
    payload: dict[str, object] = {
        "programs": [
            {
                "program_id": "Program_A",
                "title": "Program A",
                "degree_code": "PROG.TEST",
                "total_hours": 60,
                "source_url": "https://example.com/a",
                "semesters": [
                    {
                        "name": "Semester 1",
                        "courses": [
                            {"code": "ENGL 1301", "title": "Composition I", "credits": "3"},
                            {"code": "MATH 1314", "title": "College Algebra", "credits": "3"},
                        ],
                    }
                ],
            }
        ]
    }

    # Act
    pathway_rows = build_program_pathways(payload)

    # Assert
    assert len(pathway_rows) == 1
    assert set(pathway_rows[0].keys()) == {"program_name", "semester_name", "content"}
    assert pathway_rows[0]["program_name"] == "Program A"
    assert pathway_rows[0]["semester_name"] == "Semester 1"
    assert pathway_rows[0]["content"] == "ENGL 1301, MATH 1314"


def test_build_program_pathways_no_enrichment_strings() -> None:
    """Pathway content must remain code-only and exclude narrative enrichment."""
    payload: dict[str, object] = {
        "programs": [
            {
                "program_id": "Program_A",
                "title": "Program A",
                "campuses": ["CVC"],
                "semesters": [
                    {
                        "name": "Semester 1",
                        "courses": [
                            {"code": "ENGL 1301", "title": "Composition I", "credits": "3"},
                            {"code": "MATH 1314", "title": "College Algebra", "credits": "3"},
                        ],
                    }
                ],
            }
        ]
    }

    rows = build_program_pathways(payload)

    text = rows[0]["content"]

    assert "Program:" not in text
    assert "Semester:" not in text
    assert "Campus" not in text
    assert text == "ENGL 1301, MATH 1314"


def test_chunk_rows_splits_into_200_row_batches() -> None:
    """HTTPS uploads must be chunked to stay under payload limits."""
    # Arrange
    rows: list[dict[str, object]] = [{"course_code": str(index)} for index in range(405)]

    # Act
    batches = chunk_rows(rows)

    # Assert
    assert [len(batch) for batch in batches] == [200, 200, 5]