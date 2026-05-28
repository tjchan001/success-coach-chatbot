# Dallas College Chatbot — Coding Standards

> **Version:** 2026.05 | **Enforced by:** Ruff + PyTest CI gate

---

## 1. Language & Runtime

- **Python 3.11+** is the only supported runtime.
- All source files must begin with `from __future__ import annotations` to enable PEP 563 deferred evaluation.
- No third-party package may be added without a corresponding entry in `requirements.txt` and a documented reason in the PR description.

---

## 2. Style & Formatting (PEP 8 + Ruff)

- **Ruff** is the sole formatter and linter. `black`, `isort`, and `flake8` are banned.
- Line length: **100 characters** (configured in `pyproject.toml`).
- Indentation: **4 spaces** — no tabs, ever.
- Trailing commas **required** on all multi-line collections, function signatures, and import groups.
- String literals: prefer **double quotes** (`"`). Single quotes are permissible only inside f-strings to avoid escaping.
- Imports are ordered: stdlib → third-party → local, separated by a blank line. Never use wildcard imports (`from module import *`).

### Ruff rule-set (minimum)

```toml
[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = [
    "E",   # pycodestyle errors
    "W",   # pycodestyle warnings
    "F",   # pyflakes
    "I",   # isort
    "UP",  # pyupgrade
    "B",   # flake8-bugbear
    "C4",  # flake8-comprehensions
    "SIM", # flake8-simplify
    "TCH", # flake8-type-checking
    "ANN", # flake8-annotations (enforce return types)
]
ignore = ["ANN101", "ANN102"]  # skip `self` / `cls` annotations
```

---

## 3. Type Annotations

- **All** function parameters and return values must carry explicit type annotations. `Any` is forbidden unless accompanied by a `# noqa: ANN401` suppression comment and a written justification.
- Use built-in generics (`list[str]`, `dict[str, int]`) — **never** `List`, `Dict`, `Tuple` from `typing`.
- Union types are written with the `|` operator (`str | None`) — **never** `Optional[str]`.
- Pydantic models must use **Pydantic v2** (`from pydantic import BaseModel, Field`). v1 compatibility shims are banned.

---

## 4. Docstrings (Google Style)

Every public module, class, and function requires a docstring.

```python
def parse_degree_page(url: str) -> DegreePlan:
    """Fetch and parse a Dallas College degree-plan page.

    Args:
        url: Fully-qualified URL of the catalog degree page.

    Returns:
        A validated ``DegreePlan`` instance populated from the page HTML.

    Raises:
        httpx.HTTPStatusError: If the remote server returns a 4xx/5xx status.
        ValidationError: If the scraped data fails Pydantic schema validation.
    """
```

- One-line docstrings are acceptable for trivially obvious private helpers (`_`-prefixed).
- Do **not** restate what the signature already declares; explain **why** and **when**.

---

## 5. Testing (PyTest)

- Minimum coverage target: **85 %** (line + branch). CI fails below this threshold.
- Every new module must ship with a corresponding `tests/test_<module>.py` file in the same PR.
- Test structure: **Arrange / Act / Assert** — no logic inside assertions.
- Parametrize repetitive cases with `@pytest.mark.parametrize`.
- External HTTP calls must be mocked with `pytest-httpx` or `unittest.mock.patch`. No live network calls in the test suite.
- Fixtures live in `tests/conftest.py`. Do not redefine fixtures inline.

### Required pytest configuration (`pyproject.toml`)

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "--cov=. --cov-report=term-missing --cov-fail-under=85 -v"
```

---

## 6. Pydantic v2 Conventions

- Models are **immutable by default** (`model_config = ConfigDict(frozen=True)`).
- Field descriptors must include a `description` string for every field.
- Validators use `@field_validator` — the deprecated `@validator` is banned.
- Do not call `.dict()` — use `.model_dump()`.
- Do not call `.json()` — use `.model_dump_json()`.

---

## 7. Security

- Secrets (API keys, credentials) are stored **only** in environment variables. Never hard-code or commit them.
- User-supplied strings passed to shell commands must be validated against an allowlist. No `shell=True` in `subprocess` calls.
- HTML rendered in the widget must escape all user content — no raw `innerHTML` injection with untrusted data.
- Dependencies are pinned to exact versions in `requirements.txt` and audited weekly via `pip-audit`.

---

## 8. Commit & Changelog Discipline

- Every merged change requires a `CHANGELOG.md` entry under the appropriate category: `[Added]`, `[Changed]`, `[Fixed]`, `[Security]`.
- Commit messages follow **Conventional Commits**: `feat:`, `fix:`, `chore:`, `test:`, `docs:`.
- Direct pushes to `main` are prohibited. All changes flow through a reviewed pull request.
