"""Seed Dallas College catalog data into Supabase over HTTPS.

Architectural Intent:
    This utility moves the local catalog artifact into Supabase using the
    official REST client so the ingest path stays stable even when direct
    PostgreSQL routing is unavailable in the local environment.

Security Rationale:
    - Supabase credentials are read only from environment variables.
    - Uploads use the official client library over HTTPS rather than raw SQL.
    - Writes are chunked to reduce payload size and make repeated runs safer.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from supabase import Client, create_client

PROJECT_ROOT: Path = Path(__file__).resolve().parent
CATALOG_PATH: Path = PROJECT_ROOT / "data" / "catalog_with_locations.json"
BATCH_SIZE: int = 200
DEFAULT_SUPABASE_URL: str = "https://plieuwxjqkcltvpcoavh.supabase.co"
LOCATION_FALLBACK_TEXT: str = "Online / General Catalog"


def load_catalog_payload(catalog_path: Path = CATALOG_PATH) -> dict[str, object]:
    """Load the local catalog payload.

    Args:
        catalog_path: Absolute path to the catalog JSON artifact.

    Returns:
        Parsed JSON payload.

    Raises:
        RuntimeError: If the JSON root is not an object.
    """
    payload: object = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("catalog_mvp.json root must be a JSON object.")
    return payload


def extract_unique_courses(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    """Extract unique course rows keyed by course code.

    Args:
        payload: Parsed catalog payload.

    Returns:
        Mapping of course code to upload-ready course row.
    """
    programs_obj: object = payload.get("programs")
    if not isinstance(programs_obj, list):
        return {}

    unique_courses: dict[str, dict[str, object]] = {}

    for program in programs_obj:
        if not isinstance(program, dict):
            continue

        semesters_obj: object = program.get("semesters")
        if not isinstance(semesters_obj, list):
            continue

        for semester in semesters_obj:
            if not isinstance(semester, dict):
                continue

            courses_obj: object = semester.get("courses")
            if not isinstance(courses_obj, list):
                continue

            for course in courses_obj:
                if not isinstance(course, dict):
                    continue

                course_code: str = str(course.get("code", "")).strip()
                if not course_code or course_code in unique_courses:
                    continue

                unique_courses[course_code] = {
                    "course_code": course_code,
                    "title": str(
                        course.get("name", course.get("title", course_code))
                    ).strip(),
                    "credits": str(course.get("credits", "")).strip(),
                }

    return unique_courses


def _normalize_campuses(program: dict[str, object]) -> list[str]:
    """Normalize campus values into a non-empty string list."""
    campuses_obj: object = program.get("campuses", [])
    if not isinstance(campuses_obj, list):
        return []

    normalized_campuses: list[str] = [
        str(campus).strip()
        for campus in campuses_obj
        if str(campus).strip()
    ]
    return normalized_campuses


def _build_pathway_content(
    program_name: str,
    semester_name: str,
    campuses: list[str],
    course_codes: list[str],
) -> str:
    """Build the embedded pathway text with campus metadata baked in."""
    campus_string: str = ", ".join(campuses) if campuses else LOCATION_FALLBACK_TEXT
    course_string: str = ", ".join(course_codes) if course_codes else "None explicitly stated"
    return (
        f"Program: {program_name}. Semester: {semester_name}. "
        f"Offered at Campuses: {campus_string}. Required Path Requirements: {course_string}"
    )


def build_program_pathways(payload: dict[str, object]) -> list[dict[str, object]]:
    """Build structural semester rows for pathway uploads.

    Args:
        payload: Parsed catalog payload.

    Returns:
        List of upload-ready semester pathway rows.
    """
    programs_obj: object = payload.get("programs")
    if not isinstance(programs_obj, list):
        return []

    program_pathways: list[dict[str, object]] = []

    for program in programs_obj:
        if not isinstance(program, dict):
            continue

        program_name: str = str(program.get("title", "")).strip()
        semesters_obj: object = program.get("semesters")
        if not isinstance(semesters_obj, list):
            continue

        campuses: list[str] = _normalize_campuses(program)

        for semester in semesters_obj:
            if not isinstance(semester, dict):
                continue

            semester_name: str = str(semester.get("name", "")).strip()
            courses_obj: object = semester.get("courses")
            course_codes: list[str] = []
            if isinstance(courses_obj, list):
                for course in courses_obj:
                    if not isinstance(course, dict):
                        continue
                    course_code: str = str(course.get("code", "")).strip()
                    if course_code:
                        course_codes.append(course_code)

            program_pathways.append(
                {
                    "program_name": program_name,
                    "semester_name": semester_name,
                    "content": _build_pathway_content(
                        program_name=program_name,
                        semester_name=semester_name,
                        campuses=campuses,
                        course_codes=course_codes,
                    ),
                }
            )

    return program_pathways


def chunk_rows(
    rows: list[dict[str, object]],
    batch_size: int = BATCH_SIZE,
) -> list[list[dict[str, object]]]:
    """Split upload rows into fixed-size batches.

    Args:
        rows: Upload rows for one table.
        batch_size: Maximum rows per request.

    Returns:
        Batches sized for safe REST uploads.
    """
    return [rows[index : index + batch_size] for index in range(0, len(rows), batch_size)]


def upload_batches(client: Client, table_name: str, rows: list[dict[str, object]]) -> None:
    """Upload one table in fixed-size HTTPS batches.

    Args:
        client: Configured Supabase client.
        table_name: Destination table name.
        rows: Upload rows for the table.
    """
    print(f"Uploading {len(rows)} rows to '{table_name}'...")
    for batch_index, batch in enumerate(chunk_rows(rows), start=1):
        print(
            f"  Batch {batch_index}: sending {len(batch)} rows to '{table_name}' over HTTPS..."
        )
        client.table(table_name).upsert(batch).execute()


def main() -> int:
    """Run the HTTPS Supabase seeding workflow.

    Returns:
        Process exit code.
    """
    print("Initializing Supabase HTTPS Data Ingest...")

    url: str = os.environ.get("SUPABASE_URL", DEFAULT_SUPABASE_URL)
    key: str | None = os.environ.get("SUPABASE_SERVICE_KEY")
    if not key:
        print("CRITICAL ERROR: Please set your SUPABASE_SERVICE_KEY environment variable.")
        return 1

    os.environ.setdefault("SUPABASE_URL", url)
    supabase: Client = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY"))

    payload: dict[str, object] = load_catalog_payload()
    print(f"Catalog programs loaded: {len(payload.get('programs', []))}")

    official_courses: dict[str, dict[str, object]] = extract_unique_courses(payload)
    program_pathways: list[dict[str, object]] = build_program_pathways(payload)

    print(f"Unique courses computed: {len(official_courses)}")
    print(f"Program pathway rows computed: {len(program_pathways)}")

    try:
        upload_batches(supabase, "official_courses", list(official_courses.values()))
        upload_batches(supabase, "program_pathways", program_pathways)
        print("SUCCESS! Database fully seeded over HTTPS.")
        return 0
    except Exception as exc:
        print(f"API Upload Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())