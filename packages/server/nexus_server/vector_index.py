"""Vector index — #135.

Local semantic search infrastructure built on:

  * ``sqlite-vec`` — SQLite extension; vector data lives in the same
    database file as the rest of Nexus's per-user state. No external
    services to operate, no second store to back up. The ``vec0``
    virtual table supports k-NN over float32 vectors with metadata
    filtering in a single query.
  * Gemini ``text-embedding-004`` (or its successor) — cloud embeddings.
    768-dimensional, ~1 ms per text via the genai client. We don't
    bundle a local model into the .dmg because (a) a passable
    sentence-transformer is ~80 MB shipping cost, (b) Gemini embeddings
    are stronger out of the box for our mixed Chinese / English /
    medical-vocabulary content, (c) the cost is rounding-error
    (≈ ¥0.0001 / 1k chars). The whole module exposes a swap point so a
    future local-model backend is a constructor change.

The public surface is intentionally small — three operations cover
all the call sites the rest of the codebase needs:

  * :func:`init_vector_index` — idempotent schema setup; safe to call
    on every server boot.
  * :func:`upsert_chunks` — embed N text chunks under a (source_kind,
    source_id) pair. Existing chunks for the same key are replaced
    so re-distilling an attachment doesn't double-index it.
  * :func:`search_chunks` — embed a query and return the top-k most
    similar chunks across the user's data, with optional kind filter.

Failure modes
-------------
Every embedding call is wrapped: when the network is down, the API
key is missing, or Gemini quota is exceeded, the relevant function
raises ``EmbeddingUnavailable``. Callers should catch this and fall
back to lexical search (``twin_event_log.search_messages`` /
``files.search_uploaded_files``) which both already exist and require
no embedding. The point of vector search is to improve recall, not
to replace lexical — if the vector layer is down the agent should
degrade gracefully rather than 502.

Multi-tenant layout
-------------------
A single ``vector_index.db`` under ``$RUNE_HOME/data/`` holds all
users' chunks. Each row carries the ``user_id``; all reads filter on
it. The query plan still uses the ``vec0`` k-NN index because
sqlite-vec supports ``WHERE`` + ``MATCH`` + ``k = ?`` clauses
composed together. Per-user DB files were considered but rejected:
they double the file handle count for negligible isolation benefit
(an authenticated server already enforces tenant boundaries above
this layer).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────

# Gemini's text-embedding-004 emits 768-dim vectors. If we ever switch
# to a different embedding model with a different dimensionality the
# schema migration is: ALTER VIRTUAL TABLE is not supported, so we'd
# drop + recreate ``chunks_vec`` and re-embed. Don't change this
# constant without a plan for that. Stays here as a single source of
# truth referenced by both the schema and the embedding sanity check.
EMBEDDING_DIM = 768

# Cap on how many characters of a single chunk we pass to the embedder.
# Beyond ~2k chars Gemini's embedding starts to over-average and loses
# specificity; splitting longer source text into multiple chunks gives
# better retrieval. The chunker below honours this.
CHUNK_CHAR_BUDGET = 1500

# Soft cap on chunks per source. Distilled attachment summaries hit
# 4k chars sometimes; splitting them into 3 chunks at 1.5k each is
# plenty. Going beyond ~8 chunks per source means we're over-indexing
# and starting to flood retrieval results with near-duplicates.
MAX_CHUNKS_PER_SOURCE = 8

# Default embedding model. Override via NEXUS_EMBEDDING_MODEL env var
# for testing newer models without code change.
DEFAULT_EMBEDDING_MODEL = os.getenv(
    "NEXUS_EMBEDDING_MODEL", "text-embedding-004",
)


# ── Exceptions ─────────────────────────────────────────────────────────


class EmbeddingUnavailable(Exception):
    """Raised when the embedding backend can't service a request.

    Caller should fall back to lexical search (or skip the index step
    silently if it's a write path). We deliberately don't raise generic
    ``Exception`` because higher layers want to distinguish "embedding
    failed but everything else is fine" from a real bug.
    """


# ── DB connection management ───────────────────────────────────────────


def _index_db_path() -> Path:
    """Resolve the single multi-tenant vector DB path.

    Sits under ``$RUNE_HOME/data/vector_index.db`` so it shares the
    backup volume with the rest of Nexus's local state. Created on
    demand by :func:`init_vector_index`.
    """
    rune_home = os.getenv("RUNE_HOME") or str(Path.home() / ".rune")
    base = Path(rune_home) / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base / "vector_index.db"


def _open_conn() -> sqlite3.Connection:
    """Open the vector-index DB with sqlite-vec loaded.

    The connection is **not** shared across threads / async tasks
    because sqlite3 isn't thread-safe and the embedding write path is
    fully async. Each caller opens its own conn and closes it.
    """
    conn = sqlite3.connect(_index_db_path())
    conn.enable_load_extension(True)
    try:
        import sqlite_vec
        sqlite_vec.load(conn)
    except ImportError as e:
        # The .dmg ships sqlite-vec via the server's pyproject.toml
        # dependency. If it's missing we're in a broken install path —
        # raise loudly so the user sees it on first chat instead of
        # silently degrading vector queries to lexical for weeks.
        raise EmbeddingUnavailable(
            f"sqlite-vec extension not installed: {e}. "
            "Run `pip install sqlite-vec` in the server's venv."
        ) from e
    finally:
        conn.enable_load_extension(False)
    # Foreign-key support is off by default in sqlite3 connections —
    # turn it on so the chunks ↔ chunks_vec cascade actually runs.
    conn.execute("PRAGMA foreign_keys = ON")
    # Match the WAL + busy_timeout settings from the main database so
    # concurrent async reads and writes don't produce SQLITE_BUSY and
    # silently degrade to lexical search.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


# ── Schema ─────────────────────────────────────────────────────────────


def init_vector_index() -> None:
    """Idempotent schema setup. Safe to call on every server boot.

    Two-table design:

    * ``chunks`` — regular table holding the text, metadata, and a
      stable PK that's both the rowid (so vec0 can reference it) and
      the lookup key callers use for hydration after k-NN.
    * ``chunks_vec`` — virtual ``vec0`` table holding only the
      embedding, keyed by the same rowid. sqlite-vec's k-NN walks this
      one; we JOIN back to ``chunks`` to recover the source text.

    Why split: ``vec0`` virtual tables don't store auxiliary text /
    metadata columns in a queryable shape (they exist but are
    write-only via the vec0 API). Keeping the metadata in a normal
    table lets us ``WHERE user_id = ?`` cheaply and index it.
    """
    conn = _open_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       TEXT    NOT NULL,
                source_kind   TEXT    NOT NULL,    -- 'chat' | 'upload' | 'caption' | 'skill' | ...
                source_id     TEXT    NOT NULL,    -- event_id / file_id / skill_name / ...
                chunk_index   INTEGER NOT NULL,    -- 0-based ordinal within the source
                text_chunk    TEXT    NOT NULL,
                created_at    INTEGER NOT NULL     -- unix epoch ms
            )
        """)
        # Speed up the metadata pre-filter that runs before / alongside
        # the vec0 k-NN. user_id is the partition every read uses.
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_user_kind
            ON chunks(user_id, source_kind)
        """)
        # Re-indexing (e.g. when an attachment gets re-distilled) needs
        # to delete the prior chunks for that source first. Lookup by
        # (user_id, source_kind, source_id) is the hot path.
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_source
            ON chunks(user_id, source_kind, source_id)
        """)
        # vec0 table: rowid is the chunks.chunk_id, embedding is f32x768.
        # The dimensionality is part of the schema and CAN'T be changed
        # post-creation — see the EMBEDDING_DIM constant for why.
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
                embedding float[{EMBEDDING_DIM}]
            )
        """)
        conn.commit()
        logger.info("vector_index: schema ready at %s", _index_db_path())
    finally:
        conn.close()


# ── Text chunking ──────────────────────────────────────────────────────


def chunk_text(text: str, char_budget: int = CHUNK_CHAR_BUDGET) -> list[str]:
    """Split ``text`` into embedding-sized chunks.

    Strategy: prefer paragraph boundaries; fall back to sentence
    boundaries; final fallback is a hard character split. We don't
    attempt token-aware splitting because Gemini's embed_content
    handles tokenisation internally — character budgeting is a
    sufficient proxy that doesn't require pulling in tiktoken.

    Empty / whitespace-only text returns an empty list (no point
    embedding nothing). Very short text returns a single chunk.
    """
    if not text:
        return []
    text = text.strip()
    if not text:
        return []
    if len(text) <= char_budget:
        return [text]

    # Try paragraph split first — natural semantic boundary.
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paragraphs:
        if len(buf) + len(p) + 2 <= char_budget:
            buf = f"{buf}\n\n{p}" if buf else p
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        if len(p) <= char_budget:
            buf = p
        else:
            # Single paragraph too big — sentence split.
            sentences = _sentence_split(p)
            sbuf = ""
            for s in sentences:
                if len(sbuf) + len(s) + 1 <= char_budget:
                    sbuf = f"{sbuf} {s}" if sbuf else s
                else:
                    if sbuf:
                        chunks.append(sbuf)
                    if len(s) <= char_budget:
                        sbuf = s
                    else:
                        # Pathological — hard slice.
                        for i in range(0, len(s), char_budget):
                            chunks.append(s[i:i + char_budget])
                        sbuf = ""
            if sbuf:
                buf = sbuf
    if buf:
        chunks.append(buf)
    return chunks[:MAX_CHUNKS_PER_SOURCE]


def _sentence_split(p: str) -> list[str]:
    """Naive sentence splitter that handles common CJK + EN punctuation.

    Not perfect (no abbreviation handling, no quoted-period awareness)
    but good enough for an embedding chunker where the goal is
    "produce roughly meaningful units" not "produce linguistically
    correct sentences". CJK punctuation is included because medical
    notes mix English and Chinese freely.
    """
    import re
    parts = re.split(r"(?<=[\.!?。！？])\s+", p)
    return [s.strip() for s in parts if s.strip()]


# ── Embedding client ───────────────────────────────────────────────────


@dataclass
class EmbedResult:
    """Single text → vector, plus metadata for sanity checks."""
    text: str
    embedding: list[float]
    model: str


class GeminiEmbeddingClient:
    """Async wrapper around ``google.genai`` embed_content.

    Holds the genai client + model name; reused across batches. The
    client itself is thread-safe per google-genai docs — we serialize
    on the asyncio loop because the rest of Nexus is async-first.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_EMBEDDING_MODEL,
    ):
        self._api_key = api_key or os.getenv("GEMINI_API_KEY")
        self._model = model
        self._client = None  # lazy init

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        if not self._api_key:
            raise EmbeddingUnavailable(
                "GEMINI_API_KEY not set; cannot embed. Configure it in "
                "$RUNE_HOME/.env or environment to enable semantic search."
            )
        try:
            from google import genai
            self._client = genai.Client(api_key=self._api_key)
        except ImportError as e:
            raise EmbeddingUnavailable(
                f"google-genai not installed: {e}. "
                "Run `pip install google-genai` in the server's venv."
            ) from e

    async def embed_batch(
        self, texts: Sequence[str],
    ) -> list[EmbedResult]:
        """Embed a batch of texts. Empty input returns empty list.

        Gemini's API accepts a list of strings and returns a parallel
        list of embeddings, so batching is a free latency win — one
        HTTP round-trip per ~100 chunks vs N round-trips. We don't
        chunk the batch ourselves because Gemini's limit is generous
        (~2k items) and we'd hit our own MAX_CHUNKS_PER_SOURCE before
        that.
        """
        if not texts:
            return []
        self._ensure_client()

        # The google.genai client is sync; offload to a thread so we
        # don't block the asyncio loop on network I/O during chat.
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: self._client.models.embed_content(
                    model=self._model,
                    contents=list(texts),
                ),
            )
        except Exception as e:  # noqa: BLE001
            # Network errors, quota, auth — all surface here.
            raise EmbeddingUnavailable(
                f"Gemini embedding call failed: {e}"
            ) from e

        # Response shape: response.embeddings = list of objects with
        # .values: list[float]. Handle both old (single .embedding)
        # and new (list .embeddings) shapes defensively.
        embeddings_attr = getattr(response, "embeddings", None)
        if embeddings_attr is None:
            single = getattr(response, "embedding", None)
            if single is None:
                raise EmbeddingUnavailable(
                    f"Gemini embed response missing 'embeddings': "
                    f"{type(response).__name__}"
                )
            embeddings_attr = [single]

        out: list[EmbedResult] = []
        for text, emb in zip(texts, embeddings_attr):
            values = getattr(emb, "values", None) or emb
            if len(values) != EMBEDDING_DIM:
                raise EmbeddingUnavailable(
                    f"Embedding dim mismatch: got {len(values)}, "
                    f"expected {EMBEDDING_DIM}. Check NEXUS_EMBEDDING_MODEL."
                )
            out.append(EmbedResult(
                text=text,
                embedding=list(values),
                model=self._model,
            ))
        return out


