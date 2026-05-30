"""Unit tests for the hybrid Groq and Gemini chat router.

Architectural Intent:
    These tests pin the highest-risk backend behaviors in ``api/chat.py``:
    compact catalog injection, deterministic shared prompts, Groq priority,
    and Gemini fallback when Groq is unavailable or rate-limited.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from api import chat


def test_load_catalog_prompt_payload_minifies_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catalog payload loading must emit compact JSON for prompt efficiency."""
    # Arrange
    cache_path: Path = tmp_path / "catalog_mvp.json"
    cache_path.write_text(
        json.dumps({"programs": [{"title": "AAS", "semesters": []}]}, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setattr(chat, "_CATALOG_CACHE_PATH", cache_path)

    # Act
    payload: str = chat._load_catalog_prompt_payload()

    # Assert
    assert payload == '{"programs":[{"title":"AAS","semesters":[]}]}'


def test_shared_prompt_and_requests_are_deterministic() -> None:
    """Both provider payloads must share the same strict prompt and tuned generation settings."""
    # Arrange
    catalog_payload: str = '{"programs":[{"title":"AAS"}]}'

    # Act
    system_prompt: str = chat._build_system_prompt(catalog_payload)
    groq_request: chat.GroqRequest = chat._build_groq_request(
        message="Tell me about ENGL 1301",
        catalog_payload=catalog_payload,
    )
    gemini_request: chat.GeminiRequest = chat._build_gemini_request(
        message="Tell me about ENGL 1301",
        catalog_payload=catalog_payload,
    )

    # Assert
    assert "[ROLE]: Sovereign Automated AI Academic Advisor for Dallas College Computer Science/IT." in system_prompt
    assert "[AI GOVERNANCE]: You MUST state that you are an automated AI system in the initial response." in system_prompt
    assert "[MANDATORY GOVERNANCE GREETING PROTOCOL]:" in system_prompt
    assert "Greetings, I am the automated AI Advisor running on the Dallas College AI Club Sandbox Engine." in system_prompt
    assert "Directly below the greeting, append this markdown italicized bracket notice exactly:" in system_prompt
    assert "*(This application is a student-led AI Club sandbox demo and is not an officially sanctioned tool of Dallas College." in system_prompt
    assert "[SOURCE CITATION VERIFICATION RULES]:" in system_prompt
    assert "When rendering a program data card, look for the '[Catalog Source Verification Link: ...]' token" in system_prompt
    assert "labeled with an incremented index like [1], [2]" in system_prompt
    assert "CRITICAL GUARDRAIL: You are strictly forbidden from inventing, hallucinating, or predicting course prefixes or course numbers" in system_prompt
    assert "If a course code is not explicitly written in the context data layer, you must never include it in your recommendations." in system_prompt
    assert "You are strictly prohibited from writing any course code that is not explicitly listed in the VERIFIED COURSE CODE WHITELIST." in system_prompt
    assert "draw out a clear, structured sequence flow using text connectors (──>)" in system_prompt
    assert "Strict zero-tolerance hallucination lock" in system_prompt
    assert "Academic catalog context unavailable. Connection terminal error." in system_prompt
    assert "I cannot confirm that selection based on the current catalog data." in system_prompt
    assert catalog_payload in system_prompt
    assert groq_request.temperature == 0.3
    assert groq_request.top_p == 1.0
    assert groq_request.frequency_penalty == 0.7
    assert groq_request.presence_penalty == 0.5
    assert groq_request.messages[0].content == system_prompt
    assert gemini_request.generation_config["temperature"] == 0.3
    assert gemini_request.generation_config["frequencyPenalty"] == 0.7
    assert gemini_request.generation_config["presencePenalty"] == 0.5
    assert gemini_request.generation_config["topP"] == 1.0
    assert gemini_request.system_instruction.parts[0].text == system_prompt


@pytest.mark.anyio
async def test_generate_chat_reply_uses_groq_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Groq must be the first network tier when Groq keys are available."""

    class DummyAsyncClient:
        """Minimal async client stub that records provider call order."""

        requested_urls: list[str] = []

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def __aenter__(self) -> DummyAsyncClient:
            DummyAsyncClient.requested_urls = []
            return self

        async def __aexit__(
            self,
            _exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _tb: object | None,
        ) -> None:
            return None

        async def post(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            json: dict[str, object] | None = None,
        ) -> httpx.Response:
            DummyAsyncClient.requested_urls.append(url)
            assert json is not None
            if url == chat._GROQ_CHAT_URL:
                assert headers is not None
                assert headers["Authorization"] == "Bearer groq-key-one"
                assert json["temperature"] == 0.3
                assert json["frequency_penalty"] == 0.7
                assert json["presence_penalty"] == 0.5
                return httpx.Response(
                    status_code=200,
                    request=httpx.Request("POST", url),
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": "ENGL 1301 is listed for 3 credit hours.",
                                }
                            }
                        ]
                    },
                )
            pytest.fail(f"Gemini should not be called when Groq succeeds: {url}")

    # Arrange
    monkeypatch.setenv("GROQ_API_KEYS", "groq-key-one")
    monkeypatch.setenv("GEMINI_API_KEYS", "gemini-key-one")
    monkeypatch.setattr(chat, "_get_optimized_catalog_context", lambda _message: '{"programs":[]}')
    monkeypatch.setattr(chat.httpx, "AsyncClient", DummyAsyncClient)

    # Act
    response_model: chat.ChatResponse = await chat._generate_chat_reply("Tell me about ENGL 1301")

    # Assert
    assert response_model.reply == "ENGL 1301 is listed for 3 credit hours."
    assert response_model.model == "groq/llama-3.1-8b-instant"
    assert DummyAsyncClient.requested_urls == [chat._GROQ_CHAT_URL]


@pytest.mark.anyio
async def test_generate_chat_reply_falls_back_to_gemini_after_groq_429s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini must take over after all Groq keys are exhausted by 429 responses."""

    class DummyAsyncClient:
        """Minimal async client stub that returns two Groq 429s then Gemini 200."""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self._groq_calls: int = 0
            self.groq_authorizations: list[str] = []

        async def __aenter__(self) -> DummyAsyncClient:
            return self

        async def __aexit__(
            self,
            _exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _tb: object | None,
        ) -> None:
            return None

        async def post(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            json: dict[str, object] | None = None,
        ) -> httpx.Response:
            assert json is not None
            if url == chat._GROQ_CHAT_URL:
                self._groq_calls += 1
                assert headers is not None
                self.groq_authorizations.append(headers["Authorization"])
                return httpx.Response(status_code=429, request=httpx.Request("POST", url))

            assert url.startswith(chat._GEMINI_GENERATE_URL)
            assert json["generation_config"] == {
                "temperature": 0.3,
                "frequencyPenalty": 0.7,
                "presencePenalty": 0.5,
                "topP": 1.0,
                "maxOutputTokens": 512,
            }
            return httpx.Response(
                status_code=200,
                request=httpx.Request("POST", url),
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": "The catalog cache confirms ENGL 1301 at 3 credit hours.",
                                    }
                                ]
                            }
                        }
                    ]
                },
            )

    # Arrange
    dummy_client: DummyAsyncClient = DummyAsyncClient()
    monkeypatch.setenv("GROQ_API_KEYS", "groq-key-one,groq-key-two")
    monkeypatch.setenv("GEMINI_API_KEYS", "gemini-key-one")
    monkeypatch.setattr(chat, "_get_optimized_catalog_context", lambda _message: '{"programs":[]}')
    monkeypatch.setattr(chat.httpx, "AsyncClient", lambda *args, **kwargs: dummy_client)

    # Act
    response_model: chat.ChatResponse = await chat._generate_chat_reply("Tell me about ENGL 1301")

    # Assert
    assert response_model.reply == "The catalog cache confirms ENGL 1301 at 3 credit hours."
    assert response_model.model == "gemini/gemini-1.5-flash"
    assert dummy_client.groq_authorizations == [
        "Bearer groq-key-one",
        "Bearer groq-key-two",
    ]


