"""Production crawler for the live Dallas College catalog directory.

Architectural Intent:
    This script performs a full live ingest against the public Dallas College
    catalog site and writes a schema-compatible payload into
    ``data/catalog_mvp.json`` for downstream RAG routing.

Security and Stability Rationale:
    - Uses a realistic browser User-Agent to reduce immediate 403 blocks.
    - Restricts crawl scope to the official catalog host.
    - Applies bounded request timeouts and retry/backoff behavior.
    - Sleeps 1.5 seconds between per-program requests to reduce load and
      lower ban risk.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Target the specific active catalog revision tables directly
BASE_URL: str = "https://catalog.dallascollege.edu/content.php?catoid=4&navoid=944" 
ALLOWED_HOST: str = "catalog.dallascollege.edu"
OUTPUT_PATH: Path = Path(__file__).resolve().parent / "data" / "catalog_mvp.json"
REQUEST_TIMEOUT_SECONDS: float = 25.0
REQUEST_DELAY_SECONDS: float = 1.5

USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

COURSE_CODE_RE: re.Pattern[str] = re.compile(r"\b([A-Z]{4})\s+(\d{4})\b")
TOTAL_HOURS_RE: re.Pattern[str] = re.compile(
    r"(?:minimum\s+hours?\s+required|total\s+credit\s+hours?|total\s+hours?)"
    r"[\s:–\-]*(\d{1,3})",
    re.IGNORECASE,
)
SEMESTER_HEADER_RE: re.Pattern[str] = re.compile(
    r"\b(semester\s+(?:\d+|[ivx]+)|term\s+\d+|year\s+\d+|prerequisite[s]?)\b",
    re.IGNORECASE,
)

LOG: logging.Logger = logging.getLogger("crawl_dallas_college")


@dataclass(frozen=True)
class CrawlConfig:
    """Runtime options for crawl execution."""

    base_url: str = BASE_URL
    output_path: Path = OUTPUT_PATH
    request_delay_seconds: float = REQUEST_DELAY_SECONDS


def _configure_logging() -> None:
    """Initialize console logging with timestamps for progress visibility."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def _build_session() -> requests.Session:
    """Return a retrying HTTP session configured for catalog crawling."""
    session: requests.Session = requests.Session()
    retry: Retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter: HTTPAdapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
    )
    return session


def _is_allowed_catalog_url(url: str) -> bool:
    """Return True when a URL belongs to the official catalog host."""
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.netloc == ALLOWED_HOST


def _fetch_html(session: requests.Session, url: str) -> BeautifulSoup:
    """Fetch and parse one catalog page into BeautifulSoup."""
    if not _is_allowed_catalog_url(url):
        raise ValueError(f"Blocked non-catalog URL: {url}")

    response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def _normalize_url(base_url: str, href: str) -> str:
    """Normalize an href to an absolute URL on the catalog host."""
    absolute_url: str = urljoin(base_url, href)
    return absolute_url.split("#", 1)[0]


