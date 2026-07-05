# Contributing

Most users will only ever need the [web frontend](https://babyhuey.github.io/playtestproxy-fill/) — no install, paste a deck URL, download the ZIP. This file is for people who want to hack on the code.

## Setup

Dependencies are managed with [Poetry](https://python-poetry.org/):

```bash
pipx install poetry                       # or: pip install --user poetry
poetry install                            # runtime + dev deps
poetry run playwright install chromium    # for upload.py + the frontend cache test
poetry run pre-commit install
```

`pre-commit install` wires ruff, a couple of file-hygiene hooks, and `pytest` so they run on every commit and your PR doesn't bounce on a lint nit or a unit-test regression. The pytest hook auto-skips the Playwright frontend cache test on machines that don't have `playwright` installed, so a fresh clone still passes — install Chromium (above) if you want the full local coverage CI runs.

## Running things locally

Prefix commands with `poetry run` (or drop into the env once with `poetry shell`):

| Command | What it does |
|---|---|
| `poetry run pytest` | Run the test suite (offline, mocked HTTP). |
| `poetry run pytest --cov` | With coverage output. |
| `poetry run ruff check fill.py upload.py tests` | Lint. |
| `poetry run ruff format fill.py upload.py tests` | Auto-format. |
| `poetry run python -m http.server --directory docs 8000` | Serve the frontend locally. |
| `poetry run python fill.py <deck>` | Run the CLI against a real deck. |

## CI

Two workflows run on every PR:
- **CI** — Python lint/format/import-smoke + pytest with coverage, plus `node --check` and asset-presence on the static frontend.
- **CodeQL** — security + maintainability scan on Python and JS; also runs weekly on `main` (Monday 14:37 UTC).

Both must be green before merge — branch protection on `main` requires the **Python — lint + import smoke test** and **Frontend — syntax + presence checks** contexts to pass. Force-pushes and branch deletion on `main` are disabled. Admins aren't bound by the rule, so emergency overrides are still possible from the GitHub UI.

## Dependabot

Patch + minor updates auto-merge once CI passes (see `.github/workflows/dependabot-auto-merge.yml`). Major version bumps get a comment and stay open for review.

## Pull requests

Use the [PR template](.github/PULL_REQUEST_TEMPLATE.md) — it'll auto-populate. Keep changes focused; if you're touching the deck-source dispatcher, a regression test in `tests/test_fill.py` is appreciated.
