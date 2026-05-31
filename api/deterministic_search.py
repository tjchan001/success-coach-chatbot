"""Deterministic phrase-safe retrieval pipeline for course search.

Architectural Intent:
    This module provides a deterministic, hallucination-safe retrieval engine
    that enforces canonical phrase resolution, ambiguity controls, strict phrase
    gating, safe lexical matching, and explicit scoring/ranking.

Security Rationale:
    - Matching is boundary-aware to prevent unsafe substring bleed.
    - Fallback matching is policy-gated and never runs without validation.
    - Inputs and outputs use plain JSON-compatible dictionaries/lists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CANONICAL_ALIAS_MAP: dict[str, str] = {
    "film editing": "video editing",
    "editing of video": "video editing",
    "video edit": "video editing",
    "video post production": "video editing",
    "video post-production": "video editing",
}

_DOMAIN_POLICIES: dict[str, dict[str, object]] = {
    "video editing": {
        "allowed_prefixes": {"FLMC", "RTVB", "COMM"},
        "anti_terms": {
            "gene editing",
            "genome",
            "genetic",
            "molecular biology",
            "cell culture",
            "biotechnology",
            "crispr",
        },
        "fallback_tokens": {"video", "editing", "film", "post", "production"},
        "min_fallback_token_hits": 2,
    }
}


@dataclass(frozen=True)
class _QueryState:
    """Immutable normalized query state used by retrieval layers."""

    raw_text: str
    normalized_text: str
    tokens: list[str]
    is_single_word: bool


@dataclass(frozen=True)
class _CanonicalResolution:
    """Canonical phrase resolution output with alias provenance."""

    canonical_phrases: list[str]
    alias_hits: list[dict[str, str]]


@dataclass(frozen=True)
class _GateDecision:
    """Strict phrase-gate decision for one candidate against one phrase."""

    accepted: bool
    phrase_exact: bool
    fallback_used: bool
    anti_term_hit: bool
    token_hits: int
    score: float
    reasons: list[str]


def _normalize_text(value: object) -> str:
    """Normalize a text-like value to lowercase single-spaced text."""
    if not isinstance(value, str):
        return ""
    lowered: str = value.lower().strip()
    cleaned: str = re.sub(r"[^a-z0-9\s-]", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def _tokenize(text: str) -> list[str]:
    """Tokenize normalized text into boundary-safe word tokens."""
    return re.findall(r"[a-z0-9]+", text)


def _compile_phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Compile a boundary-safe pattern for a multi-word phrase."""
    normalized_phrase: str = _normalize_text(phrase)
    phrase_tokens: list[str] = [re.escape(token) for token in normalized_phrase.split() if token]
    phrase_pattern: str = r"\s+".join(phrase_tokens)
    return re.compile(rf"\b{phrase_pattern}\b")


def _has_phrase_boundary(text: str, phrase: str) -> bool:
    """Return True only when phrase appears as full boundary-delimited words."""
    if not text or not phrase:
        return False
    return _compile_phrase_pattern(phrase).search(text) is not None


def _has_word_boundary(text: str, token: str) -> bool:
    """Return True when token exists as a full word boundary match."""
    normalized_token: str = _normalize_text(token)
    if not text or not normalized_token:
        return False
    return re.search(rf"\b{re.escape(normalized_token)}\b", text) is not None


def normalize_query(query: str) -> dict[str, object]:
    """Normalize raw query text into a deterministic query state dictionary."""
    normalized_text: str = _normalize_text(query)
    tokens: list[str] = _tokenize(normalized_text)
    state: _QueryState = _QueryState(
        raw_text=query,
        normalized_text=normalized_text,
        tokens=tokens,
        is_single_word=len(tokens) == 1,
    )
    return {
        "raw_text": state.raw_text,
        "normalized_text": state.normalized_text,
        "tokens": state.tokens,
        "is_single_word": state.is_single_word,
    }


