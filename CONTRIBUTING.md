<p align="right">
  <a href="./CONTRIBUTING.ja.md">日本語</a> | <strong>English</strong>
</p>

# Contributing

Thanks for your interest in Artificial Girlfriend. Bug reports, feature requests and pull requests are all welcome — **in English or Japanese, whichever you prefer.**

This is a personal project, so replies and reviews may take a while. Thanks for your patience.

## Where to report and discuss

- **[Issues](https://github.com/ARP224/ArtificialGirlfriend/issues)** — bug reports and feature requests. Templates are provided, but the headings are only a guide; write freely and fill in what you can
- **[Discussions](https://github.com/ARP224/ArtificialGirlfriend/discussions)** — usage questions, casual chat and ideas

For bug reports, including your OS (Windows / Mac), the mode (local / server), what you did and what happened, plus what **📋 System Logs** showed or the relevant part of `logs/app.log` speeds up the investigation a lot. **Please redact private parts such as conversation content before posting.**

## Pull request policy

- **Small fixes (typos, obvious bug fixes) can be sent straight away**
- **For larger changes, please open an issue first.** Working in a direction that doesn't fit the project wastes your time as well as ours
- `master` is the distribution channel of this repository. Users receive `master` directly via `git pull`, so **merging is releasing**. Changes are merged carefully
- Changes that touch behavior are checked by the automated tests and an AI code review, and then verified by the maintainer actually running the app. This can take time. **Adding a short "do this, and this should happen" verification recipe to your PR speeds it up**

## Before opening a pull request

Your development environment is simply what the README's install steps set up — no extra setup is needed. With that in place:

- Run the test suite (no API keys needed; completes offline in under a minute):
  - Windows: `venv\Scripts\python.exe -m pytest tests/`
  - macOS: `venv/bin/python -m pytest tests/`
  - Success means **the row of dots and exit code 0** (in some environments the final summary line is not printed — that is normal)
- Run the linter on files you touched:
  - Windows: `venv\Scripts\python.exe -m ruff check <paths>`
  - macOS: `venv/bin/python -m ruff check <paths>`
- Do **not** set `git config core.autocrlf true`. This repository intentionally preserves per-file line endings (CRLF / LF). Normalizing them buries the diff in line-ending changes, making both review and history unreadable. Turn off your editor's automatic normalization as well

## Code conventions

Keeping to these four rules is all that's needed.

### 1. Layers depend downward only

```
ui/                                            UI layer (Gradio composition root app.py + handlers)
backend/backend.py, conversation_manager.py    App layer (public API boundary + conversation orchestration)
backend/server/                                Transport layer (WebSocket, sessions)
backend/llm|conversation|memory|tools|elyth/   Domain layer (side by side)
backend/shared/                                Shared layer (settings, constants, state)
```

Imports go one way, from an upper layer to a lower one. Never write the reverse (e.g. importing `ui/` from `backend/`).

### 2. Where new code goes

- New UI handlers → `ui/handlers/` (`ui/app.py` is wiring only — no logic there)
- New domain logic and tools → the matching package under `backend/llm|conversation|memory|tools|elyth/`
- WebSocket / transport → `backend/server/`
- Shared settings, constants and state → `backend/shared/`

### 3. Golden tests are a byte-level contract

`tests/golden/` holds byte-exact snapshots of the prompts and tool definitions sent to the LLM. A single character of difference can change the character's behavior there, which is why they are pinned byte for byte.

When a golden test turns red, that is a change that needs an explanation. Explain in your PR why the diff is intended. Regenerating the snapshots with `UPDATE_GOLDEN=1` to turn the tests green without an explanation is not acceptable.

### 4. Don't mix structural and behavioral changes

Keep file moves, renames and splits in separate commits from changes that alter behavior. When they are mixed, it becomes impossible to trace later which commit changed the behavior.

## License of contributions

By submitting a pull request you agree that your contribution is licensed under the **GNU Affero General Public License version 3**, the same license as the rest of this project (see `LICENSE`). You keep the copyright to your own work. There is no Contributor License Agreement to sign.

A few directories carry different licenses — `MotionPNGPlayer/`, `extras/ag_client_addon/` and `extras/chrome_extensions/AG Tab Reporter/` are MIT, and contributions to those directories are accepted under the license of the directory they touch. `nircmd-x64/` is third-party freeware that must not be modified. See `LICENSE` for the full scope.
