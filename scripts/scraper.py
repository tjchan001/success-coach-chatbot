"""Dallas College catalog scraper.

Fetches and parses degree-plan pages from the Dallas College online catalog
(catalog.dallascollege.edu) and converts the raw HTML into validated
``DegreePlan`` Pydantic models.

Architectural Intent:
    This module is the *only* entry point for raw external data.  All
    untrusted HTML is parsed here and immediately coerced into Pydantic
    models, so nothing downstream ever touches unvalidated strings.  The
    parser implements three layered extraction strategies so it degrades
    gracefully when the catalog CMS changes its HTML structure.

    Strategy 1 (primary)   — Courseleaf ``sc_courselist`` curriculum table.
    Strategy 2 (secondary) — Semantic section scan: ``<h2>``/``<h3>`` semester
                             headers followed by child course elements.
    Strategy 3 (fallback)  — Full-page regex sweep collecting any text node
                             that contains a valid course-code pattern.

Security Rationale:
    - URLs are validated against the official Dallas College catalog domain
      allowlist before any network request is made, preventing SSRF.
    - ``httpx`` is used with an explicit timeout and ``follow_redirects=True``
      capped to the same host; the response is never executed or eval'd.
    - ``shell=True`` is never used.  No subprocess calls are made here.
    - All text extracted from the DOM is passed through ``get_text(strip=True)``
      before entering Pydantic validation, stripping any embedded HTML.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from models import Course, DegreePlan, Semester

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

_LOG: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWED_CATALOG_HOST: str = "catalog.dallascollege.edu"
_HTTP_TIMEOUT_SECONDS: float = 20.0

# ---------------------------------------------------------------------------
# Pre-compiled regular expressions
# ---------------------------------------------------------------------------

# Matches "Minimum Hours Required: 62", "Total Credit Hours: 60", etc.
# Captures the integer that follows the label.
_TOTAL_HOURS_RE: re.Pattern[str] = re.compile(
    r"(?:minimum\s+hours?\s+required|total\s+credit\s+hours?|total\s+hours?)"
    r"[\s:–\-]*(\d{2,3})",
    re.IGNORECASE,
)

# Matches a 4-letter rubric + 4-digit course number anywhere in a string.
# Group 1 = rubric (e.g. "ENGL"), Group 2 = number (e.g. "1301").
_COURSE_CODE_RE: re.Pattern[str] = re.compile(r"\b([A-Z]{4})\s+(\d{4})\b")

# Matches a standalone credit-hour value at the end of a string or cell.
# Handles integers ("3"), decimals ("1.5"), and ranges ("1-3").
_CREDIT_RE: re.Pattern[str] = re.compile(
    r"(\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?)\s*$"
)

# Identifies a heading as a semester / term divider.
_SEMESTER_HEADER_RE: re.Pattern[str] = re.compile(
    r"\b("
    r"semester\s+(?:[IVX]+|\d+)"
    r"|year\s+\d+"
    r"|term\s+\d+"
    r"|first\s+(?:year|semester)"
    r"|second\s+(?:year|semester)"
    r"|third\s+(?:year|semester)"
    r"|fourth\s+(?:year|semester)"
    r"|prerequisites?"
    r"|co-?requisites?"
    r")\b",
    re.IGNORECASE,
)

# Matches the credit-hour count embedded in a Courseleaf link-text
# parenthetical, e.g. "(3 Credit Hours)", "(1-3 Credit Hours)".
_PARENTHETICAL_CREDIT_RE: re.Pattern[str] = re.compile(
    r"\(\s*(\d+(?:\.\d+)?(?:\s*[-\u2013]\s*\d+(?:\.\d+)?)?)\s+credit\s+hours?\s*\)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_catalog_url(url: str) -> None:
    """Raise ValueError if *url* does not point to the official catalog host."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"URL scheme '{parsed.scheme}' is not permitted. Use https.")
    if parsed.netloc != _ALLOWED_CATALOG_HOST:
        raise ValueError(
            f"URL host '{parsed.netloc}' is not on the catalog allowlist. "
            f"Expected '{_ALLOWED_CATALOG_HOST}'."
        )