def resolve_canonical_phrases(
    normalized_query: str,
    alias_map: dict[str, str] | None = None,
) -> dict[str, object]:
    """Resolve canonical phrases using alias mapping and direct canonical hits."""
    mapping: dict[str, str] = dict(_CANONICAL_ALIAS_MAP)
    if alias_map is not None:
        mapping.update(alias_map)

    canonical_hits: list[str] = []
    alias_hits: list[dict[str, str]] = []

    for alias, canonical in mapping.items():
        if _has_phrase_boundary(normalized_query, alias):
            canonical_hits.append(canonical)
            alias_hits.append({"alias": alias, "canonical": canonical})

    for canonical in _DOMAIN_POLICIES:
        if _has_phrase_boundary(normalized_query, canonical):
            canonical_hits.append(canonical)

    unique_canonical: list[str] = []
    seen_canonical: set[str] = set()
    for canonical in canonical_hits:
        if canonical in seen_canonical:
            continue
        unique_canonical.append(canonical)
        seen_canonical.add(canonical)

    resolution: _CanonicalResolution = _CanonicalResolution(
        canonical_phrases=unique_canonical,
        alias_hits=alias_hits,
    )
    return {
        "canonical_phrases": resolution.canonical_phrases,
        "alias_hits": resolution.alias_hits,
    }


def is_ambiguous_single_word(query_state: dict[str, object], canonical_phrases: list[str]) -> bool:
    """Return True for under-specified single-token intents with no canonical resolution."""
    is_single_word: bool = bool(query_state.get("is_single_word", False))
    return is_single_word and not canonical_phrases


def _should_clarify_noncanonical_query(
    query_state: dict[str, object],
    canonical_phrases: list[str],
) -> bool:
    """Return True when a multi-word query remains too vague for safe retrieval."""
    if canonical_phrases:
        return False

    tokens_obj: object = query_state.get("tokens", [])
    if not isinstance(tokens_obj, list):
        return False

    tokens: set[str] = {str(token).lower() for token in tokens_obj if str(token).strip()}
    weak_intent_tokens: set[str] = {
        "edit",
        "editing",
        "course",
        "class",
        "classes",
    }

    if tokens.intersection(weak_intent_tokens):
        return True

    return False


def _extract_prefix(course_code: str) -> str:
    """Extract uppercase subject prefix from a course code string."""
    match: re.Match[str] | None = re.search(r"\b([a-zA-Z]{4})\b", course_code)
    if match is None:
        return ""
    return match.group(1).upper()


def _course_searchable_text(course: dict[str, object]) -> str:
    """Build normalized searchable text from JSON-compatible course fields."""
    fields: tuple[str, ...] = ("title", "description", "notes")
    parts: list[str] = [_normalize_text(course.get(field_name, "")) for field_name in fields]
    return " ".join(part for part in parts if part).strip()


def flatten_catalog_courses(payload: dict[str, object]) -> list[dict[str, object]]:
    """Flatten catalog payload into candidate dictionaries with parent metadata."""
    candidates: list[dict[str, object]] = []
    programs_obj: object = payload.get("programs", [])
    if not isinstance(programs_obj, list):
        return candidates

    for program in programs_obj:
        if not isinstance(program, dict):
            continue
        program_id: str = str(program.get("program_id", "")).strip()
        program_title: str = str(program.get("title", "")).strip()
        semesters_obj: object = program.get("semesters", [])
        if not isinstance(semesters_obj, list):
            continue
        for semester in semesters_obj:
            if not isinstance(semester, dict):
                continue
            semester_name: str = str(semester.get("name", "Requirements")).strip() or "Requirements"
            courses_obj: object = semester.get("courses", [])
            if not isinstance(courses_obj, list):
                continue
            for course in courses_obj:
                if not isinstance(course, dict):
                    continue
                course_code: str = str(course.get("code", "")).strip()
                candidates.append(
                    {
                        "program_id": program_id,
                        "program_title": program_title,
                        "semester_name": semester_name,
                        "course": course,
                        "course_code": course_code,
                        "course_prefix": _extract_prefix(course_code),
                        "searchable_text": _course_searchable_text(course),
                    }
                )
    return candidates


