# CocoIndex Code Embedding Workspace

Live, syntax-aware semantic (RAG) index over one or more codebases, embedded with
`sentence-transformers/all-MiniLM-L6-v2` and stored in Postgres/pgvector.
Based on https://cocoindex.io/blogs/index-codebase-v1/

## Project structure

```
cocoindex/
  common.py                          # shared pipeline logic (imported, never run directly)
  repos/                             # per-repo app files you create - GITIGNORED, empty on a
                                      #   fresh clone until you add one (see _template_repo.py.txt)
  _template_repo.py.txt              # runnable template + checklist for adding a repo
  pyproject.toml                     # runtime dependencies + py-modules=["common"]
  .env / .env.example                # local config (gitignored) / checked-in template
  .gitignore
  Brain.md                           # decisions/rationale/gotchas, for AI agents & maintainers
  README.md                          # this file
  .venv/                             # Python virtualenv (not committed)
  cocoindex.db/                      # CocoIndex's own internal tracking state (LMDB, gitignored)
  *.egg-info/                        # editable-install metadata (gitignored)
  .agents/skills/cocoindex/          # official CocoIndex v1 Claude Code skill (reference docs)
  skills-lock.json                   # lockfile for the installed skill above
```

One Python file under `repos/` = one indexed repo. All repo apps share the
pipeline logic in `common.py` and the same Postgres database, but each gets
its own schema so they never collide. **`repos/` is gitignored on purpose**:
this workspace (`common.py`, the template, the docs) is meant to stay
generic and shareable across projects, while which specific repos you're
indexing — and their absolute source paths — is local/project-specific and
doesn't belong in this repo's git history. `common.py` is installed as a
top-level module via `py-modules` in `pyproject.toml` (not colocation), so
`from common import ...` resolves correctly no matter how deep under
`repos/` an app file lives.

### `common.py`

The shared pipeline every per-repo app file imports — it has no `coco.App`
of its own and is never run directly. Contains:

- `CodeEmbedding` — the dataclass = the row shape stored in Postgres (`id`,
  `filename`, `code`, `embedding`, `start_line`, `end_line`).
- `coco_lifespan` (`@coco.lifespan`) — opens one `asyncpg` connection pool
  and one `SentenceTransformerEmbedder`, exposed to every mounted component
  via the `PG_DB`/`EMBEDDER` `ContextKey`s so the (expensive) DB pool and
  model are created once and reused, not per-file/per-chunk.
- `process_file` (`@coco.fn(memo=True)`) — reads one file, detects its
  language, splits it into chunks with per-language settings
  (`CHUNKING_BY_LANGUAGE`, see "Chunking" below), then fans out to
  `process_chunk` per chunk. `memo=True` means CocoIndex skips re-running
  this for a file whose content and code haven't changed since last run.
- `process_chunk` (`@coco.fn`) — embeds one chunk (path-prefixed text only,
  see "Ranking & retrieval quality" below) and calls `table.declare_row(...)`
  — CocoIndex diffs this declared state against what's already in Postgres
  and issues the insert/update/delete itself.
- `query_once` — the actual search path: embeds the query text, pulls a wide
  candidate pool by cosine distance, reranks by path-authority, expands each
  result's context from disk, and prints it.

### `repos/<your_repo>.py` (gitignored)

The entry point CocoIndex actually runs for a given repo — what
`cocoindex update repos/<your_repo>.py` resolves to. Repo-specific by design
(see `Brain.md` for why this is a separate file instead of being folded into
`common.py`), and lives under the gitignored `repos/` directory since it
encodes an absolute local path (`SOURCE_DIR`) and is specific to whichever
repos you personally index — not something the shared workspace should
track. You create these yourself from `_template_repo.py.txt`; `repos/` is
empty on a fresh clone. Each one has:

- `SOURCE_DIR`, `PG_SCHEMA_NAME`, `TABLE_NAME` — where this repo's source
  lives and where its rows land in Postgres.
