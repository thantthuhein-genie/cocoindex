"""
Shared CocoIndex pipeline pieces reused by every per-repo app file under
repos/ (see _template_repo.py.txt). One repo = one app file that imports
from here and supplies its own SOURCE_DIR, PG_SCHEMA_NAME, and file patterns.
"""

from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass
from typing import AsyncIterator, Annotated

import asyncpg
from numpy.typing import NDArray

import cocoindex as coco
from cocoindex.connectors import postgres
from cocoindex.ops.text import RecursiveSplitter, detect_code_language
from cocoindex.ops.sentence_transformers import SentenceTransformerEmbedder
from cocoindex.resources.chunk import Chunk
from cocoindex.resources.file import FileLike
from cocoindex.resources.id import IdGenerator

DATABASE_URL = os.getenv(
    "POSTGRES_URL", "postgres://cocoindex:cocoindex@localhost:5433/cocoindex"
)
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

PG_DB = coco.ContextKey[asyncpg.Pool]("code_embedding_db")
EMBEDDER = coco.ContextKey[SentenceTransformerEmbedder]("embedder", detect_change=True)

_splitter = RecursiveSplitter()


@dataclass
class CodeEmbedding:
    id: int
    filename: str
    code: str
    embedding: Annotated[NDArray, EMBEDDER]
    start_line: int
    end_line: int


@coco.lifespan
async def coco_lifespan(
    builder: coco.EnvironmentBuilder,
) -> AsyncIterator[None]:
    async with asyncpg.create_pool(DATABASE_URL) as pool:
        builder.provide(PG_DB, pool)
        builder.provide(EMBEDDER, SentenceTransformerEmbedder(EMBED_MODEL))
        yield


@coco.fn
async def process_chunk(
    chunk: Chunk,
    filename: pathlib.PurePath,
    rel_path: pathlib.PurePath,
    id_gen: IdGenerator,
    table: postgres.TableTarget[CodeEmbedding],
) -> None:
    # Prepend the repo-relative path to the text we embed (not to the stored
    # `code`) so the vector carries path signal — e.g. "app/Models/..." vs
    # "tests/Factories/..." — instead of matching on prose content alone.
    embed_text = f"File: {rel_path}\n\n{chunk.text}"
    embedding = await coco.use_context(EMBEDDER).embed(embed_text)
    table.declare_row(
        row=CodeEmbedding(
            id=await id_gen.next_id(chunk.text),
            filename=str(filename),
            code=chunk.text,
            embedding=embedding,
            start_line=chunk.start.line,
            end_line=chunk.end.line,
        ),
    )


# Markdown docs (esp. ones with ASCII-art diagrams) need bigger chunks and
# more overlap than code, so a diagram chunk still carries the surrounding
# prose that explains it instead of coming back bare.
CHUNKING_BY_LANGUAGE = {
    "default": dict(chunk_size=1000, min_chunk_size=300, chunk_overlap=300),
    "markdown": dict(chunk_size=2000, min_chunk_size=500, chunk_overlap=600),
}


@coco.fn(memo=True)
async def process_file(
    file: FileLike,
    source_root: pathlib.Path,
    table: postgres.TableTarget[CodeEmbedding],
) -> None:
    text = await file.read_text()
    abs_path = file.file_path.path
    try:
        rel_path = abs_path.relative_to(source_root)
    except ValueError:
        rel_path = abs_path
    language = detect_code_language(filename=str(abs_path.name))
    chunking = CHUNKING_BY_LANGUAGE.get(language, CHUNKING_BY_LANGUAGE["default"])
    chunks = _splitter.split(
        text,
        language=language,
        **chunking,
    )
    id_gen = IdGenerator()
    await coco.map(process_chunk, chunks, abs_path, rel_path, id_gen, table)


# Path-based authority adjustment applied on top of cosine distance (lower is
# better, so a positive value here demotes a result and a negative one
# promotes it). Rules are additive; order doesn't matter. Tune per-repo by
# editing this list if a new codebase has a different layout convention.
AUTHORITY_RULES: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"(^|/)tests?/", re.IGNORECASE), 0.15),
    (re.compile(r"(Factory|Seeder|Test)\.php$"), 0.15),
    (re.compile(r"(^|/)database/(factories|seeders)/", re.IGNORECASE), 0.15),
    (re.compile(r"\.(spec|test)\."), 0.15),
    (re.compile(r"(^|/)(doc|docs)/", re.IGNORECASE), 0.03),
    (re.compile(r"\.mdx?$"), 0.03),
    (re.compile(r"(^|/)[Mm]odels/"), -0.05),
]


def _authority_penalty(filename: str) -> float:
    return sum(value for pattern, value in AUTHORITY_RULES if pattern.search(filename))


def _expand_to_context(
    filename: str,
    start_line: int,
    end_line: int,
    fallback_code: str,
    *,
    max_extra: int = 40,
) -> tuple[int, int, str]:
    """Re-read the chunk's surrounding lines from disk, expanded out to the
    nearest blank-line boundary on each side, so a chunk that was truncated
    mid-sentence/mid-block by size-based chunking reads as a complete unit.
    Falls back to the stored (possibly truncated) chunk if the file can't be
    read (moved, deleted, or index queried on a different machine).
    """
    try:
        with open(filename, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return start_line, end_line, fallback_code

    s = max(start_line - 1, 0)
    e = min(end_line - 1, len(lines) - 1)
    extra = 0
    while s > 0 and lines[s - 1].strip() != "" and extra < max_extra:
        s -= 1
        extra += 1
    extra = 0
    while e < len(lines) - 1 and lines[e + 1].strip() != "" and extra < max_extra:
        e += 1
        extra += 1
    return s + 1, e + 1, "".join(lines[s : e + 1])


async def query_once(
    pool: asyncpg.Pool,
    embedder: SentenceTransformerEmbedder,
    query: str,
    *,
    pg_schema_name: str,
    table_name: str,
    top_k: int = 5,
) -> None:
    query_vec = await embedder.embed(query)
    candidate_pool = max(top_k * 6, 30)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                filename,
                code,
                embedding <=> $1 AS distance,
                start_line,
                end_line
            FROM "{pg_schema_name}"."{table_name}"
            ORDER BY distance ASC
            LIMIT $2
            """,
            query_vec,
            candidate_pool,
        )

    ranked = sorted(
        rows, key=lambda r: float(r["distance"]) + _authority_penalty(r["filename"])
    )[:top_k]

    for r in ranked:
        score = 1.0 - float(r["distance"])
        start, end, code = _expand_to_context(
            r["filename"], r["start_line"], r["end_line"], r["code"]
        )
        expanded_note = " (expanded)" if (start, end) != (r["start_line"], r["end_line"]) else ""
        print(f"[{score:.3f}] {r['filename']} (L{start}-L{end}{expanded_note})")
        for line in code.rstrip("\n").splitlines():
            print(f"    {line}")
        print("---")