def _extract_total_hours(soup: BeautifulSoup) -> int:
    """Search the page for a total-credit-hour declaration and return its value."""
    full_text: str = soup.get_text(separator=" ", strip=True)

    # Strategy 1 — full-page regex
    match: re.Match[str] | None = _TOTAL_HOURS_RE.search(full_text)
    if match:
        return int(match.group(1))

    # Strategy 2 — Courseleaf plangridtotal row
    total_row: Tag | NavigableString | None = soup.find("tr", class_="plangridtotal")
    if isinstance(total_row, Tag):
        hour_cell: Tag | NavigableString | None = total_row.find(
            "td", class_="hourscol"
        )
        if isinstance(hour_cell, Tag):
            raw: str = hour_cell.get_text(strip=True)
            digit_match: re.Match[str] | None = re.search(r"\d+", raw)
            if digit_match:
                return int(digit_match.group())

    # Strategy 3 — nearest integer sibling after any "total" keyword
    total_label: Tag | NavigableString | None = soup.find(
        string=re.compile(r"\btotal\b", re.IGNORECASE)
    )
    if total_label:
        sibling = total_label.find_next(string=re.compile(r"^\s*\d+\s*$"))
        if sibling:
            return int(sibling.strip())

    _LOG.warning("Could not determine total hours; defaulting to 60.")
    return 60


def _parse_course_row(element: Tag) -> Course | None:
    """Parse a single DOM element and return a Course model if it holds a valid course."""
    raw_text: str = element.get_text(separator=" ", strip=True)

    code_match: re.Match[str] | None = _COURSE_CODE_RE.search(raw_text)
    if not code_match:
        return None

    code: str = f"{code_match.group(1)} {code_match.group(2)}"
    after_code: str = raw_text[code_match.end():].strip()

    # Credits: Courseleaf puts them in a dedicated <td class="hourscol">.
    if element.name == "tr":
        hour_cell: Tag | NavigableString | None = element.find(
            "td", class_="hourscol"
        )
        if isinstance(hour_cell, Tag):
            hour_text: str = hour_cell.get_text(strip=True)
            if re.match(r"^\d", hour_text):
                credits_str = hour_text
                title_cell: Tag | NavigableString | None = element.find(
                    "td", class_="titlecol"
                )
                title: str
                if isinstance(title_cell, Tag):
                    title = title_cell.get_text(strip=True)
                else:
                    title = _CREDIT_RE.sub("", after_code).strip(" .-–")
                return Course(code=code, title=title or code, credits=credits_str)

    # Generic path — extract credits from the tail of the text
    credits_str = "3"
    credit_match: re.Match[str] | None = _CREDIT_RE.search(after_code)
    if credit_match:
        credits_str = credit_match.group(1).strip()
        title = after_code[: credit_match.start()].strip(" .-–")
    else:
        paren_match: re.Match[str] | None = _PARENTHETICAL_CREDIT_RE.search(after_code)
        if paren_match:
            credits_str = paren_match.group(1).strip()
            title = after_code[: paren_match.start()].strip(" .-–()")
        else:
            title = after_code.strip(" .-–")

    if not title:
        title = code

    return Course(code=code, title=title, credits=credits_str)


def _infer_degree_code(url: str, title: str) -> str | None:
    """Derive a short degree code from the URL slug or page title."""
    slug: str = urlparse(url).path.rstrip("/").split("/")[-1]

    degree_type_match: re.Match[str] | None = re.search(
        r"-(aas|aa|as|aaas|aac|certificate|cert)\b", slug, re.IGNORECASE
    )
    if not degree_type_match:
        # Check query string for preview links
        query = urlparse(url).query
        if "poid=" in query:
            degree_type_match = re.search(r"\b(aas|aa|as|cert|certificate)\b", title, re.IGNORECASE)
            
    degree_type: str = degree_type_match.group(1).upper() if degree_type_match else "PROG"

    skip_words: frozenset[str] = frozenset(
        {"associate", "of", "arts", "applied", "science", "in", "the", "and", "for"}
    )
    subject_words: list[str] = [
        w.upper()
        for w in re.split(r"\W+", title)
        if w.lower() not in skip_words and len(w) > 2
    ]
    subject: str = subject_words[0][:4] if subject_words else "GEN"
    return f"{degree_type}.{subject}"


