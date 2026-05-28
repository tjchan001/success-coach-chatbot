# 🛡️ Sovereign AI Governance (v2026.05)
# Dallas College Chatbot — AI Agent Coding Mandate

---

## 🏛️ ARCHITECTURAL GPS (JSON Precision)
- **Primary Rule:** Always cross-reference `ARCHITECTURE_MAP.json` (Live Signatures) against `ARCHITECTURE_REPORT.md` (Logic).
- **Conflict Resolution:** The JSON Map is the "Ground Truth." Notify the Architect of discrepancies immediately.
- **Surgical Context:** Request only the specific file needed. Do not ingest entire directories.

---

## 📜 COMPLIANCE & GOVERNANCE
- **Keep A Changelog:** Every successful task REQUIRES a `CHANGELOG.md` entry. Use categories: [Added, Changed, Fixed, Security].
- **The "Why" Standard:** All public methods must include a Google-style docstring documenting Architectural Intent (Security/Memory rationale).
- **Map Sync:** Every session MUST end with a call to `update_map.ps1` to refresh the JSON map.

---

## ⚙️ CODE GENERATION RULES — NON-NEGOTIABLE

### 1. Pydantic v2 Only
- All data models inherit from `pydantic.BaseModel`.
- Use `model_config = ConfigDict(frozen=True)` for immutability.
- Field descriptors require `description=` on every field.
- Call `.model_dump()` and `.model_dump_json()` — NEVER `.dict()` or `.json()`.
- Validators use `@field_validator` — the deprecated `@validator` decorator is BANNED.

### 2. Ruff Compliance
- All generated Python must pass `ruff check` and `ruff format` with zero warnings.
- Line length: 100 characters.
- Target version: `py311`.
- Required rule groups: E, W, F, I, UP, B, C4, SIM, TCH, ANN.
- Wildcard imports (`from module import *`) are BANNED.
- `List`, `Dict`, `Tuple` from `typing` are BANNED — use `list[str]`, `dict[str, int]` etc.
- Union types use `|` syntax — `Optional[X]` is BANNED.

### 3. Explicit Return Types on ALL Functions
- Every function and method — public or private — must declare an explicit return type.
- `-> None` must be written out; omitting it is treated as a linting error.
- `-> Any` requires a `# noqa: ANN401` suppression and a written justification comment.

### 4. Test-Driven Generation Mandate
- **No code may be generated without an accompanying unit test block.**
- Tests are written with PyTest and placed in `tests/test_<module>.py`.
- Each test must follow the Arrange / Act / Assert pattern.
- Minimum coverage target: **85 %** (line + branch). CI fails below this threshold.
- External HTTP or I/O calls must be mocked — no live network calls in the test suite.

### 5. Typing & Style
- All source files open with `from __future__ import annotations`.
- Google-style docstrings on all public modules, classes, and functions.
- Secrets must NEVER be hard-coded; read from environment variables only.

---

## 🚫 PROHIBITED PATTERNS
| Pattern | Reason |
|---|---|
| `Optional[X]` | Use `X \| None` instead |
| `List[X]`, `Dict[K,V]` | Use `list[X]`, `dict[K,V]` |
| `.dict()` / `.json()` on Pydantic models | Use `.model_dump()` / `.model_dump_json()` |
| Hard-coded secrets or API keys | Security violation — use env vars |
| `shell=True` in subprocess | Command-injection risk |
| Code generation without tests | Violates TDD mandate |
| `@validator` (Pydantic v1) | Use `@field_validator` (v2) |