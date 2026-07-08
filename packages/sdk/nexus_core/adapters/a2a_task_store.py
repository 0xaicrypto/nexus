"""
BNBChainTaskStore — A2A-compatible TaskStore backed by on-chain state.

This is the bridge between the A2A protocol and BNBChain:
  - A2A SDK calls save()/get()/delete() on Task objects
  - We serialize the full Task to the object store (bulk data)
  - We store the task_id, status, and state_hash on BSC (TaskStateManager)

Write architecture (respects FlushPolicy):
  - Task creation (createTask):         Always sync to BSC (critical)
  - Terminal states (completed/failed): Always sync to BSC (critical)
  - Interim updates (working):          object store only (batched, BSC deferred)

  When sync_task_transitions=True (default), any status change triggers
  a BSC write.  Set to False to only sync on explicit flush().

On-chain mapping:
  BSC (TaskStateManager):  task_id, agent_id, status.state, state_hash, version
  Object store:            full serialized Task JSON (addressed by state_hash)
"""

import json
import logging
import time
from typing import Optional

from a2a.server.tasks.task_store import TaskStore
from a2a.types import Task, TaskState
from a2a.server.request_handlers.request_handler import ServerCallContext

from nexus_core.state import StateManager
from nexus_core.flush import FlushPolicy

logger = logging.getLogger("nexus_core.a2a_task_store")

# Terminal A2A states — always sync to BSC
_TERMINAL_STATES = {TaskState.completed, TaskState.failed, TaskState.canceled, TaskState.rejected}