_default_client: Optional[GeminiEmbeddingClient] = None


def get_embedding_client() -> GeminiEmbeddingClient:
    """Return the module-level singleton embedding client.

    Lazily constructed so test envs without an API key don't pay for
    the client construction unless they actually try to embed.
    """
    global _default_client
    if _default_client is None:
        _default_client = GeminiEmbeddingClient()
    return _default_client


# ── Vector encoding helpers ────────────────────────────────────────────


def _pack_vector(values: Sequence[float]) -> bytes:
    """Pack a float[768] vector into the BLOB shape sqlite-vec expects."""
    if len(values) != EMBEDDING_DIM:
        raise ValueError(
            f"vector length {len(values)} != EMBEDDING_DIM {EMBEDDING_DIM}"
        )
    return struct.pack(f"{EMBEDDING_DIM}f", *values)


# ── Public API: write path ─────────────────────────────────────────────


async def upsert_chunks(
    user_id: str,
    source_kind: str,
    source_id: str,
    text: str,
    *,
    embedding_client: Optional[GeminiEmbeddingClient] = None,
) -> int:
    """Chunk, embed, and store text under (source_kind, source_id).

    Returns the number of chunks written. Returns 0 silently for empty
    text. Replaces any prior chunks for the same key — calling this
    twice with updated text overwrites rather than accumulating
    (otherwise re-distilling an attachment would double-index it).

    Raises :class:`EmbeddingUnavailable` if the embedding backend
    can't service the call. Callers should catch and skip the write
    rather than failing the parent operation — vector index is a
    nice-to-have, not the source of truth.
    """
    chunks = chunk_text(text)
    if not chunks:
        return 0
    client = embedding_client or get_embedding_client()
    embeddings = await client.embed_batch(chunks)
    if not embeddings:
        return 0

    conn = _open_conn()
    try:
        # Wrap the delete + insert in an explicit transaction so a crash
        # mid-upsert doesn't leave chunks_vec with dangling rowids.
        conn.execute("BEGIN IMMEDIATE")
        # Remove prior chunks for this source so a re-index doesn't
        # duplicate. The two-table design means we have to delete
        # from both — vec0 doesn't cascade because the rowid linkage
        # is by convention, not a foreign key.
        rows = conn.execute(
            "SELECT chunk_id FROM chunks "
            "WHERE user_id = ? AND source_kind = ? AND source_id = ?",
            (user_id, source_kind, source_id),
        ).fetchall()
        old_ids = [r[0] for r in rows]
        if old_ids:
            placeholders = ",".join("?" * len(old_ids))
            conn.execute(
                f"DELETE FROM chunks WHERE chunk_id IN ({placeholders})",
                old_ids,
            )
            conn.execute(
                f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders})",
                old_ids,
            )

        now_ms = int(time.time() * 1000)
        for i, emb in enumerate(embeddings):
            cursor = conn.execute(
                "INSERT INTO chunks "
                "(user_id, source_kind, source_id, chunk_index, text_chunk, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, source_kind, source_id, i, emb.text, now_ms),
            )
            new_rowid = cursor.lastrowid
            conn.execute(
                "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                (new_rowid, _pack_vector(emb.embedding)),
            )
        conn.commit()
        return len(embeddings)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Public API: read path ──────────────────────────────────────────────