def _count_boundary_token_hits(text: str, tokens: set[str]) -> int:
    """Count unique boundary token hits for fallback evidence."""
    hit_count: int = 0
    for token in tokens:
        if _has_word_boundary(text, token):
            hit_count += 1
    return hit_count


def _evaluate_candidate(
    candidate: dict[str, object],
    canonical_phrase: str,
    ambiguity_resolved: bool,
) -> _GateDecision:
    """Evaluate strict phrase gate, anti-terms, fallback policy, and score."""
    policy_obj: object = _DOMAIN_POLICIES.get(canonical_phrase, {})
    policy: dict[str, object] = policy_obj if isinstance(policy_obj, dict) else {}

    searchable_text: str = str(candidate.get("searchable_text", ""))
    course_prefix: str = str(candidate.get("course_prefix", ""))

    allowed_prefixes_obj: object = policy.get("allowed_prefixes", set())
    anti_terms_obj: object = policy.get("anti_terms", set())
    fallback_tokens_obj: object = policy.get("fallback_tokens", set())
    min_hits_obj: object = policy.get("min_fallback_token_hits", 2)

    allowed_prefixes: set[str] = (
        {str(item).upper() for item in allowed_prefixes_obj}
        if isinstance(allowed_prefixes_obj, set)
        else set()
    )
    anti_terms: set[str] = (
        {str(item).lower() for item in anti_terms_obj}
        if isinstance(anti_terms_obj, set)
        else set()
    )
    fallback_tokens: set[str] = (
        {str(item).lower() for item in fallback_tokens_obj}
        if isinstance(fallback_tokens_obj, set)
        else set()
    )
    min_fallback_token_hits: int = int(min_hits_obj) if isinstance(min_hits_obj, int) else 2

    anti_term_hit: bool = any(_has_phrase_boundary(searchable_text, term) for term in anti_terms)
    phrase_exact: bool = _has_phrase_boundary(searchable_text, canonical_phrase)
    prefix_allowed: bool = course_prefix in allowed_prefixes
    token_hits: int = _count_boundary_token_hits(searchable_text, fallback_tokens)

    fallback_allowed: bool = (
        ambiguity_resolved
        and not anti_term_hit
        and not phrase_exact
        and prefix_allowed
        and token_hits >= min_fallback_token_hits
    )

    reasons: list[str] = []
    if phrase_exact:
        reasons.append("canonical_phrase_match")
    else:
        reasons.append("canonical_phrase_miss")
    if anti_term_hit:
        reasons.append("anti_term_hit")
    if prefix_allowed:
        reasons.append("allowed_prefix")
    else:
        reasons.append("disallowed_prefix")
    if fallback_allowed:
        reasons.append("validated_token_fallback")

    accepted: bool = phrase_exact or fallback_allowed
    score: float = 0.0
    if phrase_exact:
        score += 100.0
    if prefix_allowed:
        score += 35.0
    score += float(token_hits * 10)
    if anti_term_hit:
        score -= 120.0
    if fallback_allowed:
        score -= 15.0

    return _GateDecision(
        accepted=accepted,
        phrase_exact=phrase_exact,
        fallback_used=fallback_allowed,
        anti_term_hit=anti_term_hit,
        token_hits=token_hits,
        score=score,
        reasons=reasons,
    )


def _result_sort_key(row: dict[str, object]) -> tuple[float, float, str, str]:
    """Return deterministic sort key for scored rows."""
    score: float = float(row.get("score", 0.0))
    phrase_exact: bool = bool(row.get("phrase_exact", False))
    course_code: str = str(row.get("code", ""))
    program_id: str = str(row.get("program_id", ""))
    return (-score, -float(phrase_exact), course_code, program_id)


def score_and_rank(
    accepted_rows: list[dict[str, object]],
    top_k: int,
) -> list[dict[str, object]]:
    """Sort accepted rows deterministically and return top-k results."""
    ranked: list[dict[str, object]] = sorted(accepted_rows, key=_result_sort_key)
    clamped_top_k: int = max(0, top_k)
    return ranked[:clamped_top_k]