class BNBChainTaskStore(TaskStore):
    """
    A2A TaskStore implementation backed by BNBChain state layer.

    Every A2A Task is persisted as:
      1. Full Task JSON → object store (content-hash addressed)
      2. task_id + state + hash → BSC TaskStateManager contract

    With FlushPolicy:
      - Critical writes (create, terminal states) always go to BSC
      - Interim writes (working/input_required) go to the object store only
        unless sync_task_transitions=True or sync_every policy is active
    """

    def __init__(
        self,
        state_manager: StateManager,
        agent_id: str,
        flush_policy: Optional[FlushPolicy] = None,
    ):
        self._state = state_manager
        self._agent_id = agent_id
        self._policy = flush_policy or FlushPolicy()
        # Track versions for optimistic concurrency
        self._versions: dict[str, int] = {}
        # Track last synced status to detect transitions
        self._last_synced_status: dict[str, str] = {}
        # Store-only buffer: tasks with stored data but no BSC update yet
        self._pending_bsc: dict[str, str] = {}  # task_id → latest content_hash

    @property
    def flush_policy(self) -> FlushPolicy:
        return self._policy

    @flush_policy.setter
    def flush_policy(self, policy: FlushPolicy) -> None:
        self._policy = policy

    def flush(self) -> int:
        """
        Flush all pending BSC writes for tasks that were deferred.

        Returns number of tasks flushed to BSC.
        """
        count = 0
        for task_id, content_hash in list(self._pending_bsc.items()):
            try:
                status = self._last_synced_status.get(task_id, "running")
                expected_version = self._versions.get(task_id, 0)
                record = self._state.update_task(
                    task_id, content_hash,
                    status=status,
                    expected_version=expected_version,
                )
                self._versions[task_id] = record.version
                del self._pending_bsc[task_id]
                count += 1
            except Exception as e:
                logger.warning("Deferred BSC flush failed for task %s: %s", task_id, e)
        return count

    def _a2a_state_to_chain_status(self, state: TaskState) -> str:
        """Map A2A TaskState enum to our chain status string."""
        mapping = {
            TaskState.submitted: "pending",
            TaskState.working: "running",
            TaskState.completed: "completed",
            TaskState.failed: "failed",
            TaskState.canceled: "failed",
            TaskState.rejected: "failed",
            TaskState.input_required: "running",
            TaskState.auth_required: "running",
            TaskState.unknown: "pending",
        }
        return mapping.get(state, "pending")

    def _serialize_task(self, task: Task) -> dict:
        """Serialize A2A Task to JSON-compatible dict."""
        return task.model_dump(mode="json", exclude_none=True)

    def _deserialize_task(self, data: dict) -> Task:
        """Deserialize A2A Task from stored dict."""
        return Task.model_validate(data)

    def _should_sync_bsc(self, task: Task) -> bool:
        """Decide whether this save should write to BSC immediately."""
        # sync_every policy: always sync
        if self._policy.every_n_events == 1 and self._policy.interval_seconds == 0:
            return True

        state = task.status.state

        # Terminal states: always sync (data integrity)
        if state in _TERMINAL_STATES:
            return True

        # New task (not yet on chain): always sync
        if task.id not in self._versions:
            return True

        # Status transition: sync if policy says so
        if self._policy.sync_task_transitions:
            last_status = self._last_synced_status.get(task.id)
            current_status = self._a2a_state_to_chain_status(state)
            if last_status != current_status:
                return True

        return False

    def _extract_artifacts(self, task: Task) -> None:
        """
        Extract A2A task artifacts and store each as a separate object.

        A2A artifacts live inside the Task JSON blob, but for browsability
        (DCellar, CLI) we also store each artifact as an individual file at:
            rune/agents/{agentId}/artifacts/{filename}

        This runs on terminal states (completed/failed) so each artifact
        gets its own readable storage path.
        """
        if not task.artifacts:
            return

        import hashlib as _hl

        for artifact in task.artifacts:
            # Extract name and data from the A2A Artifact
            art_name = getattr(artifact, "name", None) or "unnamed"

            # A2A Artifact has .parts — each part may have .text or .inline_data
            parts_data = []
            for part in (artifact.parts or []):
                if hasattr(part, "text") and part.text:
                    parts_data.append(part.text.encode("utf-8"))
                elif hasattr(part, "root"):
                    # Pydantic model wrapper
                    root = part.root
                    if hasattr(root, "text") and root.text:
                        parts_data.append(root.text.encode("utf-8"))
                    elif hasattr(root, "inline_data") and root.inline_data:
                        parts_data.append(root.inline_data.data)
                elif hasattr(part, "inline_data") and part.inline_data:
                    parts_data.append(part.inline_data.data)

            if not parts_data:
                continue

            data = b"\n".join(parts_data)
            folder = self._state.agent_folder(self._agent_id)
            obj_path = self._state.storage_path(
                folder, "artifacts", "",
                filename=art_name,
            )
            content_hash = self._state.store_data(data, object_path=obj_path)
            print(f"      📦 [ARTIFACT] GF ← {art_name} ({len(data)} bytes) -> {content_hash[:16]}…")

    async def save(
        self, task: Task, context: ServerCallContext | None = None
    ) -> None:
        """
        Save or update an A2A Task.

        Flow (with default policy):
          1. Always: serialize Task → object store → content_hash
          2. If critical (new/terminal/transition): write to BSC immediately
          3. If interim: defer BSC write (content_hash buffered for later flush)
          4. On terminal states: extract artifacts as individual stored objects
        """
        # Step 1: Always persist full task data to the object store
        task_data = self._serialize_task(task)
        # Serialize once, hash once, store raw bytes (avoids double-serialization).
        data_bytes = json.dumps(task_data, default=str, sort_keys=True).encode("utf-8")
        import hashlib
        chash = hashlib.sha256(data_bytes).hexdigest()
        folder = self._state.agent_folder(self._agent_id)
        obj_path = self._state.storage_path(
            folder, "tasks", chash, sub_key=task.id,
        )
        content_hash = self._state.store_data(data_bytes, object_path=obj_path)

        chain_status = self._a2a_state_to_chain_status(task.status.state)

        # Step 2: Create task on BSC if it doesn't exist yet
        existing = self._state.get_task(task.id)
        if existing is None:
            self._state.create_task(task.id, self._agent_id)
            self._versions[task.id] = 0
            tid_short = task.id[:16]
            print(f"      💾 [SYNC] BSC  ← createTask({tid_short}…, agent={self._agent_id})")

        # Step 3: Decide whether to sync BSC now or defer
        tid_short = task.id[:16]
        if self._should_sync_bsc(task):
            expected_version = self._versions.get(task.id, 0)
            record = self._state.update_task(
                task.id, content_hash,
                status=chain_status,
                expected_version=expected_version,
            )
            self._versions[task.id] = record.version
            self._last_synced_status[task.id] = chain_status
            # Clear from pending (if was deferred earlier)
            self._pending_bsc.pop(task.id, None)
            print(f"      💾 [SYNC] BSC  ← updateTask({tid_short}…) status={chain_status} v{record.version}")
            print(f"      💾 [SYNC] GF   ← {content_hash[:16]}… ({len(json.dumps(task_data))} bytes)")
        else:
            # Defer BSC write — the object store has the latest data
            self._pending_bsc[task.id] = content_hash
            self._last_synced_status[task.id] = chain_status
            print(f"      💾 [DEFER] GF  ← {content_hash[:16]}… (BSC deferred, status={chain_status})")

        # Step 4: On terminal states, extract artifacts as individual files
        if task.status.state in _TERMINAL_STATES:
            self._extract_artifacts(task)

    async def get(
        self, task_id: str, context: ServerCallContext | None = None
    ) -> Task | None:
        """
        Load an A2A Task from chain.

        Checks pending (store-only) data first, then falls back to BSC.
        """
        # Check if we have a newer version in the store (deferred BSC write)
        pending_hash = self._pending_bsc.get(task_id)
        if pending_hash:
            task_data = self._state.load_json(pending_hash)
            if task_data:
                task = self._deserialize_task(task_data)
                print(f"  [A2A TaskStore] Task loaded from buffer: {task_id}")
                return task

        # Read task metadata from BSC
        record = self._state.get_task(task_id)
        if record is None or not record.state_hash:
            return None

        # Load full task data from the object store
        task_data = self._state.load_json(record.state_hash)
        if task_data is None:
            return None

        # Track version for future updates
        self._versions[task_id] = record.version

        task = self._deserialize_task(task_data)
        print(f"  [A2A TaskStore] Task loaded from chain: {task_id} [{record.status}]")
        return task

    async def delete(
        self, task_id: str, context: ServerCallContext | None = None
    ) -> None:
        """
        Mark a task as deleted on chain.

        Note: stored data is retained (content-hash objects are immutable).
        The BSC record is updated to 'failed' status to indicate deletion.
        """
        record = self._state.get_task(task_id)
        if record is not None:
            expected_version = self._versions.get(task_id)
            self._state.update_task(
                task_id, record.state_hash,
                status="failed",
                expected_version=expected_version,
            )
            self._versions.pop(task_id, None)
            self._last_synced_status.pop(task_id, None)
            self._pending_bsc.pop(task_id, None)
            print(f"  [A2A TaskStore] Task deleted from chain: {task_id}")
