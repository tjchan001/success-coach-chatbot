"""Enrich Dallas College catalog programs with campus availability.

This script reads the source catalog payload, fetches each program preview page,
extracts campus location availability, and writes a new catalog artifact with a
``campuses`` field for each program.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT: Path = Path(__file__).resolve().parent
INPUT_PATH: Path = PROJECT_ROOT / "data" / "catalog_mvp.json"
OUTPUT_PATH: Path = PROJECT_ROOT / "data" / "catalog_with_locations.json"
CATALOG_URL_TEMPLATE: str = (
    "https://catalog.dallascollege.edu/preview_program.php?catoid=5&poid={poid}"
)
KNOWN_CAMPUSES: tuple[str, ...] = (
    "Brookhaven",
    "Cedar Valley",
    "Eastfield",
    "El Centro",
    "Mountain View",
    "North Lake",
    "Richland",
)


def load_catalog_payload(catalog_path: Path = INPUT_PATH) -> dict[str, object]:
    """Load the source catalog JSON payload."""
    payload: object = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("catalog_mvp.json root must be a JSON object.")
    return payload


def extract_program_poid(program: dict[str, object]) -> str:
    """Return the catalog poid for a program when available."""
    raw_poid: object = program.get("poid", "")
    return str(raw_poid).strip() if raw_poid is not None else ""


def extract_locations_from_text(text: str) -> list[str]:
    """Extract campus names from page text or a location field."""
    normalized_text: str = re.sub(r"\s+", " ", text).strip()
    if not normalized_text:
        return []

    if re.search(r"offered on all campuses", normalized_text, flags=re.IGNORECASE):
        return list(KNOWN_CAMPUSES)

    location_match: re.Match[str] | None = re.search(
        r"Location\(s\)\s*:\s*(.+?)(?:\.|$|\n)",
        normalized_text,
        flags=re.IGNORECASE,
    )
    candidate_text: str = location_match.group(1) if location_match else normalized_text

    campuses: list[str] = []
    for campus in KNOWN_CAMPUSES:
        if re.search(rf"\b{re.escape(campus)}\b", candidate_text, flags=re.IGNORECASE):
            campuses.append(campus)

    if campuses:
        return campuses

    for campus in KNOWN_CAMPUSES:
        if re.search(rf"\b{re.escape(campus)}\b", normalized_text, flags=re.IGNORECASE):
            campuses.append(campus)

    return campuses


def fetch_program_campuses(session: requests.Session, poid: str) -> list[str]:
    """Fetch and parse a program preview page for campus availability."""
    if not poid:
        return []

    url: str = CATALOG_URL_TEMPLATE.format(poid=poid)
    response: requests.Response = session.get(url, timeout=30)
    response.raise_for_status()

    soup: BeautifulSoup = BeautifulSoup(response.text, "html.parser")
    page_text: str = soup.get_text(" ", strip=True)

    campuses: list[str] = extract_locations_from_text(page_text)
    if campuses:
        return campuses

    return extract_locations_from_text(soup.get_text("\n", strip=True))


def enrich_catalog_with_locations(payload: dict[str, object]) -> dict[str, object]:
    """Attach campus availability to every catalog program."""
    programs_obj: object = payload.get("programs")
    if not isinstance(programs_obj, list):
        return payload

    with requests.Session() as session:
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                )
            }
        )

        for index, program in enumerate(programs_obj):
            if not isinstance(program, dict):
                continue

            poid: str = extract_program_poid(program)
            campuses: list[str] = []

            try:
                campuses = fetch_program_campuses(session, poid)
            except requests.RequestException:
                campuses = []

            program["campuses"] = campuses

            if index < len(programs_obj) - 1:
                time.sleep(0.5)

    return payload


def main() -> int:
    """Build the enriched catalog artifact with campus locations."""
    payload: dict[str, object] = load_catalog_payload()
    enriched_payload: dict[str, object] = enrich_catalog_with_locations(payload)
    OUTPUT_PATH.write_text(
        json.dumps(enriched_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote enriched catalog to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
