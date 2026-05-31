# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.3.3] - 2026-05-31

### Fixed
- Updated supplemental semantic program matching in [api/chat.py](api/chat.py) to allow partial-title resolution (for example, `Veterinary Technology` now correctly matches `Veterinary Technology A.A.S.` and related title variants).
- Hardened course keyword retrieval in [api/chat.py](api/chat.py) with strict multi-word phrase filtering so intents like `video editing` and `audio engineering` do not bleed into unrelated departments.

### Changed
- Added department-prefix validation (`FLMC`, `RTVB`, `COMM` for video; `MUSC` for audio) as a guarded fallback when exact multi-word skill phrases are absent from course text.

## [0.3.2] - 2026-05-31

### Changed
- Rehydrated isolated course lookups in [api/chat.py](api/chat.py) so matching course codes now pull parent program and campus metadata into the retrieval context.

### Added
- Added semantic supplementation for animal medicine queries so vet-related searches inherit Veterinary Technology program context.

### Fixed
- Fixed course-only retrieval paths that previously returned program-level context without an explicit `PROGRAM:` / `CAMPUSES:` / `COURSE DETAILS:` fragment header.

## [0.3.1] - 2026-05-30

### Added
- Added [patch_locations.py](patch_locations.py) to repair empty campus arrays for core demo programs, including Nursing, Vet Tech, Criminal Justice, and Web Development.

### Changed
- Upgraded [seed_supabase.py](seed_supabase.py) and [embed_pathways.py](embed_pathways.py) so location metadata is baked directly into the text chunks as `Offered at Campuses` before insertion and embedding.
- Standardized campus-aware pathway text generation across the full 305-program catalog so every offering can participate in location-aware vector retrieval.

### Fixed
- Added deterministic fallback text (`Online / General Catalog`) to prevent empty campus arrays and NULL-style failures when location data is missing.

## [0.3.0] - 2026-05-30

### Added
- Implemented robust fallback safety inside credential parsing routines (`_load_groq_api_keys`, `_load_gemini_api_keys`) supporting both:
  - singular keys: `GROQ_API_KEY`, `GEMINI_API_KEY`
  - plural keys: `GROQ_API_KEYS`, `GEMINI_API_KEYS` (comma-separated)
- Enhanced `HTTPException` logging to surface upstream failures (`502`, `503`) directly in server logs.

### Changed
- Removed misleading global fallback message ("I encountered an optimization bottleneck...") that masked real errors.
- Hardened `/api/chat` exception handling to differentiate:
  - credential/config errors
  - runtime engine failures

### Fixed
- Fixed deployment failure where singular `GROQ_API_KEY` caused silent fallback loops due to strict plural parsing expectation.

---

## [0.2.0] - 2026-05-29

### Added
- Added [sql/local_embedding_setup.sql](sql/local_embedding_setup.sql) for 384-d local embedding DB migration.
- Added [sql/native_embedding_setup.sql](sql/native_embedding_setup.sql) for Supabase native embeddings with triggers and RPC.
- Added [sql/match_pathways.sql](sql/match_pathways.sql) with pgvector and cosine similarity RPC.

- Added [rag_search.py](rag_search.py) for Groq + Supabase semantic retrieval runtime.
- Added [tests/test_rag_search.py](tests/test_rag_search.py) covering RAG orchestration pipeline.

- Added [embed_pathways.py](embed_pathways.py) production embedding pipeline.
- Added [tests/test_embed_pathways.py](tests/test_embed_pathways.py) with embedding + update coverage.

- Added [crawl_dallas_college.py](crawl_dallas_college.py) production catalog crawler.
- Added crawler dependencies (`requests`, `beautifulsoup4`).

- Added strict course-code whitelist enforcement:
  - context injection: `VERIFIED COURSE CODE WHITELIST`
  - generation restricted to known codes only

- Added career-cluster synonym expansion via `CAREER_CLUSTER_MAP`
- Added real estate aliases (RELE, BUSI, BMGT)

### Changed
- Introduced strict anti-hallucination rules (prefix alignment, no fabrication, honest no-match responses)
- Enforced citation format `[n](URL)` with zero tolerance for variation
- Required mandatory disclaimer blockquote prefix in every response

