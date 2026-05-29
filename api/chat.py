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
from datetime import datetime, timezone
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

_LOG: logging.Logger = logging.getLogger(__name__)

_APP_TITLE: str = "Dallas College Chatbot API"
_APP_VERSION: str = "0.3.0"
_CATALOG_CACHE_PATH: Path = Path(__file__).resolve().parent.parent / "data" / "catalog_mvp.json"
_ANALYTICS_LOG_PATH: Path = Path(__file__).resolve().parent.parent / "data" / "analytics_logs.json"
_HTTP_TIMEOUT_SECONDS: float = 30.0
_CONTEXT_CHAR_BUDGET: int = 6000

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
    "Greetings, I am the automated AI Academic Advisor for Dallas College."
)
_OUT_OF_BOUNDS_REPLY: str = (
    "I can only assist with Dallas College academic advising topics. "
    "Please ask about degree plans, certificates, pathways, or course requirements."
)
_COURSE_CODE_PATTERN: re.Pattern[str] = re.compile(r"\b([A-Z]{4})\s*(\d{4})\b", re.IGNORECASE)


class CatalogSearchEngine:
    """In-memory catalog indexer and context slicer for token-efficient prompts.

    Architectural Intent:
        Prevents token bloat by routing each user query to a tightly scoped
        context slice. Program-specific queries receive one pathway payload,
        while broad queries receive a compact index map only.

    Security Rationale:
        The engine reads only from the local cache file and returns bounded
        strings under an explicit character budget to reduce prompt-surface
        risk and downstream request size.
    """

    def __init__(self, cache_path: Path, char_budget: int = _CONTEXT_CHAR_BUDGET) -> None:
        """Initialize and load catalog data into memory.

        Args:
            cache_path: Local catalog cache file path.
            char_budget: Maximum character budget for returned context strings.
        """
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
        """Load the local catalog cache into a dictionary."""
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
        """Return the normalized list of program objects from cache payload."""
        if self._programs_cache is not None:
            return self._programs_cache

        programs: object = self._catalog_payload.get("programs")
        if not isinstance(programs, list):
            self._programs_cache = []
            return self._programs_cache

        self._programs_cache = [program for program in programs if isinstance(program, dict)]
        return self._programs_cache

    def _classify_program_intent(self, user_query: str) -> str | None:
        """Map query keywords to a specific program ID when confidence is high."""
        lowered_query: str = user_query.lower()
        for program_id, keywords in self._intent_map.items():
            if any(keyword in lowered_query for keyword in keywords):
                return program_id
        return None

    def classify_intent(self, user_query: str) -> str:
        """Classify query intent for routing, guardrails, and analytics."""
        if self.is_out_of_bounds_query(user_query):
            return "OUT_OF_BOUNDS"
        if self._is_degree_layout_request(user_query):
            return "DEGREE_LAYOUT"
        if self._classify_program_intent(user_query) is not None:
            return "PROGRAM_FOCUSED"
        return "GENERIC_CATALOG"

    def get_matched_keywords(self, user_query: str) -> list[str]:
        """Return matched routing keywords for anonymized analytics logging."""
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
            matched.add(f"{rubric.upper()} {number}")

        return sorted(matched)

    def is_out_of_bounds_query(self, user_query: str) -> bool:
        """Return True when a query falls outside academic advising scope."""
        lowered_query: str = user_query.lower().strip()
        if not lowered_query:
            return True

        if re.search(r"\b[a-z]{4}\s*\d{4}\b", lowered_query):
            return False

        has_academic_signal: bool = any(
            keyword in lowered_query for keyword in self._academic_keywords
        )
        has_non_academic_signal: bool = any(
            keyword in lowered_query for keyword in self._non_academic_keywords
        )

        if has_non_academic_signal and not has_academic_signal:
            return True

        return not has_academic_signal

    def _is_degree_layout_request(self, user_query: str) -> bool:
        """Return True when the user asks for degree-plan structure output."""
        lowered_query: str = user_query.lower()
        return any(keyword in lowered_query for keyword in self._degree_layout_keywords)

    def _extract_course_codes(self, raw_text: str) -> list[str]:
        """Normalize and extract course codes from arbitrary prerequisite strings."""
        normalized_codes: list[str] = []
        for rubric, number in _COURSE_CODE_PATTERN.findall(raw_text):
            normalized_codes.append(f"{rubric.upper()} {number}")
        return normalized_codes

    def _extract_completed_courses_from_query(self, user_query: str) -> list[str]:
        """Extract completed course codes from user text as best-effort context."""
        return self._extract_course_codes(user_query)

    def _extract_prerequisite_codes(self, course: dict[str, object]) -> list[str]:
        """Parse prerequisite logic strings and return prerequisite course codes."""
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
        """Build directed prerequisite relationships per program."""
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
        """Return lazily-built prerequisite index, caching after first build."""
        if self._prerequisite_index_cache is None:
            self._prerequisite_index_cache = self._build_prerequisite_index()
        return self._prerequisite_index_cache

    def get_missing_prerequisites(
        self,
        completed_courses: list[str],
        target_program: str,
    ) -> dict[str, list[str]]:
        """Return advanced courses that remain locked by missing prerequisites."""
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
        """Return flattened degree-plan checklist cards for explicit layout requests."""
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
        """Return missing prerequisite dependency tree for a degree-layout query."""
        if not self._is_degree_layout_request(user_query):
            return {}

        program_id: str | None = self._classify_program_intent(user_query)
        if program_id is None:
            return {}

        completed_courses: list[str] = self._extract_completed_courses_from_query(user_query)
        return self.get_missing_prerequisites(completed_courses, program_id)

    def _json_within_budget(self, payload: dict[str, object]) -> str:
        """Serialize payload and enforce the configured character budget."""
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

    def _build_targeted_program_context(self, program: dict[str, object], program_id: str) -> str:
        """Build a bounded targeted context payload for one matched program."""
        targeted_payload: dict[str, object] = {
            "mode": "targeted_program",
            "program_id": program_id,
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
            return self._json_within_budget(targeted_payload)

        serialized: str = self._json_within_budget(targeted_payload)
        program_payload: dict[str, object] = targeted_payload["program"]
        if not isinstance(program_payload, dict):
            return serialized

        semester_list: object = program_payload.get("semesters")
        if not isinstance(semester_list, list):
            return serialized

        for semester in semesters:
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
                    if not isinstance(course, dict):
                        continue

                    compact_course: dict[str, object] = {
                        "code": course.get("code"),
                        "title": course.get("title"),
                        "credits": course.get("credits"),
                    }
                    courses_container.append(compact_course)

                    serialized = self._json_within_budget(targeted_payload)
                    if len(serialized) >= self.char_budget:
                        return serialized

            serialized = self._json_within_budget(targeted_payload)
            if len(serialized) >= self.char_budget:
                return serialized

        return serialized

    def _build_generic_index_context(self) -> str:
        """Build a compressed token-signature map for broad user queries."""
        index_payload: dict[str, object] = {"mode": "catalog_index_signature", "signature": ""}
        signature_entries: list[str] = []
        if not isinstance(index_payload.get("signature"), str):
            return self._json_within_budget(index_payload)

        seen_entries: set[tuple[str, str]] = set()

        for program in self._programs():
            program_id: str = str(program.get("program_id", "unknown_program"))
            semesters: object = program.get("semesters")
            if not isinstance(semesters, list):
                continue

            for semester in semesters:
                if not isinstance(semester, dict):
                    continue
                courses: object = semester.get("courses")
                if not isinstance(courses, list):
                    continue

                for course in courses:
                    if not isinstance(course, dict):
                        continue

                    code: str = str(course.get("code", "")).strip()
                    title: str = str(course.get("title", "")).strip()
                    if not code and not title:
                        continue

                    dedupe_key: tuple[str, str] = (code, title)
                    if dedupe_key in seen_entries:
                        continue
                    seen_entries.add(dedupe_key)

                    compact_entry: dict[str, object] = {
                        "program_id": program_id,
                        "code": code,
                        "title": title,
                        "credits": course.get("credits"),
                    }
                    normalized_title: str = re.sub(r"\s+", " ", str(compact_entry["title"])).strip()
                    signature_entries.append(
                        f"{program_id}|{compact_entry['code']}|{normalized_title}|{compact_entry['credits']}"
                    )
                    index_payload["signature"] = ";".join(signature_entries)

                    serialized: str = self._json_within_budget(index_payload)
                    if len(serialized) >= self.char_budget:
                        return serialized

        return self._json_within_budget(index_payload)

    def get_optimized_context(self, user_query: str) -> str:
        """Return a query-scoped, budget-limited context snippet for prompting.

        Args:
            user_query: End-user message used for intent-aware context slicing.

        Returns:
            A compact JSON string limited by ``char_budget``.
        """
        matched_program_id: str | None = self._classify_program_intent(user_query)
        if matched_program_id is not None:
            for program in self._programs():
                if str(program.get("program_id")) == matched_program_id:
                    return self._build_targeted_program_context(program, matched_program_id)

        return self._build_generic_index_context()


_CATALOG_SEARCH_ENGINE: CatalogSearchEngine | None = None


def _get_catalog_search_engine() -> CatalogSearchEngine:
    """Return the singleton catalog search engine, initializing on first use."""
    global _CATALOG_SEARCH_ENGINE
    if _CATALOG_SEARCH_ENGINE is None:
        _CATALOG_SEARCH_ENGINE = CatalogSearchEngine(
            cache_path=_CATALOG_CACHE_PATH,
            char_budget=_CONTEXT_CHAR_BUDGET,
        )
    return _CATALOG_SEARCH_ENGINE


def _get_optimized_catalog_context(user_query: str) -> str:
    """Return a token-optimized catalog context snippet for the user query."""
    try:
        return _get_catalog_search_engine().get_optimized_context(user_query)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


def _is_out_of_bounds_query(user_query: str) -> bool:
    """Return True when the query falls outside advising scope."""
    return _get_catalog_search_engine().is_out_of_bounds_query(user_query)


def _get_degree_progress_cards(user_query: str) -> list[dict[str, object]]:
    """Return checklist card payloads for explicit degree-layout requests."""
    return _get_catalog_search_engine().get_degree_progress_cards(user_query)


def _get_degree_prerequisite_tree(user_query: str) -> dict[str, list[str]]:
    """Return prerequisite dependency tree for explicit degree-layout requests."""
    return _get_catalog_search_engine().get_degree_prerequisite_tree(user_query)


def _write_analytics_entry_sync(entry: dict[str, object], output_path: Path) -> None:
    """Append analytics entry to local cache file synchronously."""
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
    """Write anonymized analytics metadata to a local JSON cache file.

    Privacy Design:
        Stores no raw query text. Persists only intent metadata, matched
        keywords, and guardrail trigger state.
    """
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
    """Return the configured CORS origin list from the environment.

    Returns:
        A list of origin strings suitable for FastAPI's CORS middleware.
    """
    raw_origins: str = os.environ.get("CORS_ALLOWED_ORIGINS", "*")
    origins: list[str] = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    return origins or ["*"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Yield ASGI lifespan context without forcing heavy startup initialization."""
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
    """Validated inbound chat message from the floating widget.

    Args:
        message: End-user question constrained to 1-1000 characters.
    """

    model_config = ConfigDict(frozen=True)

    message: Annotated[str, Field(min_length=1, max_length=1000)] = Field(
        ...,
        description="End-user question constrained to 1-1000 characters.",
    )


class ChatResponse(BaseModel):
    """Validated outbound assistant reply.

    Args:
        reply: Plain-text grounded response returned to the widget.
        model: Provider/model pair used to generate the response.
        progress_cards: Optional structured progress card payload for UI rendering.
        prerequisite_tree: Optional prerequisite dependency map by course code.
    """

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
    """A single Gemini content part.

    Args:
        text: Text payload for the Gemini API part.
    """

    model_config = ConfigDict(frozen=True)

    text: str = Field(..., description="Text payload for the Gemini API part.")


class GeminiContent(BaseModel):
    """A Gemini content block for a single role.

    Args:
        role: Gemini role label such as ``user``.
        parts: Ordered text parts attached to the role.
    """

    model_config = ConfigDict(frozen=True)

    role: str = Field(..., description="Gemini role label such as 'user'.")
    parts: list[GeminiPart] = Field(..., description="Ordered text parts attached to the role.")


class GeminiRequest(BaseModel):
    """HTTP payload sent to Gemini 1.5 Flash.

    Args:
        system_instruction: Shared system-level instruction block.
        contents: User message content blocks.
        generation_config: Deterministic generation configuration.
    """

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
    """One OpenAI-compatible chat message for Groq.

    Args:
        role: Chat role such as ``system`` or ``user``.
        content: Plain-text message content.
    """

    model_config = ConfigDict(frozen=True)

    role: str = Field(..., description="Chat role such as 'system' or 'user'.")
    content: str = Field(..., description="Plain-text message content.")


class GroqRequest(BaseModel):
    """HTTP payload sent to Groq's chat completions endpoint.

    Args:
        model: Groq-hosted model name.
        messages: Ordered OpenAI-compatible chat messages.
        temperature: Deterministic sampling temperature.
        top_p: Top-p setting paired with temperature 0.0.
    """

    model_config = ConfigDict(frozen=True)

    model: str = Field(..., description="Groq-hosted model name.")
    messages: list[GroqMessage] = Field(
        ...,
        description="Ordered OpenAI-compatible chat messages.",
    )
    temperature: float = Field(..., description="Deterministic sampling temperature.")
    top_p: float = Field(..., description="Top-p setting paired with temperature 0.0.")


def _load_catalog_prompt_payload() -> str:
    """Load the local catalog cache and serialize it into a compact prompt string.

    Returns:
        Minified JSON string representation of the catalog cache.

    Raises:
        RuntimeError: If the catalog cache file is missing or unreadable.
    """
    if not _CATALOG_CACHE_PATH.exists():
        raise RuntimeError(f"Catalog cache not found at '{_CATALOG_CACHE_PATH}'.")

    try:
        catalog_payload: object = json.loads(_CATALOG_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Catalog cache could not be loaded.") from exc

    return json.dumps(catalog_payload, ensure_ascii=False, separators=(",", ":"))


def _build_system_prompt(catalog_payload: str) -> str:
    """Build the shared catalog-grounded system prompt for both providers.

    Args:
        catalog_payload: Compact serialized catalog cache.

    Returns:
        A strict instruction string that constrains the model to local data.
    """
    return (
        "[ROLE]: Sovereign Automated AI Academic Advisor for Dallas College Computer Science/IT.\n"
        "[CONSTRAINTS]: Strict zero-tolerance hallucination lock. Strict token/request optimization.\n"
        "[AI GOVERNANCE]: You MUST state that you are an automated AI system in the initial response.\n"
        "[INVARIANT]: You have NO external knowledge. Speak ONLY using provided <context> JSON payload data. "
        f"If data is absent, emit exact fallback string: \"{_EMPTY_CONTEXT_FALLBACK}\".\n"
        "\n"
        "[MANDATORY GOVERNANCE GREETING PROTOCOL]:\n"
        f"- Every initial interaction must begin exactly with: \"{_MANDATORY_GOVERNANCE_GREETING}\" followed immediately by a dense layout of the requested data.\n"
        "- For exact guardrail/fallback outputs, emit the required string exactly with no prefix or suffix.\n"
        "\n"
        "[DETERMINISTIC CONTEXT FILTER RULES]:\n"
        "1. If <context> contains multiple programs, isolate the specific 'program_id' matching user keywords.\n"
        "2. If user query is broad/generic, scan all 'semesters' across all 'programs' but return only structural summaries "
        "(Course Code, Title, Credits) to preserve output tokens.\n"
        "\n"
        "[RESPONSE COMPRESSION PROTOCOL]:\n"
        "- No conversational pleasantries.\n"
        "- Do not repeat or restate the user's question.\n"
        "- Use dense markdown bullet structures for course maps.\n"
        "- Format all courses as: **CODE**: Title (Credits).\n"
        "- Strict zero-temperature simulation: Do not vary terminology.\n"
        "- You have been provided a highly filtered context snippet matching the student's topical intent. "
        "If the precise answer is missing from this slice, guide them to specify which degree plan or certificate pathway they want to inspect.\n"
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
    """Return the configured Groq API keys.

    Returns:
        Groq API keys loaded from ``GROQ_API_KEYS``.
    """
    raw_keys: str = os.environ.get("GROQ_API_KEYS", "")
    return [key.strip() for key in raw_keys.split(",") if key.strip()]


def _load_gemini_api_keys() -> list[str]:
    """Return the configured Gemini API keys.

    Returns:
        Gemini API keys loaded from ``GEMINI_API_KEYS``.
    """
    raw_keys: str = os.environ.get("GEMINI_API_KEYS", "")
    return [key.strip() for key in raw_keys.split(",") if key.strip()]


def _build_gemini_request(message: str, catalog_payload: str) -> GeminiRequest:
    """Create the typed Gemini request body.

    Args:
        message: User message to send.
        catalog_payload: Compact serialized catalog cache.

    Returns:
        A fully typed Gemini request model.
    """
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
            "temperature": 0.0,
            "topP": 1.0,
            "maxOutputTokens": 512,
        },
    )


def _build_groq_request(message: str, catalog_payload: str) -> GroqRequest:
    """Create the typed Groq chat request body.

    Args:
        message: User message to send.
        catalog_payload: Compact serialized catalog cache.

    Returns:
        A fully typed Groq request model.
    """
    return GroqRequest(
        model=_GROQ_MODEL_NAME,
        messages=[
            GroqMessage(role="system", content=_build_system_prompt(catalog_payload)),
            GroqMessage(role="user", content=message),
        ],
        temperature=0.0,
        top_p=1.0,
    )


def _extract_gemini_text(response_payload: dict[str, object]) -> str:
    """Extract plain text from a Gemini API response payload.

    Args:
        response_payload: Parsed JSON response from Gemini.

    Returns:
        The first available text part emitted by the model.

    Raises:
        RuntimeError: If the payload does not contain a text candidate.
    """
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
    """Extract plain text from a Groq chat-completions response payload.

    Args:
        response_payload: Parsed JSON response from Groq.

    Returns:
        The first available message content emitted by the model.

    Raises:
        RuntimeError: If the payload does not contain a text choice.
    """
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
    client: httpx.AsyncClient,
    message: str,
    catalog_payload: str,
) -> ChatResponse | None:
    """Attempt Groq generation with API-key rotation.

    Args:
        client: Shared async HTTP client.
        message: Validated user message.
        catalog_payload: Compact serialized catalog cache.

    Returns:
        A chat response if Groq succeeds, otherwise ``None`` so the caller can
        fall back to Gemini.

    Raises:
        HTTPException: If Groq returns a non-recoverable upstream error.
    """
    api_keys: list[str] = _load_groq_api_keys()
    if not api_keys:
        return None

    request_payload: GroqRequest = _build_groq_request(
        message=message,
        catalog_payload=catalog_payload,
    )

    for key_index, api_key in enumerate(api_keys, start=1):
        response: httpx.Response = await client.post(
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
    client: httpx.AsyncClient,
    message: str,
    catalog_payload: str,
) -> ChatResponse:
    """Attempt Gemini generation with API-key rotation.

    Args:
        client: Shared async HTTP client.
        message: Validated user message.
        catalog_payload: Compact serialized catalog cache.

    Returns:
        A chat response if Gemini succeeds.

    Raises:
        HTTPException: If Gemini configuration is missing or upstream calls fail.
    """
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
        response: httpx.Response = await client.post(
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
    """Generate a grounded chat reply using Groq first and Gemini second.

    Args:
        message: Validated user message.

    Returns:
        Typed chat response containing the provider reply.

    Raises:
        HTTPException: If catalog loading fails or all providers fail.
    """
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
    """Return a liveness response for local development and uptime probes.

    Returns:
        A small status payload for health checks.
    """
    return {"status": "ok"}


@app.get("/api/test-routing", status_code=status.HTTP_200_OK)
async def test_routing() -> dict[str, str]:
    """Return a static probe to confirm uvicorn is serving this module."""
    return {"message": "Uvicorn is successfully serving api/chat.py"}


@app.post("/api/chat/", response_model=ChatResponse, status_code=status.HTTP_200_OK)
@app.post("/api/chat", response_model=ChatResponse, status_code=status.HTTP_200_OK)
async def chat(request: ChatRequest) -> ChatResponse:
    """Handle a widget message through the hybrid Groq-to-Gemini cascade.

    Args:
        request: Validated incoming chat request.

    Returns:
        A grounded assistant reply generated from the local catalog payload.
    """
    try:
        return await _generate_chat_reply(message=request.message)
    except Exception as e:  # noqa: BLE001
        logging.error(f"Search route exception: {str(e)}")
        return ChatResponse(
            reply=(
                "I encountered an optimization bottleneck reading the catalog data "
                "structure for that topic. Please try asking about a specific course "
                "code (e.g., WLDG or AERM) while I refine my indexing rules!"
            ),
            model="System-Fallback-Shield",
            progress_cards=[],
        )
