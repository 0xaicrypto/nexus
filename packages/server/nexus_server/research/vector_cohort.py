"""P5 — patient_hash column on vector_index.chunks + cohort filter.

The vector_index lives in its OWN sqlite file (per-install, not the
main app DB), so adding the column happens here lazily at first use.
For migration purposes we expose ``ensure_patient_hash_column`` which
the app startup calls once.

The retrieval layer can then pass ``patient_hashes=[…]`` to the
``cohort_search`` helper which adds a SQL ``WHERE patient_hash IN (…)``
before the vec0 k-NN.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)


def ensure_patient_hash_column() -> bool:
    """Add chunks.patient_hash column if not present. Returns True if
    the column was added (or already existed)."""
    from nexus_server.vector_index import _open_conn  # internal helper
    conn = _open_conn()
    try:
        rows = conn.execute("PRAGMA table_info(chunks)").fetchall()
        cols = {r[1] for r in rows}
        if "patient_hash" in cols:
            return True
        conn.execute(
            "ALTER TABLE chunks ADD COLUMN patient_hash TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_user_patient "
            "ON chunks(user_id, patient_hash)"
        )
        # Backfill from source_id where possible (source_kind='upload' → upload.patient_hash;
        # source_kind='dicom' → dicom_studies.patient_hash). Best-effort.
        try:
            conn.execute(
                """
                UPDATE chunks SET patient_hash = (
                    SELECT u.patient_hash FROM uploads u
                    WHERE u.file_id = chunks.source_id
                )
                WHERE source_kind IN ('upload','caption')
                  AND (patient_hash IS NULL OR patient_hash = '')
                """
            )
        except sqlite3.Error as exc:
            logger.debug("vector_index backfill skipped: %s", exc)
        conn.commit()
        return True
    finally:
        conn.close()


def cohort_search(
    user_id: str,
    embedding: list[float],
    *, patient_hashes: Optional[list[str]] = None,
    top_k: int = 10,
) -> list[dict]:
    """KNN search within an optional cohort filter.

    If ``patient_hashes`` is None, behaves like a normal per-user search.
    """
    from nexus_server.vector_index import _open_conn
    conn = _open_conn()
    try:
        ensure_patient_hash_column()
        if patient_hashes:
            placeholders = ",".join("?" for _ in patient_hashes)
            cond = (
                "AND patient_hash IN ({}) ".format(placeholders)
            )
            args = [user_id, *patient_hashes]
        else:
            cond = ""
            args = [user_id]

        sql = f"""
            WITH knn AS (
                SELECT rowid, distance FROM chunks_vec
                WHERE embedding MATCH ?
                ORDER BY distance LIMIT ?
            )
            SELECT c.chunk_id, c.source_kind, c.source_id,
                   c.chunk_index, c.text_chunk, c.patient_hash,
                   k.distance
            FROM knn k
            JOIN chunks c ON c.chunk_id = k.rowid
            WHERE c.user_id = ? {cond}
            ORDER BY k.distance
        """
        all_args = [bytes(embedding) if isinstance(embedding, (bytes, bytearray))
                    else _f32_blob(embedding), top_k * 4, *args]
        rows = conn.execute(sql, all_args).fetchall()
        out = [
            dict(chunk_id=r[0], source_kind=r[1], source_id=r[2],
                 chunk_index=r[3], text_chunk=r[4], patient_hash=r[5],
                 distance=r[6])
            for r in rows
        ]
        return out[:top_k]
    finally:
        conn.close()


def _f32_blob(vec) -> bytes:
    import struct
    return struct.pack(f"<{len(vec)}f", *vec)