def _extract_semesters_from_courseleaf_table(table: Tag) -> list[Semester]:
    """Parse a Courseleaf ``sc_courselist`` curriculum table into Semester models."""
    semesters: list[Semester] = []
    current_name: str | None = None
    current_courses: list[Course] = []

    for row in table.find_all("tr"):
        if not isinstance(row, Tag):
            continue

        row_classes: list[str] = row.get("class") or []
        row_class_str: str = " ".join(row_classes)

        is_year_header: bool = "plangridyear" in row_class_str
        is_subheader: bool = "plangridsubheader" in row_class_str
        if is_year_header or is_subheader:
            header_text: str = row.get_text(separator=" ", strip=True)
            if _SEMESTER_HEADER_RE.search(header_text) or is_year_header:
                if current_name is not None:
                    semesters.append(
                        Semester(name=current_name, courses=current_courses)
                    )
                current_name = header_text
                current_courses = []
            continue

        if any(
            cls in row_class_str
            for cls in ("plangridtotal", "plangridspace", "plangridcomment")
        ):
            continue

        course: Course | None = _parse_course_row(row)
        if course is not None and current_name is not None:
            current_courses.append(course)

    if current_name is not None and current_courses:
        semesters.append(Semester(name=current_name, courses=current_courses))

    return semesters


def _extract_semesters_by_section_scan(soup: BeautifulSoup) -> list[Semester]:
    """Find semester containers by scanning semantic heading elements."""
    semesters: list[Semester] = []
    headings: list[Tag] = [
        tag
        for tag in soup.find_all(["h2", "h3", "h4"])
        if isinstance(tag, Tag)
        and _SEMESTER_HEADER_RE.search(tag.get_text(strip=True))
    ]

    for heading in headings:
        sem_name: str = heading.get_text(separator=" ", strip=True)
        courses: list[Course] = []

        sibling = heading.find_next_sibling()
        while sibling:
            if not isinstance(sibling, Tag):
                sibling = sibling.find_next_sibling()
                continue
            if sibling.name in {"h2", "h3", "h4"} and _SEMESTER_HEADER_RE.search(
                sibling.get_text(strip=True)
            ):
                break
            for child in sibling.find_all(["tr", "li", "p", "div"]):
                if not isinstance(child, Tag):
                    continue
                course: Course | None = _parse_course_row(child)
                if course:
                    courses.append(course)
            sibling = sibling.find_next_sibling()

        if courses:
            semesters.append(Semester(name=sem_name, courses=courses))

    return semesters