@dataclass
class SearchHit:
    """One k-NN result row."""
    chunk_id: int
    source_kind: str
    source_id: str
    text_chunk: str
    distance: float           # cosine distance; lower is more similar
    chunk_index: int
    created_at_ms: int


async def search_chunks(
    user_id: str,
    query: str,
    *,
    k: int = 10,
    source_kinds: Optional[Sequence[str]] = None,
    embedding_client: Optional[GeminiEmbeddingClient] = None,
) -> list[SearchHit]:
    """Semantic search: embed ``query`` and return the top-k chunks.

    ``source_kinds`` filters to only those source types (e.g. just
    ``["caption"]`` to find image captions, ``["chat", "caption"]``
    to span both). None / empty means no filter.

    Raises :class:`EmbeddingUnavailable` when the embedding backend
    is down — caller should fall back to lexical search rather than
    propagating the error.
    """
    q = (query or "").strip()
    if not q:
        return []
    client = embedding_client or get_embedding_client()
    results = await client.embed_batch([q])
    if not results:
        return []
    qvec = _pack_vector(results[0].embedding)

    # sqlite-vec k-NN requires the k value as a parameter on the
    # MATCH clause. To layer a metadata filter on top, we use the
    # documented WHERE rowid IN (subquery) pattern: pre-filter by
    # user_id (and optional kind) on the regular table, then ask
    # vec0 to k-NN only among those rowids. This is fast as long as
    # the user_id filter is selective (single tenant: yes; ~ 10k
    # chunks per user is well within the LIMIT-pre-filter sweet spot
    # for vec0).
    where_clauses = ["user_id = ?"]
    params: list = [user_id]
    if source_kinds:
        placeholders = ",".join("?" * len(source_kinds))
        where_clauses.append(f"source_kind IN ({placeholders})")
        params.extend(source_kinds)
    filter_sql = " AND ".join(where_clauses)

    conn = _open_conn()
    try:
        rows = conn.execute(
            f"""
            SELECT
                c.chunk_id, c.source_kind, c.source_id, c.text_chunk,
                c.chunk_index, c.created_at, v.distance
            FROM chunks_vec v
            JOIN chunks c ON c.chunk_id = v.rowid
            WHERE v.embedding MATCH ?
              AND v.rowid IN (
                  SELECT chunk_id FROM chunks WHERE {filter_sql}
              )
              AND k = ?
            ORDER BY v.distance
            """,
            (qvec, *params, k),
        ).fetchall()
    finally:
        conn.close()

    return [
        SearchHit(
            chunk_id=r[0],
            source_kind=r[1],
            source_id=r[2],
            text_chunk=r[3],
            chunk_index=r[4],
            created_at_ms=r[5],
            distance=r[6],
        )
        for r in rows
    ]