def _discover_program_directory_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Find left-nav links likely pointing to programs/degrees directories."""
    candidate_links: set[str] = set()
    nav_keywords: tuple[str, ...] = (
        "courses, degrees and certificates",
        "courses degrees and certificates",
        "programs",
        "degrees",
        "certificates",
    )

    nav_roots: list[Tag] = []
    for selector in (
        "#navigation",
        ".leftnav",
        ".sidebar",
        "nav",
        "#sidebar",
    ):
        nav_roots.extend(soup.select(selector))

    if not nav_roots:
        nav_roots = [soup]

    for root in nav_roots:
        for anchor in root.find_all("a", href=True):
            anchor_text: str = anchor.get_text(" ", strip=True).lower()
            if not any(keyword in anchor_text for keyword in nav_keywords):
                continue
            absolute_url: str = _normalize_url(base_url, anchor["href"])
            if _is_allowed_catalog_url(absolute_url):
                candidate_links.add(absolute_url)

    return sorted(candidate_links)


def _collect_program_links_from_page(soup: BeautifulSoup, page_url: str) -> set[str]:
    """Collect program detail links from one directory page."""
    program_links: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href: str = str(anchor["href"]).strip()
        if "preview_program.php" not in href:
            continue
        absolute_url: str = _normalize_url(page_url, href)
        if _is_allowed_catalog_url(absolute_url):
            program_links.add(absolute_url)
    return program_links


def discover_all_program_links(session: requests.Session, base_url: str) -> list[str]:
    """Discover all unique program links from the root and left-nav directories."""
    root_soup: BeautifulSoup = _fetch_html(session, base_url)
    directory_links: list[str] = _discover_program_directory_links(root_soup, base_url)

    LOG.info("Directory links discovered: %d", len(directory_links))
    for index, link in enumerate(directory_links, start=1):
        LOG.info("  [%d] %s", index, link)

    discovered_programs: set[str] = _collect_program_links_from_page(root_soup, base_url)

    for directory_link in directory_links:
        try:
            directory_soup: BeautifulSoup = _fetch_html(session, directory_link)
            links_on_page: set[str] = _collect_program_links_from_page(directory_soup, directory_link)
            discovered_programs.update(links_on_page)
            LOG.info(
                "Program links on %s: %d (total %d)",
                directory_link,
                len(links_on_page),
                len(discovered_programs),
            )
        except requests.RequestException as exc:
            LOG.warning("Skipping directory page due to request error (%s): %s", directory_link, exc)

    return sorted(discovered_programs)


def _extract_total_hours(soup: BeautifulSoup) -> int:
    """Extract a best-effort total hours integer from page text."""
    page_text: str = soup.get_text(" ", strip=True)
    match: re.Match[str] | None = TOTAL_HOURS_RE.search(page_text)
    if match:
        return int(match.group(1))
    return 60


def _slugify_program_id(title: str) -> str:
    """Create a stable program_id from program title text."""
    return re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_") or "Unknown_Program"


def _infer_degree_code(title: str) -> str:
    """Infer a short degree code from title tokens."""
    lowered = title.lower()
    if "certificate" in lowered:
        degree_prefix = "CERTIFICATE"
    elif "occupational skills award" in lowered:
        degree_prefix = "PROG"
    elif "a.a.s" in lowered or "aas" in lowered:
        degree_prefix = "PROG"
    elif "a.a." in lowered or "a.s." in lowered:
        degree_prefix = "PROG"
    else:
        degree_prefix = "PROG"

    tokens: list[str] = [token for token in re.split(r"\W+", title) if token and len(token) > 2]
    subject: str = tokens[0][:4].upper() if tokens else "GEN"
    return f"{degree_prefix}.{subject}"


def _extract_courses_from_container(container: Tag) -> list[dict[str, str]]:
    """Extract all course rows from one semester/container block."""
    courses: list[dict[str, str]] = []

    for row in container.find_all("tr"):
        row_text: str = row.get_text(" ", strip=True)
        match: re.Match[str] | None = COURSE_CODE_RE.search(row_text)
        if not match:
            continue

        code: str = f"{match.group(1)} {match.group(2)}"
        title_cell = row.find("td", class_="titlecol")
        hours_cell = row.find("td", class_="hourscol")

        title: str
        credits: str
        if isinstance(title_cell, Tag):
            title = title_cell.get_text(" ", strip=True)
        else:
            title = row_text[match.end():].strip(" -:\u00a0") or code

        if isinstance(hours_cell, Tag):
            credits = hours_cell.get_text(strip=True) or "3"
        else:
            credits_match: re.Match[str] | None = re.search(r"(\d+(?:\.\d+)?)\s*$", row_text)
            credits = credits_match.group(1) if credits_match else "3"

        courses.append({"code": code, "title": title or code, "credits": credits})

    if courses:
        return courses

    for item in container.find_all(["li", "p", "div"]):
        item_text: str = item.get_text(" ", strip=True)
        match = COURSE_CODE_RE.search(item_text)
        if not match:
            continue
        code = f"{match.group(1)} {match.group(2)}"
        title = item_text[match.end():].strip(" -:\u00a0") or code
        credits_match = re.search(r"(\d+(?:\.\d+)?)\s*$", item_text)
        credits = credits_match.group(1) if credits_match else "3"
        courses.append({"code": code, "title": title, "credits": credits})

    return courses


def _extract_semesters(soup: BeautifulSoup) -> list[dict[str, object]]:
    """Extract structural semester blocks and mapped courses."""
    semesters: list[dict[str, object]] = []

    for table in soup.find_all("table", class_=lambda c: c and "sc_courselist" in c):
        current_name: str | None = None
        current_courses: list[dict[str, str]] = []

        for row in table.find_all("tr"):
            classes: list[str] = row.get("class") or []
            class_blob: str = " ".join(classes)
            row_text: str = row.get_text(" ", strip=True)

            if "plangridyear" in class_blob or "plangridsubheader" in class_blob:
                if current_name and current_courses:
                    semesters.append({"name": current_name, "courses": current_courses})
                current_name = row_text
                current_courses = []
                continue

            match: re.Match[str] | None = COURSE_CODE_RE.search(row_text)
            if not match:
                continue

            extracted_courses: list[dict[str, str]] = _extract_courses_from_container(row)
            current_courses.extend(extracted_courses)

        if current_name and current_courses:
            semesters.append({"name": current_name, "courses": current_courses})

    if semesters:
        return semesters

    headers: list[Tag] = [
        tag
        for tag in soup.find_all(["h2", "h3", "h4"])
        if isinstance(tag, Tag) and SEMESTER_HEADER_RE.search(tag.get_text(" ", strip=True))
    ]

    for header in headers:
        header_text: str = header.get_text(" ", strip=True)
        semester_courses: list[dict[str, str]] = []
        sibling = header.find_next_sibling()

        while isinstance(sibling, Tag):
            if sibling.name in {"h2", "h3", "h4"} and SEMESTER_HEADER_RE.search(
                sibling.get_text(" ", strip=True)
            ):
                break
            semester_courses.extend(_extract_courses_from_container(sibling))
            sibling = sibling.find_next_sibling()

        if semester_courses:
            semesters.append({"name": header_text, "courses": semester_courses})

    if semesters:
        return semesters

    fallback_courses: list[dict[str, str]] = []
    for text_node in soup.stripped_strings:
        text_line: str = str(text_node)
        match = COURSE_CODE_RE.search(text_line)
        if not match:
            continue
        code = f"{match.group(1)} {match.group(2)}"
        title = text_line[match.end():].strip(" -:\u00a0") or code
        fallback_courses.append({"code": code, "title": title, "credits": "3"})

    if fallback_courses:
        return [{"name": "Program Requirements", "courses": fallback_courses}]

    return []


def _extract_program_name(soup: BeautifulSoup, page_url: str) -> str:
    """Extract a program title from common page heading anchors."""
    heading = soup.find("h1")
    if isinstance(heading, Tag):
        heading_text = heading.get_text(" ", strip=True)
        if heading_text:
            return heading_text

    page_title = soup.find("title")
    if isinstance(page_title, Tag):
        title_text = page_title.get_text(" ", strip=True)
        if title_text:
            return title_text.split("|", 1)[0].strip()

    parsed = urlparse(page_url)
    if parsed.query:
        return parsed.query
    return parsed.path.rsplit("/", maxsplit=1)[-1] or "Unknown Program"


def _map_program(session: requests.Session, program_url: str, index: int, total: int) -> dict[str, object] | None:
    """Crawl and map one program page into schema-compatible program payload."""
    try:
        LOG.info("[%d/%d] Visiting program: %s", index, total, program_url)
        soup: BeautifulSoup = _fetch_html(session, program_url)
    except requests.RequestException as exc:
        LOG.warning("Request failed for %s: %s", program_url, exc)
        return None

    program_title: str = _extract_program_name(soup, program_url)
    semesters: list[dict[str, object]] = _extract_semesters(soup)

    for semester in semesters:
        semester_name = str(semester.get("name", "Requirements"))
        courses = semester.get("courses", [])
        if isinstance(courses, list):
            for course in courses:
                if not isinstance(course, dict):
                    continue
                LOG.info(
                    "    mapped | %s | %s | %s",
                    program_title,
                    semester_name,
                    str(course.get("code", "")).strip(),
                )

    return {
        "program_id": _slugify_program_id(program_title),
        "title": program_title,
        "degree_code": _infer_degree_code(program_title),
        "total_hours": _extract_total_hours(soup),
        "source_url": program_url,
        "semesters": semesters,
    }


def _load_existing_ce_tracks(output_path: Path) -> list[dict[str, object]]:
    """Keep existing CE tracks if already present in catalog_mvp.json."""
    if not output_path.exists():
        return []

    try:
        payload: object = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(payload, dict):
        return []

    ce_programs: object = payload.get("continuing_education_programs")
    if isinstance(ce_programs, list):
        return [program for program in ce_programs if isinstance(program, dict)]

    return []


def _write_payload_atomic(payload: dict[str, object], output_path: Path) -> None:
    """Write crawler payload atomically to avoid partial writes."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path = output_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(output_path)