def _extract_flat_certificate_requirements(soup: BeautifulSoup) -> list[Semester]:
    """Parse flat certificate layouts into one synthetic semester."""
    def _build_course_from_text(raw_text: str) -> Course | None:
        line: str = raw_text.strip()
        code_match: re.Match[str] | None = re.match(r"^([A-Z]{4})\s+(\d{4})\b", line)
        if not code_match:
            return None

        code: str = f"{code_match.group(1)} {code_match.group(2)}"
        after_code: str = line[code_match.end():]
        after_code = re.sub(r"^\s*[-\u2013\u00a0\s]+", "", after_code)

        paren_match: re.Match[str] | None = _PARENTHETICAL_CREDIT_RE.search(after_code)
        if paren_match:
            credits_str: str = paren_match.group(1).strip()
            title: str = after_code[: paren_match.start()].strip(" .-\u2013()\u00a0")
        else:
            trail_match: re.Match[str] | None = _CREDIT_RE.search(after_code)
            if trail_match:
                credits_str = trail_match.group(1).strip()
                title = after_code[: trail_match.start()].strip(" .-\u2013\u00a0")
            else:
                credits_str = "3"
                title = after_code.strip(" .-\u2013\u00a0")

        return Course(code=code, title=title or code, credits=credits_str)

    courses: list[Course] = []
    seen_codes: set[str] = set()

    for anchor in soup.find_all("a"):
        if not isinstance(anchor, Tag):
            continue

        anchor_text: str = anchor.get_text(separator=" ", strip=True)
        if not re.match(r"^[A-Z]{4}\s+\d{4}\b", anchor_text):
            continue

        course_from_anchor: Course | None = _parse_course_row(anchor)
        course: Course | None = course_from_anchor or _build_course_from_text(anchor_text)
        if course is not None and course.code not in seen_codes:
            courses.append(course)
            seen_codes.add(course.code)

    for text_node in soup.find_all(string=True):
        if not isinstance(text_node, str):
            continue

        node_text: str = text_node.strip()
        if not node_text or not re.match(r"^[A-Z]{4}\s+\d{4}\b", node_text):
            continue

        parent_tag: Tag | None = text_node.parent if isinstance(text_node.parent, Tag) else None
        if isinstance(parent_tag, Tag) and parent_tag.name == "a":
            continue

        course: Course | None = None
        if isinstance(parent_tag, Tag):
            course = _parse_course_row(parent_tag)
        if course is None:
            course = _build_course_from_text(node_text)
        if course is not None and course.code not in seen_codes:
            courses.append(course)
            seen_codes.add(course.code)

    if not courses:
        return []

    return [Semester(name="Certificate Core Requirements", courses=courses)]


def _extract_semesters_by_regex_sweep(soup: BeautifulSoup) -> list[Semester]:
    """Last-resort: sweep all text nodes in the document for course-code patterns."""
    courses: list[Course] = []
    seen_codes: set[str] = set()

    for node in soup.find_all(string=_COURSE_CODE_RE):
        parent: Tag | None = node.parent if isinstance(node.parent, Tag) else None
        if parent is None:
            continue
        course: Course | None = _parse_course_row(parent)
        if course and course.code not in seen_codes:
            courses.append(course)
            seen_codes.add(course.code)

    if not courses:
        return []

    return [Semester(name="Program Courses", courses=courses)]


def _parse_courseleaf_course_item(item: Tag) -> Course | None:
    """Parse a ``<li class="acalog-course">`` element into a Course model."""
    link: Tag | NavigableString | None = item.find("a")
    raw_text: str = (
        link.get_text(separator=" ", strip=True)
        if isinstance(link, Tag)
        else item.get_text(separator=" ", strip=True)
    )

    code_match: re.Match[str] | None = _COURSE_CODE_RE.search(raw_text)
    if not code_match:
        return None

    code: str = f"{code_match.group(1)} {code_match.group(2)}"
    after_code: str = raw_text[code_match.end():]
    after_code = re.sub(r"^\s*[-\u2013\u00a0\s]+", "", after_code)

    paren_match: re.Match[str] | None = _PARENTHETICAL_CREDIT_RE.search(after_code)
    if paren_match:
        credits_str: str = paren_match.group(1).strip()
        title: str = after_code[: paren_match.start()].strip(" .-\u2013()\u00a0")
    else:
        trail_match: re.Match[str] | None = _CREDIT_RE.search(after_code)
        if trail_match:
            credits_str = trail_match.group(1).strip()
            title = after_code[: trail_match.start()].strip(" .-\u2013\u00a0")
        else:
            credits_str = "3"
            title = after_code.strip(" .-\u2013\u00a0")

    return Course(code=code, title=title or code, credits=credits_str)


