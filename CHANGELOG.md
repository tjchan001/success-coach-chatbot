# Changelog

All notable changes to this project are documented in this file.

## 2026-05-28

### Changed
- Updated the advisor system prompt in [api/chat.py](api/chat.py) to enforce sovereign deterministic response rules, strict context-only reasoning, and exact guardrail fallback strings.
- Added prompt assertions in [tests/test_chat.py](tests/test_chat.py) to lock the required policy text and fallback literals.
- Corrected Groq model expectation in [tests/test_chat.py](tests/test_chat.py) to match the configured `llama-3.1-8b-instant` identifier.
- Added mandatory automated-AI governance greeting protocol in [api/chat.py](api/chat.py), including the exact initial greeting string and explicit exception handling for exact fallback/guardrail outputs.
- Expanded deterministic prompt tests in [tests/test_chat.py](tests/test_chat.py) to lock AI-governance and mandatory greeting contract text.
