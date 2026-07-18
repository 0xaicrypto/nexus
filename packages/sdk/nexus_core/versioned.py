"""VersionedStore — append-only versioned JSON store with a
movable ``_current`` pointer.

This is the storage primitive shared by:

* **Phase J** (5-namespace curated memory) — each of facts /
  episodes / skills / persona / knowledge is a :class:`VersionedStore`.
* **Phase O** (falsifiable evolution rollback) — when a verdict
  decides ``reverted``, the runner calls
  :meth:`VersionedStore.rollback` to flip ``_current`` back to a
  prior version.

Layout on disk::

    {root}/
    ├── _current.json   {"version": "v0042", "updated_at": ...}
    ├── v0001.json      {data of version 1}
    ├── v0002.json
    └── ...

All version files are immutable once written. Rollback is a single
write to ``_current.json``; the version chain is preserved.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("nexus_core.versioned")


_VERSION_RE = re.compile(r"^v(\d+)\.json$")
_DEFAULT_VERSION_WIDTH = 4   # v0001, v0042, v9999


@dataclasses.dataclass(frozen=True)
class VersionRecord:
    """One entry in a :class:`VersionedStore`'s history."""
    version: str            # e.g. "v0042"
    path: Path              # absolute path on disk
    created_at: float       # POSIX timestamp


class VersionedStore:
    """A directory-backed versioned JSON store.

    Each "version" is a JSON document stored as a separate
    immutable file (``v{N}.json``). The ``_current.json`` pointer
    file names the active version — updated atomically on
    :meth:`propose` and :meth:`rollback`.

    Thread-safe within a single process via an internal lock.
    """

    def __init__(
        self,
        base_dir: str | Path,
        *,
        version_width: int = _DEFAULT_VERSION_WIDTH,
    ):
        """
        Args:
            base_dir: Directory holding the version chain.
            version_width: Zero-pad width for version numbers.
        """
        self._base_dir = Path(base_dir).resolve()
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._width = version_width
        self._lock = threading.RLock()

    # ── Read API ─────────────────────────────────────────────────

    def current_version(self) -> Optional[str]:
        """Label of the currently-active version."""
        ptr = self._read_pointer()
        return ptr.get("version") if ptr else None

    def last_commit_at(self) -> Optional[float]:
        """POSIX timestamp of the most recent ``propose``."""
        ptr = self._read_pointer()
        if not ptr:
            return None
        ts = ptr.get("updated_at")
        return float(ts) if ts is not None else None

    def current(self) -> Optional[Any]:
        """Return the JSON data of the current version."""
        v = self.current_version()
        if v is None:
            return None
        return self._read_version(v)

    def history(self, limit: Optional[int] = None) -> list[VersionRecord]:
        """List versions on disk in chronological order."""
        records: list[VersionRecord] = []
        with self._lock:
            for p in sorted(self._base_dir.iterdir()):
                m = _VERSION_RE.match(p.name)
                if not m:
                    continue
                records.append(VersionRecord(
                    version=p.stem,
                    path=p.resolve(),
                    created_at=p.stat().st_mtime,
                ))
        records.sort(key=lambda r: int(r.version[1:]))
        if limit is not None:
            records = records[:limit]
        return records

    def get(self, version: str) -> Optional[Any]:
        """Read a specific version by label."""
        return self._read_version(version)

    def __len__(self) -> int:
        return sum(
            1 for p in self._base_dir.iterdir()
            if _VERSION_RE.match(p.name)
        )

    # ── Write API ────────────────────────────────────────────────

    def propose(self, data: Any) -> str:
        """Write ``data`` as a new version, advance ``_current``."""
        with self._lock:
            highest = self._highest_existing_version_n()
            new_n = highest + 1
            new_label = f"v{new_n:0{self._width}d}"

            self._write_version(new_label, data)
            self._write_pointer(new_label)

            logger.debug(
                "versioned: %s ← %s", self._base_dir.name, new_label,
            )
            return new_label

    def rollback(self, version: str) -> str:
        """Flip ``_current`` to ``version``."""
        with self._lock:
            if self._read_version(version) is None:
                raise ValueError(
                    f"VersionedStore.rollback: target version "
                    f"{version!r} not found in {self._base_dir}"
                )
            prev = self.current_version() or ""
            self._write_pointer(version)
            logger.info(
                "versioned: %s rolled back %s → %s",
                self._base_dir.name, prev, version,
            )
            return prev

    # ── Internals ────────────────────────────────────────────────

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def _pointer_path(self) -> Path:
        return self._base_dir / "_current.json"

    def _version_path(self, version: str) -> Path:
        return self._base_dir / f"{version}.json"

    def _read_pointer(self) -> Optional[dict]:
        p = self._pointer_path()
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("versioned: pointer read failed (%s): %s", p, e)
            return None

    def _write_pointer(self, version: str) -> None:
        p = self._pointer_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"version": version, "updated_at": time.time()},
                separators=(",", ":"),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(p)

    def _read_version(self, version: str) -> Optional[Any]:
        p = self._version_path(version)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("versioned: version read failed (%s): %s", p, e)
            return None

    def _write_version(self, version: str, data: Any) -> None:
        p = self._version_path(version)
        if p.exists():
            raise FileExistsError(
                f"VersionedStore: version {version} already exists at {p}; "
                f"refusing to overwrite (versions are immutable)."
            )
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _highest_existing_version_n(self) -> int:
        highest = 0
        for p in self._base_dir.iterdir():
            m = _VERSION_RE.match(p.name)
            if m:
                highest = max(highest, int(m.group(1)))
        return highest


__all__ = ["VersionedStore", "VersionRecord"]
