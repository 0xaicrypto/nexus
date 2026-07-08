"""
BNBChainArtifactService — ADK-compatible ArtifactService backed by on-chain state.

Artifacts (generated code, analysis results, files) are stored with
content-hash addressing in the object store. The manifest (which artifacts
exist, their versions) is stored on BSC via the StateManager.
"""

import json
import time
from typing import Any, Optional, Union

from google.genai import types
from google.adk.artifacts.base_artifact_service import (
    BaseArtifactService,
    ArtifactVersion,
)

from .state import StateManager


class BNBChainArtifactService(BaseArtifactService):
    """Artifact service backed by BNBChain state layer."""

    def __init__(self, state_manager: StateManager):
        self._state = state_manager
        self._manifest_prefix = "__artifacts__"

    def _manifest_key(self, app_name: str, user_id: str,
                      session_id: Optional[str] = None) -> str:
        parts = [self._manifest_prefix, app_name, user_id]
        if session_id:
            parts.append(session_id)
        return ":".join(parts)

    def _load_manifest(self, key: str) -> dict:
        """Load artifact manifest from the object store (chain mode) or local JSON (local mode)."""
        if self._state.mode == "chain":
            # In chain mode, store manifest in the object store keyed by a deterministic hash.
            # We keep an in-memory cache of manifest hashes.
            if not hasattr(self, "_manifest_cache"):
                self._manifest_cache = {}
            manifest_hash = self._manifest_cache.get(key)
            if manifest_hash:
                data = self._state.load_json(manifest_hash)
                if data:
                    return data
            return {}
        else:
            # Local mode: use the chain JSON file
            chain = self._state._read_chain()
            manifest_hash = chain.get("artifact_manifests", {}).get(key)
            if manifest_hash:
                data = self._state.load_json(manifest_hash)
                if data:
                    return data
            return {}

    def _save_manifest(self, key: str, manifest: dict) -> None:
        """Save artifact manifest to the object store (chain mode) or local JSON (local mode)."""
        # Extract agent_id from key (format: __artifacts__:appName:userId[:sessionId])
        parts = key.split(":")
        agent_id = parts[1] if len(parts) > 1 else "unknown"
        folder = self._state.agent_folder(agent_id)
        obj_path = self._state.storage_path(
            folder, "artifacts", "", filename="_manifest.json",
        )
        content_hash = self._state.store_json(manifest, object_path=obj_path)
        if self._state.mode == "chain":
            # In chain mode, cache the manifest hash in memory.
            # The manifest data itself is stored in the object store.
            if not hasattr(self, "_manifest_cache"):
                self._manifest_cache = {}
            self._manifest_cache[key] = content_hash
        else:
            # Local mode: persist to chain JSON file
            chain = self._state._read_chain()
            if "artifact_manifests" not in chain:
                chain["artifact_manifests"] = {}
            chain["artifact_manifests"][key] = content_hash
            self._state._write_chain(chain)

    def _part_to_bytes(self, artifact: Union[types.Part, dict[str, Any], bytes, str]) -> tuple[bytes, Optional[str]]:
        """Convert a Part to bytes for storage. Returns (data, mime_type)."""
        if isinstance(artifact, bytes):
            return artifact, "application/octet-stream"
        if isinstance(artifact, str):
            return artifact.encode("utf-8"), "text/plain"
        if isinstance(artifact, dict):
            return json.dumps(artifact).encode("utf-8"), "application/json"
        if hasattr(artifact, "inline_data") and artifact.inline_data:
            return artifact.inline_data.data, artifact.inline_data.mime_type
        if hasattr(artifact, "text") and artifact.text:
            return artifact.text.encode("utf-8"), "text/plain"
        return json.dumps(artifact.model_dump(exclude_none=True)).encode("utf-8"), "application/json"

    def _bytes_to_part(self, data: bytes, mime_type: Optional[str] = None) -> types.Part:
        """Convert stored bytes back to a Part."""
        if mime_type and mime_type == "text/plain":
            return types.Part.from_text(text=data.decode("utf-8"))
        if mime_type and not mime_type.startswith("text/"):
            return types.Part.from_bytes(data=data, mime_type=mime_type)
        try:
            return types.Part.from_text(text=data.decode("utf-8"))
        except Exception:
            return types.Part.from_bytes(data=data, mime_type=mime_type or "application/octet-stream")

    # ── ADK Interface Implementation ─────────────────────────────────

    async def save_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        artifact: Union[types.Part, dict[str, Any]],
        session_id: Optional[str] = None,
        custom_metadata: Optional[dict[str, Any]] = None,
    ) -> int:
        key = self._manifest_key(app_name, user_id, session_id)
        manifest = self._load_manifest(key)

        # Store artifact data in the object store with readable path
        data, mime_type = self._part_to_bytes(artifact)
        folder = self._state.agent_folder(app_name)
        obj_path = self._state.storage_path(
            folder, "artifacts", "",
            filename=filename,
        )
        content_hash = self._state.store_data(data, object_path=obj_path)

        # Determine version
        versions = manifest.get(filename, [])
        new_version = len(versions) + 1

        version_entry = {
            "version": new_version,
            "content_hash": content_hash,
            "mime_type": mime_type,
            "custom_metadata": custom_metadata or {},
            "create_time": time.time(),
        }
        versions.append(version_entry)
        manifest[filename] = versions
        self._save_manifest(key, manifest)

        print(f"  [SDK] Artifact saved: {filename} v{new_version} (chain-backed)")
        return new_version

    async def load_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
        version: Optional[int] = None,
    ) -> Optional[types.Part]:
        key = self._manifest_key(app_name, user_id, session_id)
        manifest = self._load_manifest(key)

        versions = manifest.get(filename, [])
        if not versions:
            return None

        if version is not None:
            entry = next((v for v in versions if v["version"] == version), None)
        else:
            entry = versions[-1]  # latest

        if entry is None:
            return None

        data = self._state.load_data(entry["content_hash"])
        if data is None:
            return None

        return self._bytes_to_part(data, entry.get("mime_type"))

    async def list_artifact_keys(
        self, *, app_name: str, user_id: str, session_id: Optional[str] = None
    ) -> list[str]:
        key = self._manifest_key(app_name, user_id, session_id)
        manifest = self._load_manifest(key)
        return list(manifest.keys())

    async def delete_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
    ) -> None:
        key = self._manifest_key(app_name, user_id, session_id)
        manifest = self._load_manifest(key)
        if filename in manifest:
            del manifest[filename]
            self._save_manifest(key, manifest)
            print(f"  [SDK] Artifact deleted: {filename}")

    async def list_versions(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
    ) -> list[int]:
        key = self._manifest_key(app_name, user_id, session_id)
        manifest = self._load_manifest(key)
        versions = manifest.get(filename, [])
        return [v["version"] for v in versions]

    async def list_artifact_versions(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
    ) -> list[ArtifactVersion]:
        key = self._manifest_key(app_name, user_id, session_id)
        manifest = self._load_manifest(key)
        versions = manifest.get(filename, [])
        return [
            ArtifactVersion(
                version=v["version"],
                canonical_uri=f"rune://{v['content_hash']}",
                custom_metadata=v.get("custom_metadata", {}),
                create_time=v.get("create_time", 0.0),
                mime_type=v.get("mime_type"),
            )
            for v in versions
        ]

    async def get_artifact_version(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
        version: Optional[int] = None,
    ) -> Optional[ArtifactVersion]:
        key = self._manifest_key(app_name, user_id, session_id)
        manifest = self._load_manifest(key)
        versions = manifest.get(filename, [])
        if not versions:
            return None
        if version is not None:
            entry = next((v for v in versions if v["version"] == version), None)
        else:
            entry = versions[-1]
        if entry is None:
            return None
        return ArtifactVersion(
            version=entry["version"],
            canonical_uri=f"rune://{entry['content_hash']}",
            custom_metadata=entry.get("custom_metadata", {}),
            create_time=entry.get("create_time", 0.0),
            mime_type=entry.get("mime_type"),
        )