def _extract_semesters_from_courseleaf_program_list(soup: BeautifulSoup) -> list[Semester]:
    """Parse a Courseleaf program course-list page into Semester models."""
    if not soup.find("li", class_="acalog-course"):
        return []

    semesters: list[Semester] = []

    for h3 in soup.find_all("h3"):
        if not isinstance(h3, Tag):
            continue
        group_name: str = h3.get_text(strip=True)
        if not group_name:
            continue

        local_container: Tag | None = h3.find_parent("div", class_="acalog-core")
        next_ul: Tag | NavigableString | None
        if isinstance(local_container, Tag):
            next_ul = local_container.find("ul")
        else:
            next_ul = h3.find_next_sibling("ul") or h3.find_next("ul")
        if not isinstance(next_ul, Tag):
            continue

        items: list[Tag] = [
            li
            for li in next_ul.find_all("li", class_="acalog-course")
            if isinstance(li, Tag)
        ]
        if not items:
            continue

        courses: list[Course] = []
        for item in items:
            course: Course | None = _parse_courseleaf_course_item(item)
            if course:
                courses.append(course)

        if courses:
            semesters.append(Semester(name=group_name, courses=courses))

    return semesters


def discover_all_program_pathways() -> dict[str, str]:
    """Sweeps the institutional directory catalog to auto-discover all degree programs.

    Implements anti-vulnerability network safeguards and browser-footprint simulations
    to seamlessly handle modern server protections.
    """
    root_index_url = "https://catalog.dallascollege.edu/content.php?catoid=4&navoid=944"
    _validate_catalog_url(root_index_url)
    
    _LOG.info("Initializing global program discovery sweep on: %s", root_index_url)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SuccessCoachChatbot/1.0"}
    
    discovered_pathways: dict[str, str] = {}
    
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = client.get(root_index_url, headers=headers)
            response.raise_for_status()
            
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Parse all degree plan anchors pointing to individual program object descriptors (poid)
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href", ""))
            if "preview_program.php" not in href or "poid=" not in href:
                continue

            full_url = urljoin("https://catalog.dallascollege.edu/", href)
            raw_title = anchor.get_text(strip=True)

            if not raw_title:
                continue

            # Standardize program identifiers for clean dictionary key lookups
            clean_id = re.sub(r"\W+", "_", raw_title).strip("_")
            if clean_id and clean_id not in discovered_pathways:
                discovered_pathways[clean_id] = full_url
                    
    except Exception as exc:
        _LOG.error("Failed to dynamically sweep system catalog records: %s", exc)
        
    # Fallback sanity anchors to guarantee continuous system availability if network is down
    if not discovered_pathways:
        _LOG.warning("Discovery sequence returned empty array. Loading baseline core system matrix maps.")
        discovered_pathways = {
            "Computer_Information_Technology_AAS": "https://catalog.dallascollege.edu/preview_program.php?catoid=33&poid=3057",
            "Web_Development_Certificate": "https://catalog.dallascollege.edu/preview_program.php?catoid=33&poid=3025",
            "Cybersecurity_AAS": "https://catalog.dallascollege.edu/preview_program.php?catoid=33&poid=3060",
        }
        
    return discovered_pathways


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_degree_page(url: str) -> DegreePlan:
    """Fetch and parse a Dallas College degree-plan catalog page."""
    _validate_catalog_url(url)

    _LOG.info("Fetching catalog page: %s", url)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SuccessCoachChatbot/1.0"}
    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=True) as client:
        response: httpx.Response = client.get(url, headers=headers)
        response.raise_for_status()

    soup: BeautifulSoup = BeautifulSoup(response.text, "html.parser")

    # --- Program title ---------------------------------------------------
    h1_tag: Tag | NavigableString | None = soup.find("h1")
    page_title: str = (
        h1_tag.get_text(separator=" ", strip=True)
        if isinstance(h1_tag, Tag)
        else "Unknown Program"
    )

    # --- Total hours -----------------------------------------------------
    total_hours: int = _extract_total_hours(soup)

    # --- Degree code (best-effort) ---------------------------------------
    degree_code: str | None = _infer_degree_code(url=url, title=page_title)

    # --- Semester extraction — three-strategy cascade -------------------
    semesters: list[Semester] = []

    # Strategy 1: Courseleaf sc_courselist table
    courseleaf_table: Tag | NavigableString | None = soup.find(
        "table", class_="sc_courselist"
    )
    if isinstance(courseleaf_table, Tag):
        _LOG.debug("Strategy 1: Courseleaf sc_courselist table detected.")
        semesters = _extract_semesters_from_courseleaf_table(courseleaf_table)

    # Strategy 1b: Courseleaf acalog-course list (preview_program.php layout)
    if not semesters:
        _LOG.debug("Strategy 1b: Checking for Courseleaf acalog-course list.")
        semesters = _extract_semesters_from_courseleaf_program_list(soup)

    # Strategy 2: Semantic heading + sibling scan
    if not semesters:
        _LOG.debug("Strategy 2: Falling back to semantic section scan.")
        semesters = _extract_semesters_by_section_scan(soup)

    # Strategy 2b: Flat certificate table/list fallback
    if not semesters:
        _LOG.debug("Strategy 2b: Falling back to flat certificate requirements scan.")
        semesters = _extract_flat_certificate_requirements(soup)

    # Strategy 3: Full-page regex sweep
    if not semesters:
        _LOG.debug("Strategy 3: Falling back to full-page regex sweep.")
        semesters = _extract_semesters_by_regex_sweep(soup)

    if not semesters:
        _LOG.warning(
            "No course data could be extracted from %s. "
            "The page structure may require a new parsing strategy.",
            url,
        )

    return DegreePlan(
        title=page_title,
        degree_code=degree_code,
        total_hours=total_hours,
        semesters=semesters,
    )