def crawl_catalog(config: CrawlConfig) -> dict[str, object]:
    """Run full catalog crawl and return schema-compatible payload."""
    session: requests.Session = _build_session()

    LOG.info("Starting crawl at %s", config.base_url)
    program_links: list[str] = discover_all_program_links(session, config.base_url)
    LOG.info("Total discovered program links: %d", len(program_links))

    programs: list[dict[str, object]] = []
    for index, program_url in enumerate(program_links, start=1):
        mapped_program: dict[str, object] | None = _map_program(
            session,
            program_url,
            index,
            len(program_links),
        )
        if mapped_program is not None:
            programs.append(mapped_program)
        time.sleep(config.request_delay_seconds)

    existing_ce_tracks: list[dict[str, object]] = _load_existing_ce_tracks(config.output_path)

    payload: dict[str, object] = {
        "programs": programs,
        "continuing_education_programs": existing_ce_tracks,
    }

    _write_payload_atomic(payload, config.output_path)
    LOG.info(
        "Completed crawl. Programs saved: %d | continuing_education_programs preserved: %d | output: %s",
        len(programs),
        len(existing_ce_tracks),
        config.output_path,
    )
    return payload


def main() -> int:
    """Entrypoint for standalone script execution."""
    _configure_logging()
    config = CrawlConfig()
    try:
        crawl_catalog(config)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("Catalog crawl failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