# ── Admin / maintenance helpers ────────────────────────────────────────


def delete_chunks_for_source(
    user_id: str, source_kind: str, source_id: str,
) -> int:
    """Wipe all chunks for a (user, source_kind, source_id) tuple.

    Used when a source is deleted upstream (uploaded file removed,
    skill uninstalled). Idempotent — returns 0 if nothing matched.
    """
    conn = _open_conn()
    try:
        rows = conn.execute(
            "SELECT chunk_id FROM chunks "
            "WHERE user_id = ? AND source_kind = ? AND source_id = ?",
            (user_id, source_kind, source_id),
        ).fetchall()
        ids = [r[0] for r in rows]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"DELETE FROM chunks WHERE chunk_id IN ({placeholders})", ids,
        )
        conn.execute(
            f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders})", ids,
        )
        conn.commit()
        return len(ids)
    finally:
        conn.close()


def stats(user_id: Optional[str] = None) -> dict:
    """Diagnostic: count chunks by source_kind. For health / debugging."""
    conn = _open_conn()
    try:
        if user_id:
            rows = conn.execute(
                "SELECT source_kind, COUNT(*) FROM chunks "
                "WHERE user_id = ? GROUP BY source_kind",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT source_kind, COUNT(*) FROM chunks GROUP BY source_kind"
            ).fetchall()
        return {"by_kind": dict(rows), "total": sum(r[1] for r in rows)}
    finally:
        conn.close()
