"""FastAPI chat router backed by Groq with Gemini fallback.

Architectural Intent:
    This module is the sole HTTP boundary between the floating chat widget
    and the catalog-backed advisory assistant. It injects the local Dallas
    College catalog cache into one shared advisor prompt so both providers
    answer from the same verified repository data.

Security Rationale:
    - Request bodies are validated with immutable Pydantic v2 models before
      they enter prompt-building or network code.
    - The catalog payload is read only from the local JSON cache and minified
      before prompt injection to reduce token cost and prompt surface area.
    - Groq and Gemini API keys are read only from environment variables and
      rotated on HTTP 429 responses to tolerate free-tier rate limits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, TYPE_CHECKING

try:
    import httpx
except ImportError as exc:  # pragma: no cover
    httpx = None  # type: ignore[assignment]
    _HTTPX_IMPORT_ERROR: ImportError | None = exc
else:
    _HTTPX_IMPORT_ERROR = None
if TYPE_CHECKING:
    from httpx import AsyncClient as HttpxAsyncClient
    from httpx import Response as HttpxResponse
else:
    HttpxAsyncClient = object
    HttpxResponse = object
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

_LOG: logging.Logger = logging.getLogger(__name__)

_APP_TITLE: str = "Dallas College Chatbot API"
_APP_VERSION: str = "0.3.0"
_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
_CATALOG_CACHE_PATH: Path = _PROJECT_ROOT / "data" / "catalog_mvp.json"
_HTTP_TIMEOUT_SECONDS: float = 30.0
_CONTEXT_CHAR_BUDGET: int = 6000


def _resolve_analytics_log_path() -> Path:
    """Resolve a writable analytics log path across local and serverless environments."""
    if os.environ.get("VERCEL") or not os.access(str(_PROJECT_ROOT), os.W_OK):
        return Path(tempfile.gettempdir()) / "analytics_logs.json"
    return _PROJECT_ROOT / "data" / "analytics_logs.json"


_ANALYTICS_LOG_PATH: Path = _resolve_analytics_log_path()

_GROQ_MODEL_NAME: str = "llama-3.1-8b-instant"
_GROQ_CHAT_URL: str = "https://api.groq.com/openai/v1/chat/completions"

_GEMINI_MODEL_NAME: str = "gemini-1.5-flash"
_GEMINI_GENERATE_URL: str = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-1.5-flash:generateContent"
)

_EMPTY_CONTEXT_FALLBACK: str = "Academic catalog context unavailable. Connection terminal error."
_SELECTION_GUARDRAIL: str = (
    "I cannot confirm that selection based on the current catalog data. "
    "Please consult a Dallas College success coach or human advisor for verified pathways."
)
_MANDATORY_GOVERNANCE_GREETING: str = (
    "Greetings, I am the automated AI Advisor running on the Dallas College AI Club Sandbox Engine."
)
_MANDATORY_SANDBOX_DISCLAIMER_NOTICE: str = (
    "*(This application is a student-led AI Club sandbox demo and is not an officially sanctioned "
    "tool of Dallas College. For binding degree planning and institutional support, connect directly "
    "with a human advisor at the Official Dallas College Support Directory: "
    "https://www.dallascollege.edu/contact).*"
)
_MANDATORY_RESPONSE_PREFIX: str = (
    "> *Greetings, I am the automated AI Advisor running on the Dallas College AI Club Sandbox "
    "Engine. (This application is a student-led AI Club sandbox demo and is not an officially "
    "sanctioned tool of Dallas College. For binding degree planning and institutional support, "
    "connect directly with a human advisor at the Official Dallas College Support Directory: "
    "https://www.dallascollege.edu/contact.)*"
)
_OUT_OF_BOUNDS_REPLY: str = (
    "I can only assist with Dallas College academic advising topics. "
    "Please ask about degree plans, certificates, pathways, or course requirements."
)
_EMERGENCY_KEYWORDS: tuple[str, ...] = (
    "emergency",
    "active shooter",
    "911",
    "fire",
    "ambulance",
    "call police",
    "campus police phone",
    "reporting a crime",
    "assault",
    "suicide",
    "crisis",
    "bleeding",
    "hurt",
)

_EMERGENCY_REPLY: str = (
    "⚠️ **DALLAS COLLEGE EMERGENCY REACTION PROTOCOL** ⚠️\n\n"
    "If you are experiencing an immediate life-threatening emergency on campus, "
    "**dial 911** or contact the **Dallas College Police Department Dispatch** instantly:\n"
    "- **Phone (All Campuses):** 972-860-4290\n"
    "- **From Campus Phones:** Dial 911\n\n"
    "Please stay safe, follow institutional building evacuation markers, or shelter in place "
    "depending on your situation. (This automated advisory sandbox cannot route emergency services.)"
)
_COURSE_CODE_PATTERN: re.Pattern[str] = re.compile(r"\b([A-Z]{4})\s*(\d{4})\b", re.IGNORECASE)

CAREER_CLUSTER_MAP: dict[str, list[str]] = {
    "cooking": ["chef", "pstr", "culinary arts"],
    "programming": ["itse", "inew", "software"],
    "cybersecurity": ["cyber", "itnw", "network security"],
    "networking": ["itnw", "network", "infrastructure"],
    "web": ["itse", "web", "frontend"],
    "business": ["bcis", "business", "applications"],
    "real estate": ["real estate", "rele", "business", "busi", "bmgt"],
    "realestate": ["real estate", "rele", "business", "busi", "bmgt"],
    "police": ["crij", "criminal justice", "law enforcement"],
    "officer": ["crij", "criminal justice", "law enforcement"],
    "cop": ["crij", "criminal justice", "law enforcement"],
}


def expand_user_query(query_text: str) -> list[str]:
    """Expand a user query with cluster-derived search targets."""
    normalized_query: str = query_text.lower().strip()
    if not normalized_query:
        return []

    expanded_terms: list[str] = [normalized_query]
    seen_terms: set[str] = {normalized_query}

    for cluster_term, cluster_targets in CAREER_CLUSTER_MAP.items():
        if cluster_term not in normalized_query:
            continue
        for target in cluster_targets:
            normalized_target: str = target.lower().strip()
            if not normalized_target or normalized_target in seen_terms:
                continue
            expanded_terms.append(normalized_target)
            seen_terms.add(normalized_target)

    return expanded_terms


class CatalogSearchEngine:
    """In-memory catalog indexer and context slicer for token-efficient prompts."""

    def __init__(self, cache_path: Path, char_budget: int = _CONTEXT_CHAR_BUDGET) -> None:
        self.cache_path: Path = cache_path
        self.char_budget: int = char_budget
        self._catalog_payload: dict[str, object] = self._load_catalog_payload()
        self._programs_cache: list[dict[str, object]] | None = None
        self._prerequisite_index_cache: dict[str, dict[str, list[str]]] | None = None
        self._intent_map: dict[str, tuple[str, ...]] = {
            "Cybersecurity_AAS": ("cyber", "security", "network", "networking"),
            "Web_Development_Certificate": ("web", "html", "css", "frontend"),
            "Computer_Information_Technology_AAS": (
                "computer",
                "information",
                "technology",
                "it",
                "cit",
            ),
        }
        self._academic_keywords: tuple[str, ...] = (
            "dallas college",
            "degree",
            "plan",
            "certificate",
            "course",
            "class",
            "credit",
            "semester",
            "program",
            "pathway",
            "major",
            "advisor",
            "coach",
            "catalog",
            "curriculum",
            "cyber",
            "security",
            "network",
            "web",
            "html",
            "css",
            "frontend",
            "computer",
            "information",
            "technology",
            "cit",
            "bcis",
            "itsc",
            "cosc",
            "itnw",
            "itse",
        )
        self._non_academic_keywords: tuple[str, ...] = (
            "poem",
            "weather",
            "recipe",
            "stock",
            "sports",
            "joke",
            "movie",
            "song",
            "travel",
            "bitcoin",
            "horoscope",
            "game",
        )
        self._geographic_keywords: tuple[str, ...] = (
            "campus",
            "campuses",
            "location",
            "locations",
            "where is",
            "where are",
            "offered at",
            "where can i take",
        )
        self._degree_layout_keywords: tuple[str, ...] = (
            "degree plan",
            "show my degree",
            "degree layout",
            "course layout",
            "program layout",
            "requirements",
            "checklist",
        )

    def _load_catalog_payload(self) -> dict[str, object]:
        if not self.cache_path.exists():
            raise RuntimeError(f"Catalog cache not found at '{self.cache_path}'.")

        try:
            payload: object = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Catalog cache could not be loaded.") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("Catalog cache root must be a JSON object.")

        return payload

    def _programs(self) -> list[dict[str, object]]:
        if self._programs_cache is not None:
            return self._programs_cache

        normalized_programs: list[dict[str, object]] = []
        programs: object = self._catalog_payload.get("programs")
        if isinstance(programs, list):
            normalized_programs.extend(program for program in programs if isinstance(program, dict))

        continuing_education_programs: object = self._catalog_payload.get(
            "continuing_education_programs"
        )
        if isinstance(continuing_education_programs, list):
            normalized_programs.extend(
                program for program in continuing_education_programs if isinstance(program, dict)
            )

        self._programs_cache = normalized_programs
        return self._programs_cache

    def _classify_program_intent(self, user_query: str) -> str | None:
        lowered_query: str = user_query.lower()
        for program_id, keywords in self._intent_map.items():
            if any(keyword in lowered_query for keyword in keywords):
                return program_id
        return None

    def classify_intent(self, user_query: str) -> str:
        if self.is_out_of_bounds_query(user_query):
            return "OUT_OF_BOUNDS"
        if self._is_degree_layout_request(user_query):
            return "DEGREE_LAYOUT"
        if self._classify_program_intent(user_query) is not None:
            return "PROGRAM_FOCUSED"
        return "GENERIC_CATALOG"

    def get_matched_keywords(self, user_query: str) -> list[str]:
        lowered_query: str = user_query.lower()
        matched: set[str] = set()

        for keyword in self._academic_keywords:
            if keyword in lowered_query:
                matched.add(keyword)
        for keyword in self._non_academic_keywords:
            if keyword in lowered_query:
                matched.add(keyword)
        for keywords in self._intent_map.values():
            for keyword in keywords:
                if keyword in lowered_query:
                    matched.add(keyword)

        for rubric, number in _COURSE_CODE_PATTERN.findall(user_query):
            course_code: str = f"{rubric.upper()} {number}"
            matched.add(course_code)
            matched.add(self._build_course_catalog_url(course_code))

        return sorted(matched)

    def is_out_of_bounds_query(self, user_query: str) -> bool:
        query_lower: str = user_query.lower().strip()
        if not query_lower:
            return True

        geo_keywords: tuple[str, ...] = (
            "campus",
            "campuses",
            "location",
            "locations",
            "where",
            "offered",
            "teach",
            "taught",
        )
        if any(keyword in query_lower for keyword in geo_keywords):
            return False

        if re.search(r"\b[a-z]{4}\s*\d{4}\b", query_lower):
            return False

        has_academic_signal: bool = any(
            keyword in query_lower for keyword in self._academic_keywords
        )
        has_non_academic_signal: bool = any(
            keyword in query_lower for keyword in self._non_academic_keywords
        )

        if has_non_academic_signal and not has_academic_signal:
            return True

        return not has_academic_signal

    def _is_degree_layout_request(self, user_query: str) -> bool:
        lowered_query: str = user_query.lower()
        return any(keyword in lowered_query for keyword in self._degree_layout_keywords)

    def _extract_course_codes(self, raw_text: str) -> list[str]:
        normalized_codes: list[str] = []
        for rubric, number in _COURSE_CODE_PATTERN.findall(raw_text):
            normalized_codes.append(f"{rubric.upper()} {number}")
        return normalized_codes

    def _extract_course_headers_from_query(self, user_query: str) -> list[str]:
        return self._extract_course_codes(user_query)

    def _lookup_program_titles_from_query(self, user_query: str) -> list[str]:
        lowered_query: str = user_query.lower()
        matched_titles: list[str] = []
        seen_titles: set[str] = set()
        for program in self._programs():
            raw_title: object = program.get("title", "")
            program_title: str = raw_title.strip() if isinstance(raw_title, str) else ""
            if not program_title:
                continue
            lowered_title: str = program_title.lower()
            if lowered_title in lowered_query and lowered_title not in seen_titles:
                matched_titles.append(program_title)
                seen_titles.add(lowered_title)
        return matched_titles

    def _build_course_catalog_url(self, course_code: str) -> str:
        normalized_course_code: str = re.sub(r"\s+", " ", course_code).strip()
        encoded_course_code: str = normalized_course_code.replace(" ", "+")
        return (
            "https://catalog.dallascollege.edu/search_advanced.php?cur_cat_oid=5&"
            f"search_keyword={encoded_course_code}"
        )

    def _build_program_catalog_url(self, program_title: str) -> str:
        normalized_program_title: str = re.sub(r"\s+", " ", program_title).strip()
        encoded_program_title: str = normalized_program_title.replace(" ", "+")
        return (
            "https://catalog.dallascollege.edu/preview_program.php?m=Programs&"
            f"keyword={encoded_program_title}"
        )

    def _collect_valid_course_codes(self, programs: list[dict[str, object]]) -> list[str]:
        valid_codes: set[str] = set()
        for program in programs:
            if not isinstance(program, dict):
                continue

            semesters: object = program.get("semesters")
            if isinstance(semesters, list):
                for semester in semesters:
                    if not isinstance(semester, dict):
                        continue
                    courses: object = semester.get("courses")
                    if not isinstance(courses, list):
                        continue
                    for course in courses:
                        if not isinstance(course, dict):
                            continue
                        raw_code: object = course.get("code", "")
                        course_code: str = raw_code.strip() if isinstance(raw_code, str) else ""
                        if course_code:
                            valid_codes.add(course_code)

            tracks: object = program.get("tracks")
            if isinstance(tracks, list):
                for track in tracks:
                    if not isinstance(track, dict):
                        continue
                    courses = track.get("courses")
                    if not isinstance(courses, list):
                        continue
                    for course in courses:
                        if not isinstance(course, dict):
                            continue
                        raw_code = course.get("code", "")
                        course_code = raw_code.strip() if isinstance(raw_code, str) else ""
                        if course_code:
                            valid_codes.add(course_code)

        return sorted(valid_codes)

    def _append_verified_whitelist_line(self, context_chunk: str, valid_codes: list[str]) -> str:
        whitelist_line: str = (
            "\nVERIFIED COURSE CODE WHITELIST (STRICT COMPLIANCE REQUIRED): "
            f"{valid_codes}"
        )
        if len(context_chunk) + len(whitelist_line) <= self.char_budget:
            return f"{context_chunk}{whitelist_line}"

        remaining_budget: int = self.char_budget - len(whitelist_line)
        if remaining_budget > 0:
            return f"{context_chunk[:remaining_budget]}{whitelist_line}"
        return whitelist_line[: self.char_budget]

    def _normalize_campuses(self, program: dict[str, object]) -> list[str]:
        """Normalize campus metadata from a program row into a clean list."""
        campuses_obj: object = program.get("campuses", [])
        if not isinstance(campuses_obj, list):
            return []

        campuses: list[str] = []
        seen_campuses: set[str] = set()
        for campus in campuses_obj:
            campus_name: str = str(campus).strip()
            if not campus_name or campus_name in seen_campuses:
                continue
            campuses.append(campus_name)
            seen_campuses.add(campus_name)
        return campuses

    def _build_fragment_header(self, program: dict[str, object], fragment_text: str) -> str:
        """Prefix a context fragment with explicit program and campus metadata."""
        program_title: str = str(program.get("title", "")).strip() or str(
            program.get("program_id", "")
        ).strip()
        campuses: list[str] = self._normalize_campuses(program)
        campus_string: str = ", ".join(campuses) if campuses else "Online / General Catalog"
        return (
            f"PROGRAM: {program_title}\n"
            f"CAMPUSES: {campus_string}\n"
            f"CONTEXT FRAGMENT: {fragment_text}\n"
            "---\n"
        )

    def _resolve_semantic_program_titles(self, user_query: str) -> list[str]:
        """Map broader skill phrases to veterinary program titles."""
        lowered_query: str = user_query.lower()
        semantic_signals: tuple[str, ...] = ("animal medicine", "animal care", "vet")
        if not any(signal in lowered_query for signal in semantic_signals):
            return []

        matched_titles: list[str] = []
        seen_titles: set[str] = set()
        for program in self._programs():
            raw_title: object = program.get("title", "")
            program_title: str = raw_title.strip() if isinstance(raw_title, str) else ""
            program_id: str = str(program.get("program_id", "")).strip()
            lowered_title: str = program_title.lower()
            lowered_program_id: str = program_id.lower()
            if "veterinary" not in lowered_title and "veterinary" not in lowered_program_id:
                continue
            if lowered_title in seen_titles:
                continue
            seen_titles.add(lowered_title)
            matched_titles.append(program_title)

        return matched_titles

    def _find_program_course_matches(
        self,
        course_codes: set[str],
    ) -> list[dict[str, object]]:
        """Locate parent program rows for the requested course codes."""
        matched_rows: list[dict[str, object]] = []
        seen_matches: set[tuple[str, str, str]] = set()

        for program in self._programs():
            if not isinstance(program, dict):
                continue

            program_id: str = str(program.get("program_id", "")).strip()
            semesters: object = program.get("semesters")
            if isinstance(semesters, list):
                for semester in semesters:
                    if not isinstance(semester, dict):
                        continue
                    semester_name: str = str(semester.get("name", "Requirements")).strip() or "Requirements"
                    courses: object = semester.get("courses")
                    if not isinstance(courses, list):
                        continue
                    for course in courses:
                        if not isinstance(course, dict):
                            continue
                        raw_code: object = course.get("code", "")
                        course_code: str = raw_code.strip() if isinstance(raw_code, str) else ""
                        if not course_code or course_code.lower() not in course_codes:
                            continue
                        match_key: tuple[str, str, str] = (program_id, course_code.lower(), semester_name)
                        if match_key in seen_matches:
                            continue
                        seen_matches.add(match_key)
                        matched_rows.append(
                            {
                                "program": program,
                                "semester_name": semester_name,
                                "course": course,
                            }
                        )

            tracks: object = program.get("tracks")
            if isinstance(tracks, list):
                for track in tracks:
                    if not isinstance(track, dict):
                        continue
                    track_name: str = str(track.get("name", "Requirements")).strip() or "Requirements"
                    courses: object = track.get("courses")
                    if not isinstance(courses, list):
                        continue
                    for course in courses:
                        if not isinstance(course, dict):
                            continue
                        raw_code = course.get("code", "")
                        course_code = raw_code.strip() if isinstance(raw_code, str) else ""
                        if not course_code or course_code.lower() not in course_codes:
                            continue
                        match_key = (program_id, course_code.lower(), track_name)
                        if match_key in seen_matches:
                            continue
                        seen_matches.add(match_key)
                        matched_rows.append(
                            {
                                "program": program,
                                "semester_name": track_name,
                                "course": course,
                            }
                        )

        return matched_rows

    def _build_rehydrated_course_fragment(
        self,
        program: dict[str, object],
        semester_name: str,
        course: dict[str, object],
    ) -> str:
        """Build a course fragment with parent program and campus metadata."""
        parent_program_title: str = str(program.get("title", "")).strip() or str(
            program.get("program_id", "")
        ).strip()
        campuses: list[str] = self._normalize_campuses(program)
        parent_campuses: str = ", ".join(campuses) if campuses else "Online / General Catalog"

        course_code: str = str(course.get("code", "")).strip()
        course_title: str = str(course.get("title", "")).strip()
        credits: str = str(course.get("credits", "")).strip()
        verification_url: str = self._build_course_catalog_url(course_code) if course_code else ""
        course_content: str = (
            f"Semester: {semester_name}. Course Code: {course_code}. "
            f"Title: {course_title}. Credits: {credits}."
        )
        if verification_url:
            course_content = (
                f"{course_content} Verification: [Course Verification Link for {course_code}: {verification_url}]"
            )

        return (
            f"PROGRAM: {parent_program_title}\n"
            f"CAMPUSES: {parent_campuses}\n"
            f"COURSE DETAILS: {course_content}\n"
            "---\n"
        )

    def _extract_completed_courses_from_query(self, user_query: str) -> list[str]:
        return self._extract_course_codes(user_query)

    def _extract_prerequisite_codes(self, course: dict[str, object]) -> list[str]:
        prerequisite_sources: list[str] = []
        for field_name in ("prerequisite", "prerequisites", "notes", "description"):
            value: object = course.get(field_name)
            if isinstance(value, str) and "prereq" in value.lower():
                prerequisite_sources.append(value)

        embedded_list: object = course.get("prerequisite_courses")
        if isinstance(embedded_list, list):
            for item in embedded_list:
                if isinstance(item, str):
                    prerequisite_sources.append(item)

        prerequisite_codes: set[str] = set()
        for source in prerequisite_sources:
            prerequisite_codes.update(self._extract_course_codes(source))

        return sorted(prerequisite_codes)

    def _build_prerequisite_index(self) -> dict[str, dict[str, list[str]]]:
        index: dict[str, dict[str, list[str]]] = {}
        for program in self._programs():
            try:
                program_id: str = str(program.get("program_id", "unknown_program"))
                program_index: dict[str, list[str]] = {}
                semesters: object = program.get("semesters")
                if isinstance(semesters, list):
                    for semester in semesters:
                        if not isinstance(semester, dict):
                            continue
                        courses: object = semester.get("courses")
                        if not isinstance(courses, list):
                            continue
                        for course in courses:
                            if not isinstance(course, dict):
                                continue
                            course_code: str = str(course.get("code", "")).strip()
                            if not course_code:
                                continue
                            prerequisites: list[str] = self._extract_prerequisite_codes(course)
                            if prerequisites:
                                program_index[course_code] = prerequisites
                index[program_id] = program_index
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("Skipping malformed program prerequisite block: %s", exc)
                continue

        return index

    def _get_prerequisite_index(self) -> dict[str, dict[str, list[str]]]:
        if self._prerequisite_index_cache is None:
            self._prerequisite_index_cache = self._build_prerequisite_index()
        return self._prerequisite_index_cache

    def get_missing_prerequisites(
        self,
        completed_courses: list[str],
        target_program: str,
    ) -> dict[str, list[str]]:
        completed_set: set[str] = {code.upper().strip() for code in completed_courses}
        program_dependencies: dict[str, list[str]] = self._get_prerequisite_index().get(
            target_program,
            {},
        )

        missing_map: dict[str, list[str]] = {}
        for course_code, dependencies in program_dependencies.items():
            unmet: list[str] = [
                dependency for dependency in dependencies if dependency.upper() not in completed_set
            ]
            if unmet:
                missing_map[course_code] = unmet

        return missing_map

    def get_degree_progress_cards(self, user_query: str) -> list[dict[str, object]]:
        if not self._is_degree_layout_request(user_query):
            return []

        program_id: str | None = self._classify_program_intent(user_query)
        if program_id is None:
            return []

        for program in self._programs():
            if str(program.get("program_id")) != program_id:
                continue

            card_payload: dict[str, object] = {
                "program_id": program_id,
                "title": str(program.get("title", program_id)),
                "courses": [],
            }
            courses_payload: object = card_payload.get("courses")
            if not isinstance(courses_payload, list):
                return []

            semesters: object = program.get("semesters")
            if isinstance(semesters, list):
                for semester in semesters:
                    if not isinstance(semester, dict):
                        continue
                    semester_name: str = str(semester.get("name", "Requirements"))
                    courses: object = semester.get("courses")
                    if not isinstance(courses, list):
                        continue

                    for course in courses:
                        if not isinstance(course, dict):
                            continue
                        courses_payload.append(
                            {
                                "semester": semester_name,
                                "code": course.get("code"),
                                "title": course.get("title"),
                                "credits": course.get("credits"),
                                "completed": False,
                            }
                        )

            return [card_payload]

        return []

    def get_degree_prerequisite_tree(self, user_query: str) -> dict[str, list[str]]:
        if not self._is_degree_layout_request(user_query):
            return {}

        program_id: str | None = self._classify_program_intent(user_query)
        if program_id is None:
            return {}

        completed_courses: list[str] = self._extract_completed_courses_from_query(user_query)
        return self.get_missing_prerequisites(completed_courses, program_id)

    def _json_within_budget(self, payload: dict[str, object]) -> str:
        serialized: str = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) <= self.char_budget:
            return serialized

        overflow_payload: dict[str, object] = {
            "mode": payload.get("mode"),
            "truncated": True,
            "note": "Context budget reached. Ask for a narrower degree plan or pathway.",
        }
        overflow_serialized: str = json.dumps(
            overflow_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(overflow_serialized) <= self.char_budget:
            return overflow_serialized

        return overflow_serialized[: self.char_budget]

    def _resolve_program_catalog_source_url(self, program: dict[str, object]) -> str:
        for key in ("source_url", "catalog_url", "source", "url", "link"):
            raw_value: object = program.get(key)
            if isinstance(raw_value, str) and raw_value.strip().startswith("http"):
                return raw_value.strip()

        raw_title: object = program.get("title", "")
        program_title: str = raw_title.strip() if isinstance(raw_title, str) else ""
        if not program_title:
            raw_program_id: object = program.get("program_id", "program")
            program_title = (
                str(raw_program_id).replace("_", " ").strip() if raw_program_id is not None else "program"
            )
        return self._build_program_catalog_url(program_title)

    def _append_catalog_source_token(self, chunk_text: str, source_url: str) -> str:
        token: str = f"\n[Catalog Source Verification Link: {source_url}]"
        if len(chunk_text) + len(token) <= self.char_budget:
            return f"{chunk_text}{token}"

        remaining_budget: int = self.char_budget - len(token)
        if remaining_budget > 0:
            return f"{chunk_text[:remaining_budget]}{token}"
        return token[: self.char_budget]

    def _build_targeted_program_context(
        self,
        program: dict[str, object],
        program_id: str,
        source_url: str,
    ) -> str:
        targeted_payload: dict[str, object] = {
            "mode": "targeted_program",
            "program_id": program_id,
            "citations": {
                "program": {
                    "token": f"[Catalog Source Verification Link: {source_url}]",
                    "url": source_url,
                },
                "courses": [],
            },
            "program": {
                "program_id": program_id,
                "title": program.get("title"),
                "degree_code": program.get("degree_code"),
                "total_hours": program.get("total_hours"),
                "semesters": [],
            },
        }

        semesters: object = program.get("semesters")
        if not isinstance(semesters, list):
            return self._append_catalog_source_token(
                self._json_within_budget(targeted_payload),
                source_url,
            )

        serialized: str = self._json_within_budget(targeted_payload)
        program_payload_obj: object = targeted_payload.get("program")
        if not isinstance(program_payload_obj, dict):
            return serialized
        program_payload: dict[str, object] = program_payload_obj
        if not isinstance(program_payload, dict):
            return serialized

        semester_list: object = program_payload.get("semesters")
        if not isinstance(semester_list, list):
            return serialized

        citations_obj: object = targeted_payload.get("citations")
        course_citations: list[dict[str, str]] = []
        if isinstance(citations_obj, dict):
            citations_courses_obj: object = citations_obj.get("courses")
            if isinstance(citations_courses_obj, list):
                course_citations = citations_courses_obj  # type: ignore[assignment]
        seen_course_citation_codes: set[str] = set()

        for semester in semesters:
            try:
                if not isinstance(semester, dict):
                    continue

                compact_semester: dict[str, object] = {
                    "name": semester.get("name"),
                    "courses": [],
                }
                semester_list.append(compact_semester)

                courses_container: object = compact_semester.get("courses")
                if not isinstance(courses_container, list):
                    continue

                courses: object = semester.get("courses")
                if isinstance(courses, list):
                    for course in courses:
                        try:
                            if not isinstance(course, dict):
                                continue

                            compact_course: dict[str, object] = {
                                "code": course.get("code"),
                                "title": course.get("title"),
                                "credits": course.get("credits"),
                            }
                            compact_course["context_fragment"] = self._build_fragment_header(
                                program,
                                (
                                    f"Program: {str(program.get('title', program_id)).strip()} ({program_id}). "
                                    f"Offered at Campuses: {', '.join(self._normalize_campuses(program)) or 'Online / General Catalog'}. "
                                    f"Semester: {semester.get('name')}. Course: {course.get('code')} - {course.get('title')}"
                                ),
                            )
                            course_code: str = str(compact_course.get("code", "")).strip()
                            if course_code:
                                course_lookup_url: str = self._build_course_catalog_url(course_code)
                                compact_course["verification_token"] = (
                                    f"[Course Verification Link for {course_code}: {course_lookup_url}]"
                                )
                                if course_code not in seen_course_citation_codes and course_citations is not None:
                                    course_citations.append(
                                        {
                                            "course_code": course_code,
                                            "token": (
                                                f"[Course Verification Link for {course_code}: "
                                                f"{course_lookup_url}]"
                                            ),
                                            "url": course_lookup_url,
                                        }
                                    )
                                    seen_course_citation_codes.add(course_code)
                            courses_container.append(compact_course)

                            serialized = self._json_within_budget(targeted_payload)
                            if len(serialized) >= self.char_budget:
                                return serialized
                        except Exception as exc:  # noqa: BLE001
                            _LOG.warning("Skipping malformed course in targeted context: %s", exc)
                            continue
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("Skipping malformed semester in targeted context: %s", exc)
                continue

            serialized = self._json_within_budget(targeted_payload)
            if len(serialized) >= self.char_budget:
                return serialized

        return self._append_catalog_source_token(serialized, source_url)

    def _build_generic_index_context(self) -> str:
        index_payload: dict[str, object] = {"mode": "catalog_index_signature", "signature": ""}
        signature_entries: list[str] = []
        if not isinstance(index_payload.get("signature"), str):
            return self._json_within_budget(index_payload)

        seen_entries: set[tuple[str, str]] = set()
        seen_program_source_tokens: set[str] = set()

        for program in self._programs():
            try:
                program_id: str = str(program.get("program_id", "unknown_program"))
                source_url: str = self._resolve_program_catalog_source_url(program)
                if program_id not in seen_program_source_tokens:
                    signature_entries.append(
                        f"{program_id}|[Catalog Source Verification Link: {source_url}]"
                    )
                    seen_program_source_tokens.add(program_id)
                    index_payload["signature"] = ";".join(signature_entries)
                    serialized = self._json_within_budget(index_payload)
                    if len(serialized) >= self.char_budget:
                        return serialized

                semesters: object = program.get("semesters")
                if not isinstance(semesters, list):
                    continue

                for semester in semesters:
                    try:
                        if not isinstance(semester, dict):
                            continue
                        courses: object = semester.get("courses")
                        if not isinstance(courses, list):
                            continue

                        for course in courses:
                            try:
                                if not isinstance(course, dict):
                                    continue

                                raw_code: object = course.get("code", "")
                                code: str = raw_code.strip() if isinstance(raw_code, str) else ""

                                raw_title: object = course.get("title", "")
                                title: str = raw_title.strip() if isinstance(raw_title, str) else ""
                                if not code and not title:
                                    continue

                                dedupe_key: tuple[str, str] = (code, title)
                                if dedupe_key in seen_entries:
                                    continue
                                seen_entries.add(dedupe_key)

                                raw_credits: object = course.get("credits")
                                credits: str | int | float | None = (
                                    raw_credits
                                    if isinstance(raw_credits, (str, int, float))
                                    else None
                                )
                                normalized_title: str = re.sub(r"\s+", " ", title).strip()
                                course_lookup_url: str = self._build_course_catalog_url(code)
                                signature_entries.append(
                                    self._build_fragment_header(
                                        program,
                                        (
                                            f"Program: {title} ({program_id}). "
                                            f"Offered at Campuses: {', '.join(self._normalize_campuses(program)) or 'Online / General Catalog'}. "
                                            f"Course Code: {code}. Title: {normalized_title}. Credits: {credits}. "
                                            f"Verification: [Course Verification Link for {code}: {course_lookup_url}]"
                                        ),
                                    ).rstrip()
                                )
                                index_payload["signature"] = ";".join(signature_entries)

                                serialized: str = self._json_within_budget(index_payload)
                                if len(serialized) >= self.char_budget:
                                    return serialized
                            except Exception as exc:  # noqa: BLE001
                                _LOG.warning("Skipping malformed course in generic context: %s", exc)
                                continue
                    except Exception as exc:  # noqa: BLE001
                        _LOG.warning("Skipping malformed semester in generic context: %s", exc)
                        continue
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("Skipping malformed program in generic context: %s", exc)
                continue

        return self._json_within_budget(index_payload)

    def get_optimized_context(self, user_query: str) -> str:
        query_lower: str = user_query.lower()
        supplemental_program_titles: list[str] = self._resolve_semantic_program_titles(user_query)
        if "nurse" in query_lower or "nursing" in query_lower:
            for program in self._programs():
                program_id: str = str(program.get("program_id", "")).strip()
                program_title: str = str(program.get("title", "")).strip()
                if program_id != "Associate_Degree_Nursing_A_A_S" and "associate degree nursing" not in program_title.lower():
                    continue

                current_source_url: str = self._resolve_program_catalog_source_url(program)
                campuses_raw: object = program.get("campuses", [])
                campuses: list[str] = [
                    str(campus).strip()
                    for campus in campuses_raw
                    if isinstance(campuses_raw, list) and str(campus).strip()
                ] if isinstance(campuses_raw, list) else []
                campus_string: str = ", ".join(campuses) if campuses else "Location data unavailable"
                direct_location_context: str = self._json_within_budget(
                    {
                        "mode": "program_location_direct",
                        "program_id": program_id,
                        "title": program_title,
                        "Offered at Campuses": campus_string,
                        "source_url": current_source_url,
                    }
                )
                vector_context: str = self._build_targeted_program_context(
                    program,
                    program_id,
                    current_source_url,
                )
                combined_context: str = f"{direct_location_context}\n{vector_context}"
                valid_codes: list[str] = self._collect_valid_course_codes([program])
                return self._append_verified_whitelist_line(combined_context, valid_codes)

        matched_program_id: str | None = self._classify_program_intent(user_query)
        if matched_program_id is not None:
            for program in self._programs():
                if str(program.get("program_id")) == matched_program_id:
                    current_program_title: str = str(program.get("title", matched_program_id)).strip()
                    current_source_url: str = self._resolve_program_catalog_source_url(program)
                    _ = (current_program_title, current_source_url)
                    context_chunk: str = self._build_targeted_program_context(
                        program,
                        matched_program_id,
                        current_source_url,
                    )
                    valid_codes: list[str] = self._collect_valid_course_codes([program])
                    return self._append_verified_whitelist_line(context_chunk, valid_codes)

        query_course_headers: list[str] = self._extract_course_headers_from_query(user_query)
        if query_course_headers:
            query_course_codes: set[str] = {header.lower() for header in query_course_headers}
            course_matches: list[dict[str, object]] = self._find_program_course_matches(query_course_codes)
            if course_matches:
                fragments: list[str] = []
                matched_programs: list[dict[str, object]] = []
                seen_program_ids: set[str] = set()

                for match in course_matches:
                    program_obj: object = match.get("program")
                    course_obj: object = match.get("course")
                    semester_name: str = str(match.get("semester_name", "Requirements")).strip() or "Requirements"
                    if not isinstance(program_obj, dict) or not isinstance(course_obj, dict):
                        continue

                    fragments.append(
                        self._build_rehydrated_course_fragment(
                            program=program_obj,
                            semester_name=semester_name,
                            course=course_obj,
                        )
                    )

                    program_id: str = str(program_obj.get("program_id", "")).strip()
                    if program_id and program_id not in seen_program_ids:
                        seen_program_ids.add(program_id)
                        matched_programs.append(program_obj)

                if fragments:
                    context_chunk = "".join(fragments)
                    valid_codes = self._collect_valid_course_codes(matched_programs)
                    return self._append_verified_whitelist_line(context_chunk, valid_codes)

        matched_program_titles: list[str] = self._lookup_program_titles_from_query(user_query)
        if supplemental_program_titles:
            matched_program_titles = list(
                dict.fromkeys(matched_program_titles + supplemental_program_titles)
            )
        if matched_program_titles:
            matched_program_title_set: set[str] = {title.lower() for title in matched_program_titles}
            for program in self._programs():
                raw_title: object = program.get("title", "")
                program_title: str = raw_title.strip() if isinstance(raw_title, str) else ""
                if program_title.lower() in matched_program_title_set:
                    program_id = str(program.get("program_id", "")).strip()
                    current_source_url: str = self._resolve_program_catalog_source_url(program)
                    context_chunk = self._build_targeted_program_context(
                        program,
                        program_id,
                        current_source_url,
                    )
                    valid_codes = self._collect_valid_course_codes([program])
                    return self._append_verified_whitelist_line(context_chunk, valid_codes)

        expanded_terms: list[str] = expand_user_query(user_query)
        for program in self._programs():
            try:
                program_id: str = str(program.get("program_id", "")).strip()
                program_title: str = str(program.get("title", "")).strip()
                normalized_program_id: str = program_id.lower()
                normalized_program_title: str = program_title.lower()

                if any(
                    term in normalized_program_title or term in normalized_program_id
                    for term in expanded_terms
                ):
                    current_program_title: str = program_title
                    current_source_url: str = self._resolve_program_catalog_source_url(program)
                    _ = (current_program_title, current_source_url)
                    context_chunk = self._build_targeted_program_context(
                        program,
                        program_id,
                        current_source_url,
                    )
                    valid_codes = self._collect_valid_course_codes([program])
                    return self._append_verified_whitelist_line(context_chunk, valid_codes)

                semesters: object = program.get("semesters")
                if isinstance(semesters, list):
                    for semester in semesters:
                        try:
                            if not isinstance(semester, dict):
                                continue
                            courses: object = semester.get("courses")
                            if not isinstance(courses, list):
                                continue
                            for course in courses:
                                if not isinstance(course, dict):
                                    continue
                                raw_course_code: object = course.get("code", "")
                                normalized_course_code: str = (
                                    raw_course_code.lower().strip()
                                    if isinstance(raw_course_code, str)
                                    else ""
                                )
                                if any(term in normalized_course_code for term in expanded_terms):
                                    context_chunk = self._build_rehydrated_course_fragment(
                                        program=program,
                                        semester_name=str(semester.get("name", "Requirements")).strip()
                                        or "Requirements",
                                        course=course,
                                    )
                                    valid_codes = self._collect_valid_course_codes([program])
                                    return self._append_verified_whitelist_line(
                                        context_chunk,
                                        valid_codes,
                                    )
                        except Exception as exc:  # noqa: BLE001
                            _LOG.warning(
                                "Skipping malformed semester in expanded match loop: %s",
                                exc,
                            )
                            continue
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("Skipping malformed program in expanded match loop: %s", exc)
                continue

        context_chunk = self._build_generic_index_context()
        valid_codes = self._collect_valid_course_codes(self._programs())
        return self._append_verified_whitelist_line(context_chunk, valid_codes)


_CATALOG_SEARCH_ENGINE: CatalogSearchEngine | None = None


def _get_catalog_search_engine() -> CatalogSearchEngine:
    global _CATALOG_SEARCH_ENGINE
    if _CATALOG_SEARCH_ENGINE is None:
        _CATALOG_SEARCH_ENGINE = CatalogSearchEngine(
            cache_path=_CATALOG_CACHE_PATH,
            char_budget=_CONTEXT_CHAR_BUDGET,
        )
    return _CATALOG_SEARCH_ENGINE


def _get_optimized_catalog_context(user_query: str) -> str:
    try:
        return _get_catalog_search_engine().get_optimized_context(user_query)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


def _is_out_of_bounds_query(user_query: str) -> bool:
    return _get_catalog_search_engine().is_out_of_bounds_query(user_query)


def _get_degree_progress_cards(user_query: str) -> list[dict[str, object]]:
    return _get_catalog_search_engine().get_degree_progress_cards(user_query)


def _get_degree_prerequisite_tree(user_query: str) -> dict[str, list[str]]:
    return _get_catalog_search_engine().get_degree_prerequisite_tree(user_query)


def _write_analytics_entry_sync(entry: dict[str, object], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    existing_entries: list[dict[str, object]] = []
    if output_path.exists():
        try:
            payload: object = json.loads(output_path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                existing_entries = [item for item in payload if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            existing_entries = []

    existing_entries.append(entry)
    output_path.write_text(
        json.dumps(existing_entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def log_analytics_event(query: str, intent: str, triggered_guardrail: bool) -> None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return

    matched_keywords: list[str] = _get_catalog_search_engine().get_matched_keywords(query)
    entry: dict[str, object] = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "intent": intent,
        "matched_keywords": matched_keywords,
        "triggered_guardrail": triggered_guardrail,
    }

    await asyncio.to_thread(_write_analytics_entry_sync, entry, _ANALYTICS_LOG_PATH)


def _parse_cors_origins() -> list[str]:
    raw_origins: str = os.environ.get("CORS_ALLOWED_ORIGINS", "*")
    origins: list[str] = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    return origins or ["*"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield


app = FastAPI(lifespan=lifespan)

_CORS_ORIGINS: list[str] = _parse_cors_origins()
_ALLOW_CREDENTIALS: bool = _CORS_ORIGINS != ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    message: Annotated[str, Field(min_length=1, max_length=1000)] = Field(
        ...,
        description="End-user question constrained to 1-1000 characters.",
    )


class ChatResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    reply: str = Field(..., description="Plain-text grounded response returned to the widget.")
    model: str = Field(..., description="Provider/model pair used to generate the response.")
    progress_cards: list[dict[str, object]] | None = Field(
        default=None,
        description="Optional structured progress-card payload for interactive checklist rendering.",
    )
    prerequisite_tree: dict[str, list[str]] | None = Field(
        default=None,
        description="Optional prerequisite dependency map for checklist constraints.",
    )


class GeminiPart(BaseModel):
    model_config = ConfigDict(frozen=True)
    text: str = Field(..., description="Text payload for the Gemini API part.")


class GeminiContent(BaseModel):
    model_config = ConfigDict(frozen=True)
    role: str = Field(..., description="Gemini role label such as 'user'.")
    parts: list[GeminiPart] = Field(..., description="Ordered text parts attached to the role.")


class GeminiRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    system_instruction: GeminiContent = Field(
        ...,
        description="Shared system-level instruction block.",
    )
    contents: list[GeminiContent] = Field(
        ...,
        description="User message content blocks.",
    )
    generation_config: dict[str, float | int] = Field(
        ...,
        description="Deterministic generation configuration.",
    )


class GroqMessage(BaseModel):
    model_config = ConfigDict(frozen=True)
    role: str = Field(..., description="Chat role such as 'system' or 'user'.")
    content: str = Field(..., description="Plain-text message content.")


class GroqRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    model: str = Field(..., description="Groq-hosted model name.")
    messages: list[GroqMessage] = Field(
        ...,
        description="Ordered OpenAI-compatible chat messages.",
    )
    temperature: float = Field(..., description="Deterministic sampling temperature.")
    top_p: float = Field(..., description="Top-p setting paired with temperature 0.0.")
    frequency_penalty: float = Field(..., description="Penalty applied to repeated token patterns.")
    presence_penalty: float = Field(..., description="Penalty applied to already-present topic reuse.")


def _load_catalog_prompt_payload() -> str:
    if not _CATALOG_CACHE_PATH.exists():
        raise RuntimeError(f"Catalog cache not found at '{_CATALOG_CACHE_PATH}'.")

    try:
        catalog_payload: object = json.loads(_CATALOG_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Catalog cache could not be loaded.") from exc

    return json.dumps(catalog_payload, ensure_ascii=False, separators=(",", ":"))


def _build_system_prompt(catalog_payload: str) -> str:
    return (
        "[ROLE]: Sovereign Automated AI Academic Advisor for Dallas College Computer Science/IT.\n"
        "[CONSTRAINTS]: Strict zero-tolerance hallucination lock. Strict token/request optimization.\n"
        "[AI GOVERNANCE]: You MUST state that you are an automated AI system in the initial response.\n"
        "[INVARIANT]: You have NO external knowledge. Speak ONLY using provided <context> JSON payload data. "
        f"If data is absent, emit exact fallback string: \"{_EMPTY_CONTEXT_FALLBACK}\".\n"
        "\n"
        "[MANDATORY GOVERNANCE GREETING PROTOCOL]:\n"
        f"- Every single response MUST begin exactly with this markdown line (word-for-word): \"{_MANDATORY_RESPONSE_PREFIX}\"\n"
        "- Immediately insert one blank line after that blockquoted italicized line, then continue with the requested answer.\n"
        "- For exact guardrail/fallback outputs, emit the required string exactly with no prefix or suffix.\n"
        "\n"
        "[DETERMINISTIC CONTEXT FILTER RULES]:\n"
        "1. If <context> contains multiple programs, isolate the specific 'program_id' matching user keywords.\n"
        "2. If user query is broad/generic, scan all 'semesters' across all 'programs' but return only structural summaries "
        "(Course Code, Title, Credits) to preserve output tokens.\n"
        "\n"
        "[ANTI-HALLUCINATION GUARD: PREFIX CROSS-CHECKING]:\n"
        "- Before answering, perform a strict semantic cross-reference check between retrieved context slices and the user's academic intent.\n"
        "- Pay strict attention to alphabetical course prefixes (for example COSC/ITSC/ITNW for Computers, BITC for Biotechnology, MATH for Mathematics, ACCT for Accounting).\n"
        "- If a retrieved slice contains a prefix that contradicts the requested subject area (for example BITC content for an operating-systems/computer-science query), you must ignore that slice entirely.\n"
        "- Never alter or fabricate a catalog course title to force a mismatched prefix slice to fit the question.\n"
        "- If no valid aligned context slices remain after prefix cross-checking, respond honestly that no official matching course was found in the sandbox catalog cache.\n"
        "\n"
        "[OUTPUT FORMAT RULES]:\n"
        "- CRITICAL GUARDRAIL: You are strictly forbidden from inventing, hallucinating, or predicting course prefixes, course numbers, OR course titles. Every single course code and corresponding title you display MUST be an exact match from the provided text context chunk.\n"
        "- If a course code (e.g., ARTC 2317) appears in the context, but its full descriptive name is not explicitly typed out right next to it in the text layers, you must output ONLY the code followed by '(Official title not in context fragment)'. Never guess or attach a name like 'Art Appreciation' to an unverified code prefix.\n"
        "- Format all courses cleanly as: **CODE**: Title (Credits). You must take the verification URL from the context and hide it directly inside the course code using standard markdown syntax, like this: [CODE](URL). Never output 'Course Verification Link' as plain text on a new line.\n"
        "- If a student asks about campus locations, geographic availability, or where a program is offered, parse the provided text context chunk for 'Offered at Campuses:'. Extract those exact physical campus locations and explicitly state them in your reply. If the context chunk does not list any campuses for that program, politely state that location availability is missing from the current fragment.\n"
        "- If a user asks about a specific skill or course topic (e.g., video editing, audio engineering, welding) and you locate a matching context fragment, always inspect the 'CAMPUSES:' metadata attached to that fragment's header. Confidently state those campuses as the location where the program/topic is offered, even if the overall program name uses broader terminology than the user's exact keyword.\n"
        "\n"
        "[RESPONSE COMPRESSION PROTOCOL]:\n"
        "- No conversational pleasantries.\n"
        "- Do not repeat or restate the user's question.\n"
        "- Use dense markdown bullet structures for course maps.\n"
        "- Format all courses as: **CODE**: Title (Credits).\n"
        "- When displaying individual courses, extract the corresponding '[Course Verification Link for ...]' token from the context chunk and embed it directly as an inline hyperlink on the course code itself (e.g., [CHEF 1301](url)).\n"
        "- If a course item in the context data list specifies items within a 'prerequisites' array, draw out a clear, structured sequence flow using text connectors (──>) to indicate the mandatory foundational track before displaying advanced classes.\n"
        "- Strict zero-temperature simulation: Do not vary terminology.\n"
        "- You have been provided a highly filtered context snippet matching the student's topical intent. "
        "If the precise answer is missing from this slice, guide them to specify which degree plan or certificate pathway they want to inspect.\n"
        "\n"
        "[SOURCE CITATION VERIFICATION RULES]:\n"
        "- When rendering a program data card, look for the '[Catalog Source Verification Link: ...]' token embedded inside the provided text context chunk.\n"
        "- Never show raw token text or raw verification URLs in normal paragraph/bullet prose.\n"
        "- Use compact inline footnote hyperlinks only: attach sequential clickable references directly beside the related program/path term as [1](URL), [2](URL), etc.\n"
        "- CRITICAL FORMAT RULE: Footnote references must use exact markdown link syntax with no spaces or extra characters between the bracket and parentheses: [1](URL). Never emit '[1] URL'.\n"
        "- Do not output a bottom 'Sources' or 'References' section; keep citations inline only to save widget scroll space.\n"
        "- Keep response copy dense and direct with no introductory fluff.\n"
        "\n"
        "[GUARDRAIL TRIGGER ACTIONS]:\n"
        "- If the user requests courses/tracks not explicitly keyed in <context>, emit exactly:\n"
        f"  \"{_SELECTION_GUARDRAIL}\"\n"
        "- If the <context> block is empty (\"[]\"), emit exactly:\n"
        f"  \"{_EMPTY_CONTEXT_FALLBACK}\"\n"
        "\n"
        "[INPUT DATA ENVIRONMENT]:\n"
        "<context>\n"
        f"{catalog_payload}\n"
        "</context>\n"
        "\n"
        "[USER QUERY]: Provided in the user message."
    )


def _load_groq_api_keys() -> list[str]:
    raw_keys: str = os.environ.get("GROQ_API_KEYS") or os.environ.get("GROQ_API_KEY") or ""
    return [key.strip() for key in raw_keys.split(",") if key.strip()]


def _load_gemini_api_keys() -> list[str]:
    raw_keys: str = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY") or ""
    return [key.strip() for key in raw_keys.split(",") if key.strip()]


def _build_gemini_request(message: str, catalog_payload: str) -> GeminiRequest:
    return GeminiRequest(
        system_instruction=GeminiContent(
            role="system",
            parts=[GeminiPart(text=_build_system_prompt(catalog_payload))],
        ),
        contents=[
            GeminiContent(
                role="user",
                parts=[GeminiPart(text=message)],
            )
        ],
        generation_config={
            "temperature": 0.3,
            "frequencyPenalty": 0.7,
            "presencePenalty": 0.5,
            "topP": 1.0,
            "maxOutputTokens": 512,
        },
    )


def _build_groq_request(message: str, catalog_payload: str) -> GroqRequest:
    return GroqRequest(
        model=_GROQ_MODEL_NAME,
        messages=[
            GroqMessage(role="system", content=_build_system_prompt(catalog_payload)),
            GroqMessage(role="user", content=message),
        ],
        temperature=0.3,
        top_p=1.0,
        frequency_penalty=0.7,
        presence_penalty=0.5,
    )


def _extract_gemini_text(response_payload: dict[str, object]) -> str:
    candidates: object = response_payload.get("candidates")
    if not isinstance(candidates, list):
        raise RuntimeError("Gemini response did not include candidates.")

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content: object = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts: object = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            text: object = part.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()

    raise RuntimeError("Gemini response did not include a text part.")


def _extract_groq_text(response_payload: dict[str, object]) -> str:
    choices: object = response_payload.get("choices")
    if not isinstance(choices, list):
        raise RuntimeError("Groq response did not include choices.")

    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message_payload: object = choice.get("message")
        if not isinstance(message_payload, dict):
            continue
        content: object = message_payload.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

    raise RuntimeError("Groq response did not include a message content string.")


async def _request_groq_reply(
    client: HttpxAsyncClient,
    message: str,
    catalog_payload: str,
) -> ChatResponse | None:
    api_keys: list[str] = _load_groq_api_keys()
    if not api_keys:
        return None

    request_payload: GroqRequest = _build_groq_request(
        message=message,
        catalog_payload=catalog_payload,
    )

    for key_index, api_key in enumerate(api_keys, start=1):
        response: HttpxResponse = await client.post(
            _GROQ_CHAT_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=request_payload.model_dump(mode="json"),
        )

        if response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            _LOG.warning(
                "Groq key %d/%d hit HTTP 429; rotating to the next key.",
                key_index,
                len(api_keys),
            )
            continue

        if response.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            _LOG.warning(
                "Groq key %d/%d returned upstream status %d; trying the next key or fallback.",
                key_index,
                len(api_keys),
                response.status_code,
            )
            continue

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Groq request failed with status {response.status_code}.",
            ) from exc

        try:
            response_json: dict[str, object] = response.json()
            reply_text: str = _extract_groq_text(response_json)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Groq returned an invalid response payload.",
            ) from exc

        return ChatResponse(reply=reply_text, model=f"groq/{_GROQ_MODEL_NAME}")

    return None


async def _request_gemini_reply(
    client: HttpxAsyncClient,
    message: str,
    catalog_payload: str,
) -> ChatResponse:
    api_keys: list[str] = _load_gemini_api_keys()
    if not api_keys:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No usable provider keys are configured. Set GROQ_API_KEYS or GEMINI_API_KEYS.",
        )

    request_payload: GeminiRequest = _build_gemini_request(
        message=message,
        catalog_payload=catalog_payload,
    )

    for key_index, api_key in enumerate(api_keys, start=1):
        request_url: str = f"{_GEMINI_GENERATE_URL}?key={api_key}"
        response: HttpxResponse = await client.post(
            request_url,
            json=request_payload.model_dump(mode="json"),
        )

        if response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            _LOG.warning(
                "Gemini key %d/%d hit HTTP 429; rotating to the next key.",
                key_index,
                len(api_keys),
            )
            continue

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Gemini request failed with status {response.status_code}.",
            ) from exc

        try:
            response_json: dict[str, object] = response.json()
            reply_text: str = _extract_gemini_text(response_json)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Gemini returned an invalid response payload.",
            ) from exc

        return ChatResponse(reply=reply_text, model=f"gemini/{_GEMINI_MODEL_NAME}")

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="All configured provider keys are rate-limited. Try again shortly.",
    )


async def _generate_chat_reply(message: str) -> ChatResponse:
    normalized_message = message.lower().strip()
    if any(keyword in normalized_message for keyword in _EMERGENCY_KEYWORDS):
        return ChatResponse(
            reply=_EMERGENCY_REPLY,
            model="guardrail/emergency",
            progress_cards=None,
            prerequisite_tree=None,
        )

    intent: str = _get_catalog_search_engine().classify_intent(message)

    if intent == "OUT_OF_BOUNDS":
        await log_analytics_event(
            query=message,
            intent=intent,
            triggered_guardrail=True,
        )
        return ChatResponse(
            reply=_OUT_OF_BOUNDS_REPLY,
            model="guardrail/local",
            progress_cards=None,
            prerequisite_tree=None,
        )

    degree_progress_cards: list[dict[str, object]] = _get_degree_progress_cards(message)
    if degree_progress_cards:
        prerequisite_tree: dict[str, list[str]] = _get_degree_prerequisite_tree(message)
        await log_analytics_event(
            query=message,
            intent="DEGREE_LAYOUT",
            triggered_guardrail=False,
        )
        return ChatResponse(
            reply=(
                "Degree progress checklist generated from your requested pathway. "
                "Mark completed courses to track remaining requirements."
            ),
            model="catalog/local",
            progress_cards=degree_progress_cards,
            prerequisite_tree=prerequisite_tree,
        )

    catalog_payload: str = _get_optimized_catalog_context(message)
    await log_analytics_event(
        query=message,
        intent=intent,
        triggered_guardrail=False,
    )

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        groq_response: ChatResponse | None = await _request_groq_reply(
            client=client,
            message=message,
            catalog_payload=catalog_payload,
        )
        if groq_response is not None:
            return groq_response

        return await _request_gemini_reply(
            client=client,
            message=message,
            catalog_payload=catalog_payload,
        )


@app.get("/api/health", status_code=status.HTTP_200_OK)
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/test-routing", status_code=status.HTTP_200_OK)
async def test_routing() -> dict[str, str]:
    return {"message": "Uvicorn is successfully serving api/chat.py"}


@app.post("/api/chat/", response_model=ChatResponse, status_code=status.HTTP_200_OK)
@app.post("/api/chat", response_model=ChatResponse, status_code=status.HTTP_200_OK)
async def chat(request: ChatRequest) -> ChatResponse:
    try:
        return await _generate_chat_reply(message=request.message)
    except HTTPException as http_exc:
        logging.error(f"Upstream provider connection error: {http_exc.detail}")
        return ChatResponse(
            reply=f"System Connection Failure: {http_exc.detail} Please check your live environment credentials.",
            model="System-Error-Shield",
            progress_cards=[],
        )
    except Exception as e:  # noqa: BLE001
        logging.critical(f"Unhandled operational backend panic: {str(e)}", exc_info=True)
        return ChatResponse(
            reply="The server encountered an operational anomaly parsing this catalog slice. Please try your request again shortly.",
            model="System-Fallback-Shield",
            progress_cards=[],
        )