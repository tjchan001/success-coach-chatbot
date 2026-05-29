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
    """Both provider payloads must share the same strict prompt and temperature 0.0."""
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
    assert "Greetings, I am the automated AI Academic Advisor for Dallas College." in system_prompt
    assert "Strict zero-tolerance hallucination lock" in system_prompt
    assert "Academic catalog context unavailable. Connection terminal error." in system_prompt
    assert "I cannot confirm that selection based on the current catalog data." in system_prompt
    assert catalog_payload in system_prompt
    assert groq_request.temperature == 0.0
    assert groq_request.top_p == 1.0
    assert groq_request.messages[0].content == system_prompt
    assert gemini_request.generation_config["temperature"] == 0.0
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
                assert json["temperature"] == 0.0
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
    monkeypatch.setattr(chat, "_load_catalog_prompt_payload", lambda: '{"programs":[]}')
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
                "temperature": 0.0,
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
    monkeypatch.setattr(chat, "_load_catalog_prompt_payload", lambda: '{"programs":[]}')
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
    monkeypatch.setattr(chat, "_load_catalog_prompt_payload", lambda: '{"programs":[]}')
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
    monkeypatch.setattr(chat, "_load_catalog_prompt_payload", lambda: '{"programs":[]}')

    # Act / Assert
    with pytest.raises(HTTPException) as exc_info:
        await chat._generate_chat_reply("Hello")

    assert exc_info.value.status_code == 503
