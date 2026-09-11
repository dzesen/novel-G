# Novel-G

[中文文档](README.zh-CN.md) | English

**A local writing workspace for long-form fiction.** Bring story ideas, world references, volume and chapter planning, prose, and character state into one workspace. Write manually, revise with AI, or generate volumes and books within a scope and budget you approve.

Novels and reference material live in your configured MongoDB database. Manual writing requires no model API key. You choose and configure the providers used for AI features.

[Features](#features) · [Quick start](#quick-start) · [User guide](docs/user-guide.zh-CN.md) · [Troubleshooting](docs/troubleshooting.zh-CN.md) · [License and origin](#license-and-origin)

The current source version is [0.1.0-rc.3](VERSION), a prerelease. The installation workflow targets Windows, with local and trusted LAN use described below.

## Features

| Feature | What it provides |
| --- | --- |
| Writing and revision | Bookshelves, volumes, chapter outlines, prose, autosave, draft recovery, word counts, and text export |
| AI assistance | Ideas, story direction, volume and chapter outlines, prose, continuation, scene rewrites, style checks, and volume retrospectives |
| Book generation | Volume or whole-book jobs with chapter progress, usage records, pause reasons, and recovery actions |
| World references | Characters, locations, items, rules, lore, factions, and relationship maps in one place |
| Story continuity | Character memory, permanent facts, plot threads, and chapter state; Story Health checks run without model calls |
| Tavern card import | SillyTavern character-card JSON, PNG metadata, and world books, reviewed before use in an existing or new novel |
| Illustrations and covers | Covers, character portraits, and scene illustrations through compatible image APIs or ComfyUI workflows |

The writing workflow covers **idea → story direction → world references → volume outline → chapter outline → prose → state updates**. Start from a blank project, AI inspiration, or imported character cards.

## Quick start

### Requirements

| Dependency | Requirement |
| --- | --- |
| Operating system | Windows 10/11 |
| Python | 64-bit 3.11 or 3.12, with Tcl/Tk and the interpreter on PATH |
| Node.js | 64-bit 22.x, at least 22.13, or 24.x, including npm |
| MongoDB | 7/8, running locally or reachable through a configured connection |
| Network | Required to download dependencies on first installation and to reach configured AI providers |

This is a source installation. Install Python, Node.js, and MongoDB separately.

### Install and launch

1. In the [project repository](https://github.com/dzesen/novel-G), select **Code → Download ZIP**. Extract the source into a writable directory and open the folder containing `setup.bat` and `start.bat`.
2. Start MongoDB. The default connection is `mongodb://localhost:27017`. For another address, copy `backend/config/config_default.yaml` to `backend/config/config.yaml` and edit `mongodb_url`.
3. Double-click `setup.bat` to check prerequisites, install dependencies, and build the frontend. If installation fails, fix the reported problem and run it again to reuse completed steps.
4. Double-click `start.bat`. The launcher opens the browser when services are ready. Create the administrator account on first use.

The default address is [http://127.0.0.1:3000](http://127.0.0.1:3000). For daily use, run `start.bat`. See the [source installation guide](docs/source-release.zh-CN.md) for detailed installation and upgrade steps.

### Create your first novel

1. Create a novel from the bookshelf, using your own story direction, AI inspiration, or imported Tavern cards.
2. Organize characters and world references, plan volumes and chapters, then edit outlines and prose in the chapter workspace.
3. To use generation, configure a model, choose a chapter, volume, or book task, and review its readiness check before approving the scope and budget.

Saving prose and updating story state are separate actions. Run a state update when chapter changes should inform later writing. Administrators can also add authors under **Settings → Local Users**; each account has its own bookshelf.

## Models and generation

Administrators can configure OpenAI-compatible endpoints, Gemini, Claude, and the model used by each workflow in Settings. Covers and illustrations require a compatible image API or ComfyUI workflow configured separately.

Browsing pages and checking readiness do not call paid models. Authors explicitly start generation, review, and illustration requests. Automated jobs stay within the approved scope, call limits, and budget, and pause when an author decision is needed. AI requests send the relevant creative material to the configured provider.

Review generated text against your story and references. Choose an independent review mode when needed, inspect generation records and usage, and recover unfinished drafts after truncation or missing scenes. Model quality, speed, and compatibility vary; see [supported scope and known limitations](docs/known-limitations.zh-CN.md).

## Data and backups

The application uses **FastAPI, Next.js, and MongoDB**, with a Windows launcher.

| Data | Location |
| --- | --- |
| Novels, reference cards, state, and accounts | The configured MongoDB database |
| Model keys, database connection, and runtime settings | `backend/config/config.yaml` |
| Images and local file assets | `managed-assets/`, `static/` |
| Database backups | `backups/` by default; configurable in Settings |
| Logs and check results | `logs/`, `reports/` |

Runtime configuration is created from the defaults on first use and is ignored by Git. Keep configuration files containing credentials out of commits and issue attachments.

Administrators can manage database backups under **Settings → Backup & Restore**. The chapter editor exports a chapter or whole novel as text. Database backups do not include runtime configuration or local images; preserve those files separately when migrating.

Before upgrading, stop generation, resolve unconfirmed usage, and back up your data, then stop the frontend and backend. Extract the new source into a new directory, migrate configuration and file assets, and run `setup.bat`. Restoring a database requires users to sign in again and authorize further generation. See [upgrades and data preservation](docs/source-release.zh-CN.md#升级现有安装).

## Everyday maintenance

Run these commands from PowerShell in the project directory.

| Command | Purpose |
| --- | --- |
| `.\setup.bat --check` | Check the installation and MongoDB without downloading dependencies or rebuilding |
| `.\setup.bat --repair` | After stopping services, reinstall dependencies and rebuild while preserving configuration and local data |
| `.\start.bat --manual` | Open the launcher, then choose a mode and start services manually |

Checks write local logs and may create the default configuration during the first configuration check. If dependencies are ready but MongoDB is unavailable, start the database or correct the connection and run `--check` again. Installation logs are in `logs/installation/`.

For a shared trusted LAN, stop services, enable trusted LAN mode in the launcher, and restart. Public hosted deployment has not been validated; see the [security notes](docs/security.zh-CN.md) for deployment scope.

## Documentation and feedback

Detailed project documentation is currently in Chinese.

- [User guide](docs/user-guide.zh-CN.md)
- [Book generation and pause handling](docs/user-guide.zh-CN.md#自动成书)
- [Installation, upgrades, and data preservation](docs/source-release.zh-CN.md)
- [Troubleshooting](docs/troubleshooting.zh-CN.md)
- [Supported scope and known limitations](docs/known-limitations.zh-CN.md)
- [Security](docs/security.zh-CN.md)

Report problems and suggestions through [GitHub Issues](https://github.com/dzesen/novel-G/issues), with reproduction steps, environment details, and redacted error information. See [contributing](docs/contributing.zh-CN.md) for development and submission conventions.

## License and origin

Novel-G uses the [GNU Affero General Public License v3.0](LICENSE), identified as `AGPL-3.0-only`.

This project is based on the `dev` branch of [YILING0013/AI_NovelGenerator](https://github.com/YILING0013/AI_NovelGenerator), from [snapshot 9f8504f](https://github.com/YILING0013/AI_NovelGenerator/tree/9f8504f2833102bb65b1b7cf72c6a499fe989930) dated 2026-05-17. Thanks to the original authors and contributors; their attribution, copyright, and commit authorship are preserved.

See [license and origin](docs/license-status.zh-CN.md) and [upstream and third-party notices](docs/third-party-notices.zh-CN.md) for provenance and third-party material.