@pytest.mark.anyio
async def test_generate_chat_reply_falls_back_to_gemini_when_groq_keys_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini must be called when no Groq keys are configured."""

    class DummyAsyncClient:
        """Minimal async client stub that rejects Groq calls and serves Gemini."""

        requested_urls: list[str] = []

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def __aenter__(self) -> DummyAsyncClient:
            DummyAsyncClient.requested_urls = []
            return self

        async def __aexit__(
            self,
            _exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _tb: object | None,
        ) -> None:
            return None

        async def post(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            json: dict[str, object] | None = None,
        ) -> httpx.Response:
            DummyAsyncClient.requested_urls.append(url)
            assert json is not None
            assert headers is None
            assert url.startswith(chat._GEMINI_GENERATE_URL)
            return httpx.Response(
                status_code=200,
                request=httpx.Request("POST", url),
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": "The local catalog cache shows ENGL 1301 for 3 credit hours.",
                                    }
                                ]
                            }
                        }
                    ]
                },
            )

    # Arrange
    monkeypatch.setenv("GROQ_API_KEYS", "")
    monkeypatch.setenv("GEMINI_API_KEYS", "gemini-key-one")
    monkeypatch.setattr(chat, "_get_optimized_catalog_context", lambda _message: '{"programs":[]}')
    monkeypatch.setattr(chat.httpx, "AsyncClient", DummyAsyncClient)

    # Act
    response_model: chat.ChatResponse = await chat._generate_chat_reply("Tell me about ENGL 1301")

    # Assert
    assert response_model.reply == "The local catalog cache shows ENGL 1301 for 3 credit hours."
    assert response_model.model == "gemini/gemini-1.5-flash"
    assert DummyAsyncClient.requested_urls == [f"{chat._GEMINI_GENERATE_URL}?key=gemini-key-one"]


@pytest.mark.anyio
async def test_generate_chat_reply_fails_without_provider_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing Groq and Gemini keys must surface as a 503 configuration error."""
    # Arrange
    monkeypatch.delenv("GROQ_API_KEYS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.setattr(chat, "_get_optimized_catalog_context", lambda _message: '{"programs":[]}')

    # Act / Assert
    with pytest.raises(HTTPException) as exc_info:
        await chat._generate_chat_reply("degree plan requirements")

    assert exc_info.value.status_code == 503


@pytest.mark.anyio
async def test_out_of_bounds_query_short_circuits_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Out-of-bounds queries must be contained locally without provider token usage."""

    class DummyAsyncClient:
        """Fail immediately if network calls are attempted for out-of-bounds input."""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def __aenter__(self) -> DummyAsyncClient:
            return self

        async def __aexit__(
            self,
            _exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _tb: object | None,
        ) -> None:
            return None

        async def post(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            json: dict[str, object] | None = None,
        ) -> httpx.Response:
            pytest.fail(f"Provider call must not occur for out-of-bounds query: {url}")

    # Arrange
    monkeypatch.setattr(chat.httpx, "AsyncClient", DummyAsyncClient)

    # Act
    response_model: chat.ChatResponse = await chat._generate_chat_reply("write a poem about rain")

    # Assert
    assert response_model.model == "guardrail/local"
    assert "Dallas College academic advising topics" in response_model.reply


def test_context_slicer_isolates_program_by_keyword(tmp_path: Path) -> None:
    """Keyword-specific queries must isolate to the matched program payload only."""
    # Arrange
    cache_path: Path = tmp_path / "catalog_mvp.json"
    cache_path.write_text(
        json.dumps(
            {
                "programs": [
                    {
                        "program_id": "Web_Development_Certificate",
                        "title": "Web Development Certificate",
                        "total_hours": 30,
                        "semesters": [
                            {
                                "name": "Certificate Core",
                                "courses": [
                                    {
                                        "code": "ITSE 1301",
                                        "title": "Web Design Tools",
                                        "credits": "3",
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "program_id": "Cybersecurity_AAS",
                        "title": "Cybersecurity AAS",
                        "total_hours": 60,
                        "semesters": [
                            {
                                "name": "Semester 1",
                                "courses": [
                                    {
                                        "code": "ITNW 1358",
                                        "title": "Network Plus",
                                        "credits": "3",
                                    }
                                ],
                            }
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    search_engine: chat.CatalogSearchEngine = chat.CatalogSearchEngine(cache_path=cache_path)

    # Act
    context: str = search_engine.get_optimized_context("web developer requirements")

    # Assert
    assert "Web_Development_Certificate" in context
    assert "ITSE 1301" in context
    assert "Cybersecurity_AAS" not in context
    assert "ITNW 1358" not in context
    assert "VERIFIED COURSE CODE WHITELIST (STRICT COMPLIANCE REQUIRED):" in context


def test_expand_user_query_appends_cluster_targets() -> None:
    """Cluster-mapped keywords must append deterministic prefix targets."""
    # Arrange / Act
    expanded_terms: list[str] = chat.expand_user_query("programming options")

    # Assert
    assert expanded_terms[0] == "programming options"
    assert "itse" in expanded_terms
    assert "inew" in expanded_terms
    assert "software" in expanded_terms


def test_expand_user_query_supports_real_estate_aliases() -> None:
    """Both spaced and unspaced real-estate queries must expand to catalog targets."""
    # Arrange / Act
    spaced_terms: list[str] = chat.expand_user_query("real estate listings")
    compact_terms: list[str] = chat.expand_user_query("realestate listings")

    # Assert
    assert "rele" in spaced_terms
    assert "rele" in compact_terms
    assert "bmgt" in spaced_terms
    assert "busi" in compact_terms


def test_context_slicer_matches_program_via_expanded_course_prefix(tmp_path: Path) -> None:
    """Expanded query terms must match against course prefixes in RAG gatherer logic."""
    # Arrange
    cache_path: Path = tmp_path / "catalog_mvp.json"
    cache_path.write_text(
        json.dumps(
            {
                "programs": [
                    {
                        "program_id": "Web_Development_Certificate",
                        "title": "Web Development Certificate",
                        "total_hours": 30,
                        "semesters": [
                            {
                                "name": "Certificate Core",
                                "courses": [
                                    {
                                        "code": "ITSE 1301",
                                        "title": "Web Design Tools",
                                        "credits": "3",
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "program_id": "Cybersecurity_AAS",
                        "title": "Cybersecurity AAS",
                        "total_hours": 60,
                        "semesters": [
                            {
                                "name": "Semester 1",
                                "courses": [
                                    {
                                        "code": "ITNW 1358",
                                        "title": "Network Plus",
                                        "credits": "3",
                                    }
                                ],
                            }
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    search_engine: chat.CatalogSearchEngine = chat.CatalogSearchEngine(cache_path=cache_path)

    # Act
    context: str = search_engine.get_optimized_context("programming certificate options")

    # Assert
    assert "Web_Development_Certificate" in context
    assert "ITSE 1301" in context
    assert "Cybersecurity_AAS" not in context
    assert "ITNW 1358" not in context


def test_targeted_context_uses_program_source_url_token(tmp_path: Path) -> None:
    """Targeted context must append source token using direct program source URL when present."""
    # Arrange
    cache_path: Path = tmp_path / "catalog_mvp.json"
    cache_path.write_text(
        json.dumps(
            {
                "programs": [
                    {
                        "program_id": "Web_Development_Certificate",
                        "title": "Web Development Certificate",
                        "source_url": "https://catalog.example.edu/program/web",
                        "semesters": [
                            {
                                "name": "Core",
                                "courses": [
                                    {
                                        "code": "ITSE 1301",
                                        "title": "Web Design Tools",
                                        "credits": "3",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    search_engine: chat.CatalogSearchEngine = chat.CatalogSearchEngine(cache_path=cache_path)

    # Act
    context: str = search_engine.get_optimized_context("web requirements")

    # Assert
    assert "[Catalog Source Verification Link: https://catalog.example.edu/program/web]" in context
    assert "[Course Verification Link for ITSE 1301:" in context
    assert "VERIFIED COURSE CODE WHITELIST (STRICT COMPLIANCE REQUIRED): ['ITSE 1301']" in context


def test_targeted_context_generates_fallback_source_url_token(tmp_path: Path) -> None:
    """Targeted context must append fallback advanced-search source URL when direct URL is absent."""
    # Arrange
    cache_path: Path = tmp_path / "catalog_mvp.json"
    cache_path.write_text(
        json.dumps(
            {
                "programs": [
                    {
                        "program_id": "Welding_Certificate",
                        "title": "Welding Technology",
                        "semesters": [
                            {
                                "name": "Core",
                                "courses": [
                                    {
                                        "code": "WLDG 1313",
                                        "title": "Introduction to Blueprint Reading",
                                        "credits": "3",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    search_engine: chat.CatalogSearchEngine = chat.CatalogSearchEngine(cache_path=cache_path)

    # Act
    context: str = search_engine.get_optimized_context("welding technology program")

    # Assert
    expected_url: str = (
        "https://catalog.dallascollege.edu/preview_program.php?m=Programs&"
        "keyword=Welding+Technology"
    )
    assert f"[Catalog Source Verification Link: {expected_url}]" in context


def test_targeted_context_generates_real_estate_fallback_source_url_token(tmp_path: Path) -> None:
    """Real-estate fallback links must normalize to the requested advanced-search URL."""
    # Arrange
    cache_path: Path = tmp_path / "catalog_mvp.json"
    cache_path.write_text(
        json.dumps(
            {
                "programs": [
                    {
                        "program_id": "Real_Estate_Certificate",
                        "title": "Real Estate",
                        "semesters": [
                            {
                                "name": "Core",
                                "courses": [
                                    {
                                        "code": "RELE 1300",
                                        "title": "Principles of Real Estate",
                                        "credits": "3",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    search_engine: chat.CatalogSearchEngine = chat.CatalogSearchEngine(cache_path=cache_path)

    # Act
    context: str = search_engine.get_optimized_context("realestate program")

    # Assert
    expected_url: str = (
        "https://catalog.dallascollege.edu/preview_program.php?m=Programs&"
        "keyword=Real+Estate"
    )
    assert f"[Catalog Source Verification Link: {expected_url}]" in context


def test_context_slicer_matches_by_course_header_lookup(tmp_path: Path) -> None:
    """Course-header regex extraction must map directly to its program context."""
    # Arrange
    cache_path: Path = tmp_path / "catalog_mvp.json"
    cache_path.write_text(
        json.dumps(
            {
                "programs": [
                    {
                        "program_id": "Chemistry_AAS",
                        "title": "Chemistry AAS",
                        "semesters": [
                            {
                                "name": "Semester 1",
                                "courses": [
                                    {
                                        "code": "CHEM 1411",
                                        "title": "General Chemistry I",
                                        "credits": "4",
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "program_id": "Business_Administration_AAS",
                        "title": "Business Administration AAS",
                        "semesters": [
                            {
                                "name": "Semester 1",
                                "courses": [
                                    {
                                        "code": "BUSI 1301",
                                        "title": "Business Principles",
                                        "credits": "3",
                                    }
                                ],
                            }
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    search_engine: chat.CatalogSearchEngine = chat.CatalogSearchEngine(cache_path=cache_path)

    # Act
    context: str = search_engine.get_optimized_context("show CHEM 1411 prerequisites")

    # Assert
    assert "Chemistry_AAS" in context
    assert "CHEM 1411" in context
    assert "Business_Administration_AAS" not in context


def test_matched_keywords_include_direct_course_lookup_url() -> None:
    """Extracted course codes must emit a precise direct lookup URL for catalog navigation."""
    # Arrange / Act
    matched_keywords: list[str] = chat._get_catalog_search_engine().get_matched_keywords(
        "Tell me about CHEF 1301 prerequisites"
    )

    # Assert
    assert "CHEF 1301" in matched_keywords
    assert (
        "https://catalog.dallascollege.edu/search_advanced.php?cur_cat_oid=5&"
        "search_keyword=CHEF+1301"
    ) in matched_keywords


def test_context_slicer_bounds_token_budget_on_generic_queries(tmp_path: Path) -> None:
    """Generic queries must return a slim index map within the configured budget."""
    # Arrange
    cache_path: Path = tmp_path / "catalog_mvp.json"
    cache_path.write_text(
        json.dumps(
            {
                "programs": [
                    {
                        "program_id": "Computer_Information_Technology_AAS",
                        "title": "Computer Information Technology AAS",
                        "total_hours": 60,
                        "semesters": [
                            {
                                "name": "Semester 1",
                                "courses": [
                                    {
                                        "code": "BCIS 1305",
                                        "title": "Business Computer Applications",
                                        "credits": "3",
                                    },
                                    {
                                        "code": "ITSC 1309",
                                        "title": "Integrated Software Applications I",
                                        "credits": "3",
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "program_id": "Cybersecurity_AAS",
                        "title": "Cybersecurity AAS",
                        "total_hours": 60,
                        "semesters": [
                            {
                                "name": "Semester 1",
                                "courses": [
                                    {
                                        "code": "ITNW 1358",
                                        "title": "Network Plus",
                                        "credits": "3",
                                    }
                                ],
                            }
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    char_budget: int = 220
    search_engine: chat.CatalogSearchEngine = chat.CatalogSearchEngine(
        cache_path=cache_path,
        char_budget=char_budget,
    )

    # Act
    context: str = search_engine.get_optimized_context("what classes are available")

    # Assert
    assert '"mode":"catalog_index_signature"' in context or '"truncated":true' in context
    assert len(context) <= char_budget


def test_programs_include_continuing_education_container(tmp_path: Path) -> None:
    """Root-level continuing_education_programs must be included in normalized program iteration."""
    # Arrange
    cache_path: Path = tmp_path / "catalog_mvp.json"
    cache_path.write_text(
        json.dumps(
            {
                "programs": [
                    {
                        "program_id": "Web_Development_Certificate",
                        "title": "Web Development Certificate",
                        "semesters": [],
                    }
                ],
                "continuing_education_programs": [
                    {
                        "program_id": "CE_Business_Operations_Accelerator",
                        "title": "Business Operations Accelerator",
                        "tracks": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    search_engine: chat.CatalogSearchEngine = chat.CatalogSearchEngine(cache_path=cache_path)

    # Act
    program_ids: set[str] = {str(program.get("program_id")) for program in search_engine._programs()}

    # Assert
    assert "Web_Development_Certificate" in program_ids
    assert "CE_Business_Operations_Accelerator" in program_ids


def test_prerequisite_index_reports_missing_dependencies(tmp_path: Path) -> None:
    """Prerequisite evaluator must surface locked courses for incomplete pathways."""
    # Arrange
    cache_path: Path = tmp_path / "catalog_mvp.json"
    cache_path.write_text(
        json.dumps(
            {
                "programs": [
                    {
                        "program_id": "Web_Development_Certificate",
                        "title": "Web Development Certificate",
                        "semesters": [
                            {
                                "name": "Core",
                                "courses": [
                                    {
                                        "code": "ITSE 1401",
                                        "title": "Web Foundations",
                                        "credits": "4",
                                    },
                                    {
                                        "code": "ITSE 2302",
                                        "title": "Advanced Web",
                                        "credits": "3",
                                        "prerequisites": "Prerequisite: ITSE 1401 or equivalent",
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    search_engine: chat.CatalogSearchEngine = chat.CatalogSearchEngine(cache_path=cache_path)

    # Act
    missing: dict[str, list[str]] = search_engine.get_missing_prerequisites(
        completed_courses=[],
        target_program="Web_Development_Certificate",
    )

    # Assert
    assert missing == {"ITSE 2302": ["ITSE 1401"]}


@pytest.mark.anyio
async def test_degree_layout_response_includes_prerequisite_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Degree layout responses must include prerequisite_tree metadata for UI enforcement."""
    # Arrange
    async def _no_op_log(
        query: str,
        intent: str,
        triggered_guardrail: bool,
    ) -> None:
        _ = (query, intent, triggered_guardrail)
        return None

    monkeypatch.setattr(chat, "_get_degree_progress_cards", lambda _message: [{"program_id": "Web_Development_Certificate", "title": "Web Development", "courses": []}])
    monkeypatch.setattr(chat, "_get_degree_prerequisite_tree", lambda _message: {"ITSE 2302": ["ITSE 1401"]})
    monkeypatch.setattr(chat, "log_analytics_event", _no_op_log)

    class DummySearchEngine:
        def classify_intent(self, _user_query: str) -> str:
            return "DEGREE_LAYOUT"

        def get_matched_keywords(self, _user_query: str) -> list[str]:
            return []

    monkeypatch.setattr(chat, "_get_catalog_search_engine", lambda: DummySearchEngine())

    # Act
    response_model: chat.ChatResponse = await chat._generate_chat_reply("show my degree plan")

    # Assert
    assert response_model.model == "catalog/local"
    assert response_model.prerequisite_tree == {"ITSE 2302": ["ITSE 1401"]}