- Rewrote embedding system:
  - replaced OpenAI with `sentence-transformers (all-MiniLM-L6-v2)`
  - shifted to 384-d vector standard

- Reworked:
  - `rag_search.py` → local embeddings
  - all related tests mocked for local inference

- Updated dependencies:
  - removed `openai`
  - added `sentence-transformers`, `torch`, `groq`

- Fully refactored `seed_supabase.py`:
  - switched from PostgreSQL to HTTPS via Supabase client
  - enforced schema contract correctness
  - added 200-row batch upserts

- Updated all test suites for:
  - schema alignment
  - batching logic
  - prompt contract enforcement

- Tuned inference:
  - `temperature=0.3`
  - `frequency_penalty=0.7`
  - `presence_penalty=0.5`

- Implemented deep linking system:
  - program preview URLs
  - dynamic keyword linking
  - per-course verification tokens

- Strengthened output integrity:
  - exact-record course enforcement
  - zero hallucinated codes allowed

- Upgraded catalog:
  - prerequisite arrays added (CHEF 1301, PSTR 2331, BUSI 2301)
  - added `continuing_education_programs`

- Improved context engine:
  - per-course verification links
  - CE container parsing
  - prerequisite flow rendering (`──>`) logic

- Updated frontend:
  - widget greeting + disclaimer alignment
  - improved context binding for program/source URLs

- Replaced index landing UI with Dallas AI Club banner

- Enhanced query matching:
  - expanded-term matching against title, ID, course code
  - defensive exception handling in loops

- Expanded citation system:
  - dynamic `[Catalog Source Verification Link]` tokens
  - footer citation extraction from context
  - fallback URL generation when source missing

- Completed governance layer:
  - mandatory greeting
  - legal disclaimer enforcement
  - domain-specific citation requirements

### Production & Server Infrastructure
- Excluded heavy Python dev directories from Vercel serverless bundle (fixed build limits)
- Authorized Render production domain in CSP `connect-src`
- Updated `public/widget.js` to point directly to live Render backend API

---

## [0.1.0] - 2026-05-28

### Changed
- Enforced deterministic advisor system prompt rules (strict context-only outputs)
- Added test assertions locking fallback strings and governance behavior
- Corrected Groq model reference to `llama-3.1-8b-instant`

- Implemented mandatory AI governance greeting protocol

- Refactored scraper:
  - multi-program extraction
  - atomic writes
  - `program_id` metadata support

- Added test coverage for scraper integrity

- Frontend improvements:
  - markdown rendering (`**bold**`, lists)
  - sanitized HTML injection

- Implemented certificate fallback extraction:
  - supports flat layouts
  - regex-based global scan (`^[A-Z]{4} \d{4}`)

- Added nested structure handling + regression tests

- Introduced `CatalogSearchEngine`:
  - query-based context slicing
  - intent routing

- Replaced frontend startup init with FastAPI lifespan manager

- Added frontend features:
  - session persistence (`localStorage`)
  - chat history rehydration
  - progress-card UI (interactive checklist)

- Added backend safety systems:
  - out-of-scope request rejection
  - structured `progress_cards` responses

- Implemented prerequisite engine:
  - dependency tracking
  - prerequisite tree output

- Added anonymized analytics logging

- Optimized broad-query routing (lightweight catalog signature)

- UI improvements:
  - skeleton loading
  - smooth scroll
  - prerequisite enforcement feedback

- Updated scraper root index:
  - made catalog version-independent

- Hardened backend:
  - top-level exception shield
  - standardized JSON error outputs

- Frontend error handling:
  - fallback to raw text when JSON fails

- Added routing resilience:
  - `/api/chat` and `/api/chat/`

- Enabled permissive CORS for diagnostics

- Added diagnostics logging for API calls

- Added `/api/test-routing` probe endpoint

- Optimized startup:
  - lazy prerequisite indexing
  - defensive skipping of malformed data

- Strengthened runtime safety:
  - catch-all search exception handler
  - System-Fallback-Shield output

- Prevented startup crashes:
  - removed eager engine initialization
  - guarded `httpx` import

- Hardened catalog iteration:
  - strict type checks
  - safe `.get()` access

- Redirected analytics logs:
  - writable temp storage (`/tmp`) for serverless environments