- `app_main` (`@coco.fn`) — mounts the Postgres target table
  (`postgres.mount_table_target`), walks `SOURCE_DIR` with
  `localfs.walk_dir(..., live=True)` filtered by `PatternFilePathMatcher`
  (the `included_patterns`/`excluded_patterns` you'd edit for a new repo),
  then mounts `process_file` (imported from `common.py`) over every matched
  file via `coco.mount_each`.
- `app = coco.App(...)` — the object the CLI looks up by convention when you
  pass this file as `APP_TARGET`.
- a `query()` function plus `if __name__ == "__main__":` block, so
  `python repos/<your_repo>.py "text"` runs a one-off search via
  `common.query_once` without going through the `cocoindex` CLI at all.

### `_template_repo.py.txt`

An actual runnable app template, not just instructions — copy it into
`repos/<your_repo>.py` and fill in the placeholders (`SOURCE_DIR`,
`PG_SCHEMA_NAME`, `APP_NAME`, `INCLUDED_PATTERNS`, `EXCLUDED_PATTERNS`); its
own docstring has the full checklist. Named `.txt` on purpose so nothing
ever tries to import it as a Python module before you've filled it in.

### `pyproject.toml`

Declares the four runtime dependencies
(`cocoindex[postgres,sentence_transformers]`, `asyncpg`, `pgvector`,
`numpy`, `python-dotenv`), plus `py-modules = ["common"]` so `common.py` is
installed into the venv as a top-level importable module — this is what
lets `repos/*.py` files do `from common import ...` and have it resolve
regardless of how deep they live under `repos/` (this isn't a distributable
library otherwise, just scripts installed editable so the venv gets the
`cocoindex`
CLI and SDK on the path).

### `.env` / `.env.example`

`.env` holds the three environment variables the pipeline reads at runtime
(`COCOINDEX_DB`, `POSTGRES_URL`, `PYTORCH_ENABLE_MPS_FALLBACK`) and is loaded
automatically both by the `cocoindex` CLI and by `load_dotenv()` in each app
file's `__main__` block. It's gitignored because it's machine-specific (a
different machine would use a different `POSTGRES_URL`/port).
`.env.example` is the checked-in template with the same keys, meant to be
copied (`cp .env.example .env`) on a fresh machine.

### `cocoindex.db/`

CocoIndex's own internal state store — an embedded LMDB key-value database
(hence the `mdb/lock.mdb` + `mdb/data.mdb` files inside). This is how
CocoIndex knows, on the next `cocoindex update`, which files/chunks changed
since last time (content fingerprints), so it can skip unchanged work and
remove rows for deleted files. **This is not the vector data** — that lives
in Postgres. Deleting this directory just forces a full reprocess next run;
nothing is lost that isn't cheaply rebuildable from the source repo.

### `*.egg-info/`

Setuptools metadata generated by `pip install -e .` (editable install) —
package name, dependency list, source manifest. Regenerated automatically on
reinstall, never hand-edited, safe to delete.

### `.agents/skills/cocoindex/` and `skills-lock.json`

The official CocoIndex Claude Code skill (`SKILL.md` + `references/*.md`),
installed via the `skills` CLI (`skills-lock.json` pins which version is
installed, the same idea as a `package-lock.json`). This is reference
documentation, not code this project imports — read it before writing or
changing any CocoIndex pipeline code here, since CocoIndex went through a
complete v0→v1 API rewrite and this is the accurate v1 reference (see
`Brain.md`).

### `Brain.md`

Not for running anything — context for AI agents and future maintainers:
why each non-default decision here was made, gotchas hit during setup, and
a pointer to the skill docs above. Update it when you make another
non-obvious decision; don't duplicate this README's how-to-run instructions
into it.

### `.gitignore`

