"""Algorithmically populate Dallas College campus hubs for every program.

This utility reads data/catalog_with_locations.json, maps every program to one or
more official campus hubs using deterministic keyword rules, and writes the
updated payload back in place.
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parent
CATALOG_PATH: Path = PROJECT_ROOT / "data" / "catalog_with_locations.json"
ONLINE_FALLBACK: str = "Online / General Catalog"
GENERAL_STUDIES_KEYWORDS: tuple[str, ...] = (
    "general studies",
    "core options",
    "associate of arts",
    "associate of science",
    "university transfer",
    "core curriculum",
)
CAMPUSES: tuple[str, ...] = (
    "Brookhaven",
    "Cedar Valley",
    "Eastfield",
    "El Centro",
    "Mountain View",
    "North Lake",
    "Richland",
)

RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("nursing", "vocational nursing"),
        ("Brookhaven", "El Centro", "Mountain View", "North Lake"),
    ),
    (("veterinary",), ("Cedar Valley",)),
    (
        (
            "dental",
            "diagnostic medical sonography",
            "cardiovascular",
            "radiologic",
            "respiratory",
            "medical laboratory",
            "surgical technology",
        ),
        ("El Centro",),
    ),
    (("emergency medical", "paramedic"), ("Brookhaven", "El Centro")),
    (("automotive", "diesel"), ("Eastfield",)),
    (
        (
            "aviation",
            "electronics",
            "machining",
            "welding",
            "construction",
            "hvac",
            "electrical technology",
        ),
        ("Eastfield", "Mountain View"),
    ),
    (
        (
            "recording technology",
            "commercial music",
            "digital music",
            "video game",
            "visual communications",
        ),
        ("Cedar Valley",),
    ),
    (("fashion", "apparel", "interior design"), ("El Centro",)),
    (("culinary", "pastry", "hospitality", "food service"), ("El Centro",)),
    (
        ("accounting", "business administration", "management", "marketing"),
        ("Brookhaven", "Cedar Valley", "Eastfield", "El Centro", "Mountain View", "North Lake", "Richland"),
    ),
    (
        (
            "computer",
            "software",
            "web development",
            "cybersecurity",
            "networking",
            "information technology",
            "programming",
        ),
        ("Brookhaven", "El Centro", "North Lake", "Richland"),
    ),
)


def _normalize(value: object) -> str:
    """Normalize text for resilient substring matching."""
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace("-", " ").replace("_", " ")


def _normalize_campuses(value: object) -> list[str]:
    """Normalize campus arrays into a stable list of unique strings."""
    if not isinstance(value, list):
        return []

    campuses: list[str] = []
    seen: set[str] = set()
    for campus in value:
        campus_name: str = str(campus).strip()
        if not campus_name or campus_name in seen:
            continue
        seen.add(campus_name)
        campuses.append(campus_name)
    return campuses


def _build_search_blob(program: dict[str, object]) -> str:
    """Build a searchable text blob from available program fields."""
    parts: list[str] = []
    for field_name in (
        "program_id",
        "title",
        "degree_code",
        "source_url",
        "description",
        "summary",
        "department",
        "discipline",
        "school",
    ):
        field_value: object = program.get(field_name)
        if isinstance(field_value, str) and field_value.strip():
            parts.append(_normalize(field_value))

    semesters_obj: object = program.get("semesters")
    if isinstance(semesters_obj, list):
        for semester in semesters_obj:
            if not isinstance(semester, dict):
                continue
            semester_name: object = semester.get("name")
            if isinstance(semester_name, str) and semester_name.strip():
                parts.append(_normalize(semester_name))

            courses_obj: object = semester.get("courses")
            if not isinstance(courses_obj, list):
                continue
            for course in courses_obj:
                if not isinstance(course, dict):
                    continue
                for course_field in ("code", "title", "name", "description"):
                    course_value: object = course.get(course_field)
                    if isinstance(course_value, str) and course_value.strip():
                        parts.append(_normalize(course_value))

    campuses_obj: object = program.get("campuses")
    if isinstance(campuses_obj, list):
        parts.extend(_normalize(campus) for campus in campuses_obj if isinstance(campus, str))

    return " ".join(part for part in parts if part)


def _is_general_studies(program_blob: str) -> bool:
    """Detect programs that should remain location-agnostic."""
    return any(keyword in program_blob for keyword in GENERAL_STUDIES_KEYWORDS)


def _assign_campuses(program: dict[str, object]) -> list[str]:
    """Assign campuses using ordered keyword rules and safe fallbacks."""
    program_blob: str = _build_search_blob(program)
    matched_campuses: list[str] = []
    seen: set[str] = set()

    for keywords, campuses in RULES:
        if not any(keyword in program_blob for keyword in keywords):
            continue
        for campus in campuses:
            if campus in seen:
                continue
            seen.add(campus)
            matched_campuses.append(campus)

    if matched_campuses:
        return matched_campuses

    if _is_general_studies(program_blob):
        return []

    return [ONLINE_FALLBACK]


def _campus_summary() -> dict[str, int]:
    """Create a predictable campus audit summary payload."""
    summary: dict[str, int] = {campus: 0 for campus in CAMPUSES}
    summary[ONLINE_FALLBACK] = 0
    return summary


def patch_catalog_locations(payload: dict[str, object]) -> tuple[int, dict[str, int]]:
    """Patch campuses for every program in the catalog payload."""
    programs_obj: object = payload.get("programs")
    if not isinstance(programs_obj, list):
        raise RuntimeError("catalog_with_locations.json must contain a 'programs' list.")

    updates: int = 0
    summary: dict[str, int] = _campus_summary()

    for program in programs_obj:
        if not isinstance(program, dict):
            continue

        assigned_campuses: list[str] = _assign_campuses(program)
        existing_campuses: list[str] = _normalize_campuses(program.get("campuses"))
        if assigned_campuses != existing_campuses:
            updates += 1

        program["campuses"] = assigned_campuses

        for campus in assigned_campuses:
            if campus in summary:
                summary[campus] += 1
            elif campus == ONLINE_FALLBACK:
                summary[ONLINE_FALLBACK] += 1

    return updates, summary


def main() -> int:
    """Load, patch, and save catalog_with_locations.json in place."""
    payload_obj: object = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload_obj, dict):
        raise RuntimeError("catalog_with_locations.json root must be a JSON object.")

    updated_count, summary = patch_catalog_locations(payload_obj)

    CATALOG_PATH.write_text(
        json.dumps(payload_obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Patched programs: {updated_count}")
    print(f"Saved: {CATALOG_PATH}")
    print("Campus assignment summary:")
    for campus, count in summary.items():
        print(f"  {campus}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