def search(
    query: str,
    catalog_payload: dict[str, object],
    top_k: int = 3,
    alias_map: dict[str, str] | None = None,
) -> dict[str, object]:
    """Execute deterministic search pipeline over a JSON-compatible catalog payload."""
    query_state: dict[str, object] = normalize_query(query)
    normalized_query: str = str(query_state.get("normalized_text", ""))
    canonical_resolution: dict[str, object] = resolve_canonical_phrases(normalized_query, alias_map)

    canonical_phrases_obj: object = canonical_resolution.get("canonical_phrases", [])
    canonical_phrases: list[str] = (
        [str(item) for item in canonical_phrases_obj] if isinstance(canonical_phrases_obj, list) else []
    )

    ambiguous: bool = is_ambiguous_single_word(query_state, canonical_phrases)
    if ambiguous:
        return {
            "status": "needs_clarification",
            "query": query_state,
            "canonical_resolution": canonical_resolution,
            "results": [],
            "trace": [
                {
                    "event": "ambiguity_gate",
                    "reason": "single_word_query_without_canonical_phrase",
                }
            ],
        }

    needs_clarification: bool = _should_clarify_noncanonical_query(query_state, canonical_phrases)
    if needs_clarification:
        return {
            "status": "needs_clarification",
            "query": query_state,
            "canonical_resolution": canonical_resolution,
            "results": [],
            "trace": [
                {
                    "event": "borderline_query_gate",
                    "reason": "noncanonical_query_requires_clarification",
                }
            ],
        }

    candidates: list[dict[str, object]] = flatten_catalog_courses(catalog_payload)
    accepted_rows: list[dict[str, object]] = []
    rejected_trace: list[dict[str, object]] = []

    for candidate in candidates:
        if not canonical_phrases:
            rejected_trace.append(
                {
                    "course_code": candidate.get("course_code", ""),
                    "accepted": False,
                    "reasons": ["no_canonical_phrase_resolved"],
                }
            )
            continue

        best_decision: _GateDecision | None = None
        best_phrase: str = ""
        for canonical_phrase in canonical_phrases:
            decision: _GateDecision = _evaluate_candidate(
                candidate=candidate,
                canonical_phrase=canonical_phrase,
                ambiguity_resolved=not ambiguous,
            )
            if best_decision is None or decision.score > best_decision.score:
                best_decision = decision
                best_phrase = canonical_phrase

        if best_decision is None:
            continue

        code: str = str(candidate.get("course_code", "")).strip()
        course_obj: object = candidate.get("course", {})
        course: dict[str, object] = course_obj if isinstance(course_obj, dict) else {}

        if best_decision.accepted:
            accepted_rows.append(
                {
                    "code": code,
                    "title": str(course.get("title", "")).strip(),
                    "description": str(course.get("description", "")).strip(),
                    "program_id": str(candidate.get("program_id", "")).strip(),
                    "program_title": str(candidate.get("program_title", "")).strip(),
                    "semester_name": str(candidate.get("semester_name", "")).strip(),
                    "course_prefix": str(candidate.get("course_prefix", "")).strip(),
                    "score": round(best_decision.score, 4),
                    "phrase_exact": best_decision.phrase_exact,
                    "fallback_used": best_decision.fallback_used,
                    "matched_canonical_phrase": best_phrase,
                    "reasons": best_decision.reasons,
                }
            )
        else:
            rejected_trace.append(
                {
                    "course_code": code,
                    "accepted": False,
                    "matched_canonical_phrase": best_phrase,
                    "reasons": best_decision.reasons,
                }
            )

    ranked_results: list[dict[str, object]] = score_and_rank(accepted_rows, top_k)
    return {
        "status": "ok",
        "query": query_state,
        "canonical_resolution": canonical_resolution,
        "results": ranked_results,
        "trace": rejected_trace,
    }


__all__: list[str] = [
    "flatten_catalog_courses",
    "is_ambiguous_single_word",
    "normalize_query",
    "resolve_canonical_phrases",
    "score_and_rank",
    "search",
]
