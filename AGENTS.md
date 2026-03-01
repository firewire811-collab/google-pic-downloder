# Agents Guide (Repo-Specific)

This repository is a small Python/PyQt6 desktop app. There is no formal build/lint/test automation configured yet; follow the conventions in the existing code.

## Repo Layout

- `main.py`: app entry point.
- `app/`: application modules.
  - `app/main_window.py`: PyQt UI + orchestration.
  - `app/metadata.py`: network + HTML parsing to extract artwork metadata.
  - `app/db.py`: SQLite persistence.
  - `app/paths.py`: repo-relative data/download directories.
- `data/`: runtime data (e.g. `data/artworks.sqlite3`, `data/thumbs/`).
- `download/`: output images.
- `venv/`: local virtualenv (treat as generated; do not edit).

## Environment

- Python: this repo includes a `venv/` created with Python 3.13.x (`venv/pyvenv.cfg`).
- Dependencies: `requirements.txt` (runtime deps only).

## Build / Run / Test / Lint

### Install deps

```bash
python -m pip install -r requirements.txt
```

### Run app

```bash
python main.py
```

### Build

- No build step (no packaging/bundling config; app runs from source).

### Tests

- No test suite currently (no `tests/` and no pytest/unittest config).

If you add tests, keep them out of `venv/` and prefer one of:

- `unittest` (stdlib)
  - Run all tests:
    ```bash
    python -m unittest
    ```
  - Run one module:
    ```bash
    python -m unittest tests.test_metadata
    ```
  - Run one test:
    ```bash
    python -m unittest tests.test_metadata.TestMetadata.test_extract_year
    ```

- `pytest` (not installed by default)
  - Run all tests:
    ```bash
    pytest
    ```
  - Run one file:
    ```bash
    pytest tests/test_metadata.py
    ```
  - Run one test:
    ```bash
    pytest tests/test_metadata.py::test_extract_year
    ```

### Lint / Format / Type check

- No repo-configured tooling (no `pyproject.toml`, `ruff.toml`, `.flake8`, `.editorconfig`, etc.).

If you add tooling, prefer:
- `ruff` for lint/format + import sorting
- `pytest` for tests

## Cursor / Copilot Rules

- No `.cursor/rules/`, `.cursorrules`, or `.github/copilot-instructions.md` found in this repo.

## Coding Style (Inferred From Existing Code)

Follow the established patterns in `app/`.

### Formatting

- Indentation: 4 spaces.
- Prefer double quotes for strings.
- Use trailing commas in multiline imports/calls/collections.
- Wrap long calls using parentheses (Black-like style).

### Imports

Match the import structure used throughout `app/`:

1. `from __future__ import annotations` at the top of each module.
2. Standard library imports.
3. Third-party imports (`requests`, `PyQt6`, `bs4`).
4. Local imports (`from app...`).

Keep groups separated by a blank line.

### Naming

- Classes: `PascalCase` (e.g., `MainWindow`, `ArtworkDb`, `AssetMetadata`).
- Functions/methods/locals: `snake_case`.
- “Private” helpers/fields: single leading underscore (e.g., `_clean_text`, `self._db`).
- Constants: `UPPER_SNAKE_CASE` (e.g., `ASSET_PREFIX`).

### Types

- Type hint new functions/methods.
- Use modern typing (e.g., `Artwork | None`, `list[int]`) and keep `from __future__ import annotations`.
- Prefer `@dataclass` for simple records (see `app/db.py`, `app/metadata.py`).

### Error Handling

- For UI actions, catch exceptions at the boundary and notify the user (see `QMessageBox.warning(...)` in `app/main_window.py`).
- Avoid empty `except:`; catch `Exception` only when doing best-effort behavior (e.g., thumbnail cache write).
- When returning failure from helpers, return `False`/`None` rather than silently succeeding.

### I/O and Paths

- Use `pathlib.Path` everywhere (see `app/paths.py`).
- All repo-relative outputs go through `data_dir()`, `downloads_dir()`, `thumbs_dir()`.

### Networking / HTML Parsing

- Use `requests.get(..., timeout=...)` and call `raise_for_status()`.
- Set a reasonable `User-Agent` and `Accept-Language` header when scraping (see `app/metadata.py`).
- Prefer parsing JSON-LD / OG tags first; keep selectors as a targeted fallback.

### Database

- SQLite via stdlib `sqlite3`.
- Use parameterized queries (`?` placeholders) and keep schema creation in `_init_db()`.
- Prefer returning typed dataclasses (`Artwork`) from query results.

## Safe Agent Behavior (Repo Hygiene)

- Treat `venv/` as generated; do not modify, lint, or scan it for project conventions.
- Treat `data/` and `download/` as runtime outputs; avoid committing large binaries.
- Keep changes minimal and local to the feature/bug being addressed.
