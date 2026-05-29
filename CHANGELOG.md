# Changelog

All notable changes to this project are documented in this file.

## 2026-05-28

### Changed
- Updated the advisor system prompt in [api/chat.py](api/chat.py) to enforce sovereign deterministic response rules, strict context-only reasoning, and exact guardrail fallback strings.
- Added prompt assertions in [tests/test_chat.py](tests/test_chat.py) to lock the required policy text and fallback literals.
- Corrected Groq model expectation in [tests/test_chat.py](tests/test_chat.py) to match the configured `llama-3.1-8b-instant` identifier.
- Added mandatory automated-AI governance greeting protocol in [api/chat.py](api/chat.py), including the exact initial greeting string and explicit exception handling for exact fallback/guardrail outputs.
- Expanded deterministic prompt tests in [tests/test_chat.py](tests/test_chat.py) to lock AI-governance and mandatory greeting contract text.
- Refactored [scripts/scraper.py](scripts/scraper.py) CLI execution to scrape three catalog pathways into one multi-program payload, adding explicit `program_id` metadata per program and persisting via atomic writes to `data/catalog_mvp.json`.
- Added scraper helper tests in [tests/test_scraper.py](tests/test_scraper.py) for multi-program payload assembly and atomic JSON write integrity.
- Updated [public/widget.js](public/widget.js) response rendering to support constrained markdown formatting (`**bold**`, dash bullets) via a zero-dependency `formatMarkdown(text)` pipeline and bot-message `innerHTML` injection from sanitized content.