Keeps local/derived state out of version control: `.env` (machine-specific
config), `cocoindex.db/` (this machine's tracking state), `__pycache__/` /
`*.egg-info/` / `.venv/` (Python build/venv artifacts), `.DS_Store`.

### `.venv/`

Standard Python virtualenv holding the installed `cocoindex` package, its
CLI entrypoint, and the other `pyproject.toml` dependencies. Not committed —
recreate on a new machine with:
```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

## Postgres / pgvector

Uses an **isolated** Postgres instance, separate from any shared/DBngin Postgres
that hosts other project databases.

| Setting | Value |
|---|---|
| Engine | Homebrew `postgresql@17` (`brew services start postgresql@17`) |
| Host / Port | `localhost:5433` |
| Database | `cocoindex` |
| Role | `cocoindex` / password `cocoindex` |
| Extension | `vector` (pgvector 0.8.5, `brew install pgvector` + `brew link pgvector`) |

Schema-per-repo convention: `<schema>.code_embeddings`, e.g. `myrepo.code_embeddings`.

One-time setup, for reference (already done):

```bash
brew install pgvector
brew link --overwrite pgvector
# set port = 5433 in /opt/homebrew/var/postgresql@17/postgresql.conf
brew services start postgresql@17

PG=/opt/homebrew/opt/postgresql@17/bin
$PG/psql -p 5433 -U postgres -d postgres -c "CREATE ROLE cocoindex WITH LOGIN PASSWORD 'cocoindex' CREATEDB;"
$PG/psql -p 5433 -U postgres -d postgres -c "CREATE DATABASE cocoindex OWNER cocoindex;"
$PG/psql -p 5433 -U postgres -d cocoindex -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

### Viewing the data in TablePlus

| Field | Value |
|---|---|
| Host | `localhost` |
| Port | `5433` |
| User | `cocoindex` |
| Password | `cocoindex` |
| Database | `cocoindex` |
| SSL | off |

After connecting, switch the sidebar schema selector from `public` to the repo's
schema (e.g. `myrepo`), then open `code_embeddings`. TablePlus has no native
pgvector renderer, so the `embedding` column shows as raw array/text — expected.

## Chunking

`common.py`'s `process_file` uses per-language chunk settings (`CHUNKING_BY_LANGUAGE`):

| Language | chunk_size | min_chunk_size | chunk_overlap |
|---|---|---|---|
| default (code) | 1000 | 300 | 300 |
| markdown | 2000 | 500 | 600 |

Markdown gets bigger chunks and more overlap than the blog's original defaults
because docs with ASCII-art diagrams were coming back as isolated diagram-only
chunks with no surrounding prose/heading — the larger window keeps a diagram
attached to the section that explains it. If a new repo's docs have the same
issue, add/tune an entry in `CHUNKING_BY_LANGUAGE` rather than touching the
default code chunking.

## Ranking & retrieval quality

Raw cosine similarity over code embeddings tends to favor verbose,
comment/prose-heavy chunks (tests, factories, fixtures) over terse
definitions (models, short methods) — a "where is X actually defined" query
would surface `tests/` before `app/Models/`. `common.py`'s `query_once`
addresses this in two ways, both applied at query time (no need to
reprocess when tuning):

- **Path-embedded text**: each chunk's embedding is computed over
  `f"File: {rel_path}\n\n{chunk.text}"` (not the raw chunk alone — the stored
  `code` column stays clean), so the vector itself carries path signal.
- **Authority reranking** (`AUTHORITY_RULES`): the query fetches a wider
  candidate pool (`top_k * 6`, min 30) by pure distance, then re-sorts by
  `distance + path_penalty` before truncating to `top_k`. Penalties demote
  `tests/`, `*Factory.php`, `*Seeder.php`, `database/factories|seeders/`, and
  docs (`doc/`, `*.md`); a bonus promotes `Models/`. Tune this list per repo —
  it's written against Laravel conventions.

Query results also **expand to the nearest blank-line boundary** on disk
around the stored `start_line`/`end_line` (`_expand_to_context`, capped at 40
extra lines each direction) before printing — this fixes chunks that were cut
off mid-sentence/mid-docblock by size-based chunking. Falls back to the raw
stored chunk if the file can't be read from the querying machine. Results
print with `(expanded)` when this kicked in.

None of this eliminates the fundamental ceiling of embedding-based code
search: it's good for orienting yourself or finding candidate files fast, not
a substitute for reading the actual source before editing, and there's still
no recency signal (a stale doc and current code rank purely on similarity).

## Running

```bash
cd /Users/tth-genie/Desktop/code/cocoindex
source .venv/bin/activate

cocoindex update repos/<your_repo>.py       # catch-up: scan, diff, exit
cocoindex update -L repos/<your_repo>.py     # live: keeps watching and re-embeds on save
cocoindex ls                                 # list all registered apps
python repos/<your_repo>.py "some query"     # semantic search
```

Re-running `update` is incremental: unchanged files/chunks are skipped
(memoization + content-hash chunk IDs), rows for deleted files are cleaned up
automatically. Live mode (`-L`) means you generally don't need to re-run
anything manually — leave it running in a spare terminal while you work.

## Adding another repo to index

Per-repo app files live under `repos/` (gitignored — see "Project structure"
above for why).

1. `mkdir -p repos` (first time only)
2. `cp _template_repo.py.txt repos/<new_repo_name>.py`
3. Edit the placeholders marked in the new file (leave `common.py` untouched):
   - `SOURCE_DIR` -> absolute path to the new repo
   - `PG_SCHEMA_NAME` -> a unique schema name, e.g. `"other_repo"`
   - `APP_NAME` -> a unique app name, e.g. `"OtherRepoCodeEmbedding"`
   - `INCLUDED_PATTERNS` / `EXCLUDED_PATTERNS` -> match the new repo's actual
     languages and vendor/build dirs. Check first, don't assume:
     ```bash
     find <repo> -type f -not -path "*/vendor/*" -not -path "*/node_modules/*" \
       | sed 's/.*\.//' | sort | uniq -c | sort -rn | head -20
     ```
4. `cocoindex update repos/<new_repo_name>.py` (or `-L` for live)
5. `python repos/<new_repo_name>.py "some query"` to verify
6. `cocoindex ls` to confirm both apps are registered

See `_template_repo.py.txt`'s own docstring for the same checklist in-repo.

## Using this while working with Claude Code

**Manual, zero setup:** ask Claude to run
`python repos/<your_repo>.py "your query"` from this directory during a
session in the target repo.

**Native MCP tool (recommended for regular use):** via
[`cocoindex-code`](https://cocoindex.io/cocoindex-code/) (`ccc`) — note this
builds its own separate local index, not this project's pgvector table:

```bash
pipx install 'cocoindex-code[full]'
cd /path/to/target/repo
ccc init
ccc index
claude mcp add cocoindex-code -- ccc mcp
```

After that, Claude Code auto-uses semantic search as a native tool in that repo.
Re-run `ccc index` after pulling changes to keep it in sync.