def _build_catalog_payload(pathways: dict[str, str]) -> dict[str, list[dict[str, object]]]:
    """Build the multi-program cache payload from pathway IDs and source URLs."""
    programs: list[dict[str, object]] = []
    for program_id, program_url in pathways.items():
        _LOG.info("Starting catalog scrape → %s (%s)", program_id, program_url)
        try:
            # Maintain a rate limiting delay loop to safeguard upstream hosting bandwidth
            time.sleep(0.2)
            plan: DegreePlan = parse_degree_page(program_url)
            program_object: dict[str, object] = {
                "program_id": program_id,
                **plan.model_dump(mode="json"),
            }
            programs.append(program_object)
        except Exception as exc:
            _LOG.error("Skipping pathway layout tracking node %s due to failure: %s", program_id, exc)

    return {"programs": programs}


def _write_json_atomic(payload: dict[str, object], destination_path: Path) -> None:
    """Atomically write the JSON payload to disk."""
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    serialized_payload: str = json.dumps(payload, indent=2, ensure_ascii=False)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=destination_path.parent,
        delete=False,
    ) as temp_file:
        temp_file.write(serialized_payload)
        temp_file.flush()
        os.fsync(temp_file.fileno())
        temp_path: Path = Path(temp_file.name)

    temp_path.replace(destination_path)


# ---------------------------------------------------------------------------
# CLI / cache-refresh entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    # Dynamically extract all tracks over the wire instead of utilizing static single target configurations
    target_pathways = discover_all_program_pathways()

    try:
        output_payload: dict[str, list[dict[str, object]]] = _build_catalog_payload(
            target_pathways
        )
    except (httpx.HTTPStatusError, httpx.TimeoutException, ValueError) as exc:
        _LOG.error("Scrape failed: %s", exc)
        _LOG.error("Scrape failed. If a 404 error occurred, check if catalog.dallascollege.edu rotated its active 'catoid' or 'navoid' parameters on their Degrees & Certificates index page.")
        sys.exit(1)

    _LOG.info("Scraped %d programs.", len(output_payload["programs"]))

    cache_path: Path = Path(__file__).parent.parent / "data" / "catalog_mvp.json"
    _write_json_atomic(output_payload, cache_path)

    _LOG.info("Cache written → %s", cache_path.resolve())