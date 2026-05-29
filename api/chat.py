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

import json
import logging
import os
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

_LOG: logging.Logger = logging.getLogger(__name__)

_APP_TITLE: str = "Dallas College Chatbot API"
_APP_VERSION: str = "0.3.0"
_CATALOG_CACHE_PATH: Path = Path(__file__).resolve().parent.parent / "data" / "catalog_mvp.json"
_HTTP_TIMEOUT_SECONDS: float = 30.0

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


def _parse_cors_origins() -> list[str]:
    """Return the configured CORS origin list from the environment.

    Returns:
        A list of origin strings suitable for FastAPI's CORS middleware.
    """
    raw_origins: str = os.environ.get("CORS_ALLOWED_ORIGINS", "*")
    origins: list[str] = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    return origins or ["*"]


app = FastAPI(
    title=_APP_TITLE,
    description="Hybrid Groq and Gemini catalog-grounded Dallas College advising API.",
    version=_APP_VERSION,
)

_CORS_ORIGINS: list[str] = _parse_cors_origins()
_ALLOW_CREDENTIALS: bool = _CORS_ORIGINS != ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=_ALLOW_CREDENTIALS,
    allow_methods=["GET", "POST", "OPTIONS"],
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
    """

    model_config = ConfigDict(frozen=True)

    reply: str = Field(..., description="Plain-text grounded response returned to the widget.")
    model: str = Field(..., description="Provider/model pair used to generate the response.")


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
    try:
        catalog_payload: str = _load_catalog_prompt_payload()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

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


@app.post("/api/chat", response_model=ChatResponse, status_code=status.HTTP_200_OK)
async def chat(request: ChatRequest) -> ChatResponse:
    """Handle a widget message through the hybrid Groq-to-Gemini cascade.

    Args:
        request: Validated incoming chat request.

    Returns:
        A grounded assistant reply generated from the local catalog payload.
    """
    return await _generate_chat_reply(message=request.message)
