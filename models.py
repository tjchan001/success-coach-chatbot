"""Pydantic v2 data models for the Dallas College course catalog.

Architectural Intent:
    These models are the single source of truth for all catalog data flowing
    through the pipeline — from the scraper, through the API, to the chat
    widget.  Making them immutable (``frozen=True``) prevents accidental
    mutation across async boundaries and makes them safe to cache.

Security Rationale:
    All fields are strictly typed and validated by Pydantic at construction
    time.  Untrusted scraper output never reaches application logic without
    first being coerced through these models, eliminating injection vectors
    at the data-ingestion boundary.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Course(BaseModel):
    """A single college course within a degree plan.

    Args:
        code: Catalog course code (e.g. ``"ENGL 1301"``).
        title: Human-readable course title (e.g. ``"Composition I"``).
        credits: Credit hours expressed as a string to preserve catalog
            formatting such as ``"3"`` or ``"1-3"`` for variable-credit
            courses.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(..., description="Catalog course code, e.g. 'ENGL 1301'.")
    title: str = Field(..., description="Human-readable course title.")
    credits: str = Field(
        ...,
        description=(
            "Credit hours as a string to support variable-credit notation, "
            "e.g. '3' or '1-3'."
        ),
    )


class Semester(BaseModel):
    """An ordered semester block within a degree plan.

    Args:
        name: Semester label as printed in the catalog
            (e.g. ``"Fall Semester 1"``).
        courses: Ordered list of courses offered in this semester.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(..., description="Semester label, e.g. 'Fall Semester 1'.")
    courses: list[Course] = Field(
        default_factory=list,
        description="Ordered list of courses offered in this semester.",
    )


class DegreePlan(BaseModel):
    """A complete degree plan for a Dallas College academic program.

    Args:
        title: Full degree title (e.g. ``"Associate of Arts"``).
        degree_code: Catalog degree code when available
            (e.g. ``"AA.PSYC"``).  May be ``None`` if not published.
        total_hours: Total credit hours required to complete the degree.
        semesters: Ordered list of semester blocks comprising the plan.
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(..., description="Full degree title, e.g. 'Associate of Arts'.")
    degree_code: str | None = Field(
        None,
        description="Catalog degree code, e.g. 'AA.PSYC'. None if not published.",
    )
    total_hours: int = Field(
        ...,
        ge=1,
        description="Total credit hours required to complete the degree.",
    )
    semesters: list[Semester] = Field(
        default_factory=list,
        description="Ordered list of semester blocks comprising the degree plan.",
    )
