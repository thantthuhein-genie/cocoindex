# Brain.md — context for AI agents working on this project

This file is decisions/rationale/gotchas for whoever (human or agent) touches
this repo next. For setup/run instructions see [README.md](README.md). For
CocoIndex API usage, see the next section first — it overrides your training
data.

## Read this first: CocoIndex v1, not v0

This project depends on `cocoindex>=1.0.0`. There was a complete API rewrite
between v0 and v1 — **anything you "know" about CocoIndex from training data
is almost certainly the removed v0 API** (`@cocoindex.flow_def`, `FlowBuilder`,
`DataScope`, `transform_flow`, `cocoindex.sources.*`, `cocoindex.functions.*`).
Do not emit v0 patterns.

The authoritative, up-to-date reference is installed in this repo at
[.agents/skills/cocoindex/SKILL.md](.agents/skills/cocoindex/SKILL.md) (+
`references/*.md` alongside it) — read that before writing or changing any
CocoIndex pipeline code here. It has a v0→v1 symbol translation table at the
bottom.

## What this project is

A CocoIndex v1 pipeline that builds a live, syntax-aware semantic (RAG) index
over one or more separate codebases (not this repo's own code — see
`SOURCE_DIR` in each app file), storing embeddings in Postgres/pgvector.
This workspace itself is generic and indexes nothing by default — `repos/`
is gitignored and empty until you add a repo-specific app file (see
`_template_repo.py.txt` and README.md's "Adding another repo to index").
Built from https://cocoindex.io/blogs/index-codebase-v1/, adapted
substantially — see below.

## Architecture decisions and why

- **Isolated Postgres 17 on its own port, not any pre-existing shared
  instance.** This setup was first built on a machine whose existing
  Postgres was an older engine already hosting several unrelated project
  databases used by other services. pgvector wasn't available for that
  engine, and bolting a C extension onto a shared instance other services
  depend on was the wrong blast radius for a side index. Used Homebrew's
  `postgresql@17` (own port `5433`, own data dir) instead —
  `brew install pgvector` + `brew link --overwrite pgvector`. If your
  machine already has a Postgres with pgvector available, you likely don't
  need a second instance at all — this was a constraint of that specific
  environment, not a hard requirement of the pipeline.
- **One Postgres database (`cocoindex`), one schema per indexed repo**
  (`<repo>.code_embeddings`), not one database per repo. Keeps connection
  config simple while still preventing collisions.
- **`common.py` (shared pipeline) + one file per repo under `repos/`**, not
  a single monolithic `main.py`. The blog's original example hardcodes one
  repo per file; this workspace needs to scale to multiple repos, so only
  the truly repo-specific bits (`SOURCE_DIR`, `PG_SCHEMA_NAME`, `AppConfig`
  name, file-pattern matchers) live in the per-repo file. Adding a repo =
  copy `_template_repo.py.txt` into `repos/<name>.py`, fill in the
  placeholders.
- **`repos/` is gitignored; `common.py` is installed as a top-level module**
  (`py-modules = ["common"]` in `pyproject.toml`, not just directory
  colocation). This workspace is meant to be generic/reusable across
  projects — which specific repos you index, and their absolute source
  paths, is local/project-specific and shouldn't live in this repo's git
  history. The first commit accidentally included a repo-specific app file
  at the root before this split existed; it was amended out (single-commit
  repo, no other history riding on it) rather than left in as a legacy
  artifact. `py-modules` (instead of the original `packages = []`) is what
  makes `from common import ...` resolve correctly from `repos/*.py`
  regardless of colocation — verified it fails without this
  (`ModuleNotFoundError`) before landing on the fix.
- **Markdown gets bigger chunks** (`CHUNKING_BY_LANGUAGE` in `common.py`:
  2000/500/600 vs 1000/300/300 for code). Root cause: an indexed repo's
  markdown docs had ASCII-art diagrams that were landing as isolated,
  contextless chunks under the blog's default 1000-char chunking — a
  problem for any repo with diagram-heavy docs, not specific to one project.
- **Chunks are embedded with their relative path prepended**
  (`f"File: {rel_path}\n\n{chunk.text}"`, embedding-only — the stored `code`
  column stays clean). Root cause: raw code embeddings favor
  comment/prose-heavy chunks (tests, factories, fixtures) over terse
  definitions, so "where is X defined" queries were dominated by test files
  instead of the actual model/class definitions.
- **Query-time authority reranking** (`AUTHORITY_RULES` in `common.py`):
  fetch `top_k * 6` candidates by raw cosine distance, then re-sort by
  `distance + path_penalty` (demote tests/factories/seeders/docs, promote
  `Models/`) before truncating to `top_k`. Tunable per repo without
  reprocessing.
- **Context expansion at query time** (`_expand_to_context`): re-reads the
  actual file on disk and grows the printed snippet out to the nearest
  blank-line boundary around the stored `start_line`/`end_line`, because
  fixed-size chunking regularly cuts mid-sentence/mid-docblock. Falls back to
  the stored (possibly truncated) chunk if the file isn't readable from the
  querying machine.
- See README.md → "Ranking & retrieval quality" for the honest ceiling on all
  of the above: this is for orienting/finding candidate files fast, not a
  substitute for reading the actual source before editing, and there's still
  no recency signal (a stale doc and current code rank purely on similarity).

## Gotchas hit during setup (don't rediscover these)

- **`COCOINDEX_DB` (or `Settings.db_path`) is a required setting** — the
  blog's abbreviated example omits it from the walkthrough text even though
  its own `.env.example` has it. Omitting it throws
  `ValueError: Environment settings must provide Settings.db_path` on
  `cocoindex update`.
- **pgvector doesn't auto-appear in `postgresql@17`'s extension dir** after
  `brew install pgvector` — you need `brew link --overwrite pgvector`
  explicitly, otherwise `CREATE EXTENSION vector` fails to find the control
  file.
- **The running Postgres superuser is `postgres`, not your OS user.** If
  your Postgres install only has a `postgres` role, connecting as your shell
  username fails with `role "..." does not exist`. Use `psql -U postgres`
  for admin actions in that case.
- **The vector table lives in a named schema, not `public`.** `\dt` with no
  schema, or TablePlus's default sidebar view, will show nothing — switch to
  the repo's schema (e.g. `myrepo`) first.
- **Changing chunking/embedding logic invalidates old chunk IDs.** IDs are
  content-hash derived, so a chunking-parameter change effectively creates a
  new set of rows. CocoIndex's own docs say this should cascade
  incrementally without a manual reset — this project used
  `cocoindex update --full-reprocess -f` when validating chunking/embedding
  changes anyway, for certainty rather than out of strict necessity.
- **Homebrew's `postgresql@17` is a persistent background service**
  (`brew services start`) — it survives reboots/logins until explicitly
  stopped (`brew services stop postgresql@17`).

## File map (see README.md for full detail)

- `common.py` — shared pipeline: dataclass, embedder/DB lifespan,
  `process_file`/`process_chunk`, `query_once` (reranking + expansion live
  here)
- `repos/*.py` — your per-repo app files; gitignored and empty on a fresh
  clone (see "repos/ is gitignored" above)
- `_template_repo.py.txt` — runnable template + checklist for adding a repo
- `.agents/skills/cocoindex/` — the official CocoIndex Claude Code skill,
  installed via the `skills` CLI (see `skills-lock.json`) — treat as the API
  ground truth, not this file
- `cocoindex.db/` — CocoIndex's own internal LMDB tracking store (gitignored)
- `.env` — local config, gitignored; `.env.example` is the template
