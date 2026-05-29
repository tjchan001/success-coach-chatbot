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
- Added a dedicated flat-certificate fallback in [scripts/scraper.py](scripts/scraper.py) that scans `<tr>`/`<li>` rows matching course-code prefixes and maps them into a synthetic semester named `Certificate Core Requirements` when standard semester extraction returns empty.
- Added a certificate fallback regression test in [tests/test_scraper.py](tests/test_scraper.py) to lock schema-compatible output for flat certificate layouts.
- Refactored the certificate fallback in [scripts/scraper.py](scripts/scraper.py) to scan globally across anchor text and document text nodes using the anchored rubric regex `^[A-Z]{4}\s+\d{4}\b`, ensuring nested non-`tr`/`li` course elements are captured.
- Added nested-anchor regression coverage in [tests/test_scraper.py](tests/test_scraper.py) to validate `Certificate Core Requirements` extraction when course links are embedded in non-standard containers.
- Added `CatalogSearchEngine` in [api/chat.py](api/chat.py) for in-memory metadata-guided context slicing, including keyword-to-program intent routing and bounded context generation via `get_optimized_context(user_query)`.
- Refactored chat generation in [api/chat.py](api/chat.py) to inject query-optimized context snippets instead of full catalog dumps and updated metaprompt instructions to request pathway clarification when filtered slices lack exact answers.
- Added context slicer regression tests in [tests/test_chat.py](tests/test_chat.py): targeted program isolation by keyword and generic-query budget-bound index slicing.
- Replaced deprecated FastAPI `@app.on_event("startup")` initialization in [api/chat.py](api/chat.py) with an async lifespan context manager bound via `app = FastAPI(lifespan=lifespan)`.
- Added frontend session persistence in [public/widget.js](public/widget.js) using `localStorage` (`dc_chatbot_history`), including automatic re-hydration and a top-level clear-history control.
- Added dynamic interactive progress-card rendering in [public/widget.js](public/widget.js) for structured backend checklist payloads with per-course completion toggles persisted to history state.
- Added backend scope guardrails in [api/chat.py](api/chat.py) to short-circuit out-of-bounds non-academic requests with a local containment reply and no provider calls.
- Added structured `progress_cards` response support in [api/chat.py](api/chat.py) for explicit degree-layout requests, enabling UI checklist rendering of pathway course requirements.
- Added out-of-bounds containment regression coverage in [tests/test_chat.py](tests/test_chat.py) to verify boundary enforcement without upstream context/token spend.
- Added prerequisite dependency indexing and directed missing-prerequisite evaluation in [api/chat.py](api/chat.py) via `get_missing_prerequisites(completed_courses, target_program)` and degree-layout `prerequisite_tree` response payload support.
- Added asynchronous local anonymized analytics logging in [api/chat.py](api/chat.py) writing timestamped intent metadata to `data/analytics_logs.json` without storing raw query text.
- Compressed broad-query catalog routing in [api/chat.py](api/chat.py) into an ultra-light token signature map (`catalog_index_signature`) that strips extraneous whitespace and secondary metadata.
- Upgraded [public/widget.js](public/widget.js) with animated skeleton loading states, smooth scroll snapping, and prerequisite-aware progress-card checkbox enforcement with inline warnings and shake animation feedback.
- Added prerequisite regression coverage in [tests/test_chat.py](tests/test_chat.py) for dependency indexing and degree-layout `prerequisite_tree` emission.
- Updated discovery root index in [scripts/scraper.py](scripts/scraper.py) to `https://catalog.dallascollege.edu/content.php?catoid=4&navoid=944` and made program-link extraction catoid-agnostic by matching `preview_program.php` + `poid=` across catalog version rotations.
