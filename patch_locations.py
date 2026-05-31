"""Patch known demo programs with campus location arrays.

This utility updates empty campus arrays in data/catalog_with_locations.json for
specific demonstration programs used by the chatbot.
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parent
CATALOG_PATH: Path = PROJECT_ROOT / "data" / "catalog_with_locations.json"


def _normalize(value: object) -> str:
    """Normalize text for resilient substring matching."""
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace("-", " ").replace("_", " ")


def _is_empty_campuses(value: object) -> bool:
    """Return True when campuses should be treated as empty."""
    if value is None:
        return True
    if not isinstance(value, list):
        return False
    return len(value) == 0


def patch_catalog_locations(payload: dict[str, object]) -> int:
    """Patch empty campuses arrays for known demonstration target programs."""
    programs_obj: object = payload.get("programs")
    if not isinstance(programs_obj, list):
        raise RuntimeError("catalog_with_locations.json must contain a 'programs' list.")

    updates: int = 0

    for program in programs_obj:
        if not isinstance(program, dict):
            continue

        program_id_text: str = _normalize(program.get("program_id", ""))
        title_text: str = _normalize(program.get("title", ""))
        combined: str = f"{program_id_text} {title_text}".strip()

        if not _is_empty_campuses(program.get("campuses")):
            continue

        # Nursing
        if "associate degree nursing" in combined or "associate_degree_nursing_a_a_s" in str(
            program.get("program_id", "")
        ).lower():
            program["campuses"] = ["Brookhaven", "El Centro", "Mountain View", "North Lake"]
            updates += 1
            continue

        # Veterinary Technology
        if "veterinary technology" in combined or "veterinary_technology_a_a_s" in str(
            program.get("program_id", "")
        ).lower():
            program["campuses"] = ["Cedar Valley"]
            updates += 1
            continue

        # Criminal Justice / Peace Officer
        if (
            "basic criminal justice studies" in combined
            or "peace officer" in combined
            or "basic_criminal_justice_studies" in str(program.get("program_id", "")).lower()
            or "peace_officer" in str(program.get("program_id", "")).lower()
        ):
            program["campuses"] = ["Eastfield"]
            updates += 1
            continue

        # Web Design / Web Development
        if "web development" in combined or "web design" in combined or "web_development" in str(
            program.get("program_id", "")
        ).lower():
            program["campuses"] = ["Brookhaven", "El Centro", "North Lake", "Richland"]
            updates += 1
            continue

    return updates


def main() -> int:
    """Load, patch, and save catalog_with_locations.json in place."""
    payload_obj: object = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload_obj, dict):
        raise RuntimeError("catalog_with_locations.json root must be a JSON object.")

    updated_count: int = patch_catalog_locations(payload_obj)

    CATALOG_PATH.write_text(
        json.dumps(payload_obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Patched programs: {updated_count}")
    print(f"Saved: {CATALOG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
