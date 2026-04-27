# Contributing

Most users will only ever need the [web frontend](https://babyhuey.github.io/playtestproxy-fill/) — no install, paste a deck URL, download the ZIP. This file is for people who want to hack on the code.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r dev-requirements.txt
.venv/bin/playwright install chromium    # only needed for upload.py
.venv/bin/pre-commit install
```

`pre-commit install` wires ruff + a couple of file-hygiene hooks so they run on every commit and your PR doesn't bounce on whitespace.

## Running things locally

| Command | What it does |
|---|---|
| `pytest` | Run the test suite (offline, mocked HTTP). |
| `pytest --cov` | With coverage output. |
| `ruff check fill.py upload.py tests` | Lint. |
| `ruff format fill.py upload.py tests` | Auto-format. |
| `python -m http.server --directory docs 8000` | Serve the frontend locally. |
| `python fill.py <deck>` | Run the CLI against a real deck. |

## CI

Two workflows gate every PR:
- **CI** — Python lint/format/import-smoke + pytest with coverage, plus `node --check` on the static frontend.
- **CodeQL** — security + maintainability scan on Python and JS, weekly on `main`.

Branch protection on `main` requires both green before merge.

## Dependabot

Patch + minor updates auto-merge once CI passes (see `.github/workflows/dependabot-auto-merge.yml`). Major version bumps get a comment and stay open for review.

## Pull requests

Use the [PR template](.github/PULL_REQUEST_TEMPLATE.md) — it'll auto-populate. Keep changes focused; if you're touching the deck-source dispatcher, a regression test in `tests/test_fill.py` is appreciated.
