"""Tests for deterministic phrase-safe retrieval pipeline."""

from __future__ import annotations

import json

from api import deterministic_search


def _catalog_fixture() -> dict[str, object]:
    return {
        "programs": [
            {
                "program_id": "Biotech_AAS",
                "title": "Biotechnology A.A.S.",
                "semesters": [
                    {
                        "name": "Semester 1",
                        "courses": [
                            {
                                "code": "BITC 2431",
                                "title": "Gene Editing Lab",
                                "description": "Hands-on gene editing and cell culture techniques.",
                            }
                        ],
                    }
                ],
            },
            {
                "program_id": "Film_Certificate",
                "title": "Film and Video Certificate",
                "semesters": [
                    {
                        "name": "Semester 1",
                        "courses": [
                            {
                                "code": "RTVB 2430",
                                "title": "Film and Video Editing",
                                "description": "Nonlinear video editing projects.",
                            },
                            {
                                "code": "COMM 2301",
                                "title": "Advanced Video Workshop",
                                "description": "Video production and editing portfolio studio.",
                            },
                            {
                                "code": "RTVB 1301",
                                "title": "Videography and Crediting Basics",
                                "description": "Crediting workflows without editing specialization.",
                            },
                        ],
                    }
                ],
            },
        ]
    }


def test_alias_mapping_resolves_film_editing_to_video_editing() -> None:
    normalized: dict[str, object] = deterministic_search.normalize_query("film editing")
    resolution: dict[str, object] = deterministic_search.resolve_canonical_phrases(
        str(normalized["normalized_text"])
    )

    assert "video editing" in resolution["canonical_phrases"]


def test_single_word_editing_is_ambiguous() -> None:
    result: dict[str, object] = deterministic_search.search("editing", _catalog_fixture())

    assert result["status"] == "needs_clarification"


def test_strict_phrase_gate_rejects_gene_editing_candidate() -> None:
    result: dict[str, object] = deterministic_search.search("video editing", _catalog_fixture(), top_k=5)
    codes: list[str] = [str(row.get("code", "")) for row in result["results"]]

    assert "BITC 2431" not in codes


def test_boundary_matching_blocks_substring_bleed() -> None:
    result: dict[str, object] = deterministic_search.search("video editing", _catalog_fixture(), top_k=10)
    codes: list[str] = [str(row.get("code", "")) for row in result["results"]]

    assert "RTVB 1301" not in codes


def test_validated_fallback_accepts_allowed_prefix_when_exact_phrase_missing() -> None:
    result: dict[str, object] = deterministic_search.search("video editing", _catalog_fixture(), top_k=10)
    rows_by_code: dict[str, dict[str, object]] = {
        str(row.get("code", "")): row for row in result["results"]
    }

    assert bool(rows_by_code["COMM 2301"].get("fallback_used", False))


def test_scoring_ranking_and_top_k_are_deterministic() -> None:
    result: dict[str, object] = deterministic_search.search("video editing", _catalog_fixture(), top_k=1)

    assert [row["code"] for row in result["results"]] == ["RTVB 2430"]


def test_output_is_json_serializable() -> None:
    result: dict[str, object] = deterministic_search.search("film editing", _catalog_fixture(), top_k=3)
    payload: str = json.dumps(result)

    assert isinstance(payload, str)


def test_phrase_mutations_canonicalize_to_video_editing() -> None:
    for query in ("editing of video", "video edit"):
        result: dict[str, object] = deterministic_search.search(query, _catalog_fixture(), top_k=5)
        codes: list[str] = [str(row.get("code", "")) for row in result["results"]]

        assert result["status"] == "ok"
        assert "video editing" in result["canonical_resolution"]["canonical_phrases"]
        assert any(code.startswith(("RTVB", "COMM", "FLMC")) for code in codes)
        assert "BITC 2431" not in codes


def test_mixed_domain_contamination_keeps_video_results_and_excludes_biotech() -> None:
    result: dict[str, object] = deterministic_search.search(
        "video editing with CRISPR",
        _catalog_fixture(),
        top_k=5,
    )
    codes: list[str] = [str(row.get("code", "")) for row in result["results"]]

    assert result["status"] == "ok"
    assert "video editing" in result["canonical_resolution"]["canonical_phrases"]
    assert any(code.startswith(("RTVB", "COMM", "FLMC")) for code in codes)
    assert "BITC 2431" not in codes


def test_ambiguous_borderline_query_requires_clarification() -> None:
    result: dict[str, object] = deterministic_search.search("editing course", _catalog_fixture())

    assert result["status"] == "needs_clarification"
    assert result["results"] == []


def test_anti_term_evasion_does_not_leak_gene_editing() -> None:
    result: dict[str, object] = deterministic_search.search("media gene editing", _catalog_fixture())
    codes: list[str] = [str(row.get("code", "")) for row in result["results"]]

    assert result["status"] == "needs_clarification"
    assert "BITC 2431" not in codes


def test_prefix_spoofing_does_not_select_biotech_course() -> None:
    result: dict[str, object] = deterministic_search.search("BITC video class", _catalog_fixture())
    codes: list[str] = [str(row.get("code", "")) for row in result["results"]]

    assert result["status"] == "needs_clarification"
    assert "BITC 2431" not in codes
    assert codes == []
