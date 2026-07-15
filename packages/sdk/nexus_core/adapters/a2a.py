"""
A2A Stateless Adapter — bridges A2A protocol with BNBChain state layer.

Provides helpers to:
  1. Create chain-backed A2A agents, each with its own runtime
  2. Build AgentCards with on-chain identity (ERC-8004 agentId)
  3. Handle A2A messages (server-side: handle_message)
  4. Send A2A messages to remote agents (client-side: send_message)
  5. Track multi-agent collaboration via shared context_id

Architecture: Each agent runs in its own runtime (separate process/server
in production). The runtime_id is recorded on chain so the network knows
which runtime is currently executing a given agent. In production each
runtime exposes an HTTP endpoint; for Phase 1 we use an A2ARuntime
wrapper that simulates this boundary.

    ┌─────────────────────┐     ┌─────────────────────┐
    │  Runtime-1 (laptop) │     │  Runtime-2 (cloud)   │
    │  ┌───────────────┐  │     │  ┌───────────────┐   │
    │  │  Orchestrator │  │ A2A │  │  Data Analyst │   │
    │  │  Agent        │──┼────▶│  │  Agent        │   │
    │  └───────────────┘  │     │  └───────────────┘   │
    └─────────────────────┘     └──────────────────────┘
           │                          Chain (shared)
           │ A2A                     ┌──────────────┐
           ▼                         │ BSC          │
    ┌─────────────────────┐         │  anchoring   │
    │  Runtime-3 (server) │         └──────────────┘
    │  ┌───────────────┐  │
    │  │  Risk Assessor│  │
    │  │  Agent        │  │
    │  └───────────────┘  │
    └─────────────────────┘
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from a2a.types import (
    Artifact,
    Message,
    Role,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)

from nexus_core.adapters.a2a_task_store import BNBChainTaskStore
from nexus_core.flush import FlushPolicy
from nexus_core.state import StateManager


@dataclass
class A2AAgentConfig:
    """Configuration for a chain-backed A2A agent."""
    agent_id: str
    name: str
    description: str
    skills: list[dict] = field(default_factory=list)
    url: str = "http://localhost:8000"


class StatelessA2AAgent:
    """
    A stateless A2A agent backed by BNBChain.

    Each agent has:
      - An ERC-8004 identity (agentId)
      - Its own runtime_id (which runtime is executing this agent)
      - A BNBChainTaskStore (tasks persisted to chain)
      - An AgentCard (A2A discovery metadata)
      - An execute() function (the actual agent logic)

    The agent holds NO state in memory. Everything is on chain.

    IMPORTANT: In production, each agent runs in its own runtime
    (separate process / container / server). The runtime_id is
    recorded on chain via AgentStateExtension.active_runtime so
    the network knows which endpoint currently hosts this agent.
    """

    def __init__(
        self,
        config: A2AAgentConfig,
        state_manager: StateManager,
        runtime_id: str = "default-runtime",
        execute_fn: Optional[Callable] = None,
        flush_policy: Optional[FlushPolicy] = None,
    ):
        self.config = config
        self.runtime_id = runtime_id
        self._state = state_manager
        self.task_store = BNBChainTaskStore(
            state_manager, config.agent_id, flush_policy=flush_policy,
        )
        self._execute_fn = execute_fn

        # Ensure agent is registered on chain
        existing = self._state.get_agent(config.agent_id)
        if existing is None:
            self._state.register_agent(config.agent_id, owner="system")

        # Record which runtime is hosting this agent.
        # For freshly minted agents, state_root may be empty (hasState=False).
        current_root = existing.state_root if existing and existing.state_root else ""
        self._state.update_state_root(
            config.agent_id,
            current_root,
            runtime_id=runtime_id,
        )

    def get_agent_card(self) -> dict:
        """
        Build an A2A AgentCard.

        In production this is served at /.well-known/agent-card.json
        and includes the ERC-8004 agentId for on-chain identity verification.
        """
        return {
            "name": self.config.name,
            "description": self.config.description,
            "url": self.config.url,
            "skills": self.config.skills,
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            # BNBChain extension: on-chain identity + runtime info
            "metadata": {
                "bnbchain": {
                    "agentId": self.config.agent_id,
                    "protocol": "ERC-8004",
                    "stateExtension": "AgentStateExtension",
                    "activeRuntime": self.runtime_id,
                }
            },
        }

    async def handle_message(
        self,
        message: Message,
        task_id: Optional[str] = None,
        context_id: Optional[str] = None,
    ) -> Task:
        """
        Handle an incoming A2A message/send request.

        This is the server-side A2A flow (called when this agent's
        runtime receives an A2A JSON-RPC message/send):
          1. Create or load Task from chain
          2. Add incoming message to history
          3. Execute agent logic
          4. Save updated Task to chain
          5. Return Task with results

        Args:
            message: The incoming A2A Message
            task_id: Existing task ID (for continuation), or None for new
            context_id: Shared context for multi-agent collaboration
        """
        context_id = context_id or str(uuid.uuid4())
        agent_name = self.config.name
        agent_id = self.config.agent_id
        tid_short = (task_id or "new")[:16]

        # Extract message preview for logging
        msg_preview = ""
        if message.parts:
            part = message.parts[0]
            text = part.text if hasattr(part, 'text') else (part.root.text if hasattr(part, 'root') and hasattr(part.root, 'text') else str(part))
            msg_preview = text[:60] + ("…" if len(text) > 60 else "")

        print(f"    📨 [{agent_name}] Received message: \"{msg_preview}\"")
        print(f"       task={tid_short}…  context={context_id[:12]}…  runtime={self.runtime_id}")

        # Load or create task
        task = None
        if task_id:
            task = await self.task_store.get(task_id)

        if task is None:
            task_id = task_id or str(uuid.uuid4())
            task = Task(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.submitted),
                history=[],
                artifacts=[],
            )
            print(f"    📋 [{agent_name}] Created new task: {task_id[:16]}…")

        # Add incoming message to history
        if task.history is None:
            task.history = []
        task.history.append(message)

        # Update status to working
        task.status = TaskStatus(
            state=TaskState.working,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        print(f"    🔄 [{agent_name}] Status: submitted → working")
        await self.task_store.save(task)

        # Execute agent logic
        if self._execute_fn:
            print(f"    ⚙️  [{agent_name}] Executing agent logic...")
            task = await self._execute_fn(task, self._state)

        # Mark completed
        task.status = TaskStatus(
            state=TaskState.completed,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

        # Extract response preview for logging
        resp_preview = ""
        if task.history and len(task.history) > 1:
            last_msg = task.history[-1]
            if last_msg.parts:
                part = last_msg.parts[0]
                text = part.text if hasattr(part, 'text') else (part.root.text if hasattr(part, 'root') and hasattr(part.root, 'text') else str(part))
                resp_preview = text[:60] + ("…" if len(text) > 60 else "")

        print(f"    ✅ [{agent_name}] Status: working → completed")
        if resp_preview:
            print(f"       Response: \"{resp_preview}\"")
        if task.artifacts:
            for art in task.artifacts:
                art_name = art.name or "unnamed"
                print(f"       Artifact: {art_name}")
        await self.task_store.save(task)

        return task


class A2ARuntime:
    """
    Represents a single runtime hosting one agent.

    In production each A2ARuntime is a separate process / container
    running an HTTP server that exposes the A2A JSON-RPC endpoint.
    Multiple runtimes connect to the same chain (shared state layer).

    For Phase 1 we simulate this by giving each A2ARuntime its own
    StateManager (pointing to the same on-disk chain directory) and
    its own runtime_id.

    Usage:
        runtime = A2ARuntime(
            runtime_id="runtime-analyst-01",
            agent_config=A2AAgentConfig(...),
            state_dir="/shared/chain/state",
            execute_fn=my_agent_logic,
        )
        # This agent is now "running" in its own runtime
        task = await runtime.agent.handle_message(msg)
    """

    def __init__(
        self,
        runtime_id: str,
        agent_config: A2AAgentConfig,
        state_dir: str,
        execute_fn: Optional[Callable] = None,
        state_manager: Optional[StateManager] = None,
    ):
        self.runtime_id = runtime_id
        self.config = agent_config
        # Each runtime has its own StateManager instance
        # (in production: its own web3 connection)
        self.state_manager = state_manager or StateManager(base_dir=state_dir)
        self.agent = StatelessA2AAgent(
            config=agent_config,
            state_manager=self.state_manager,
            runtime_id=runtime_id,
            execute_fn=execute_fn,
        )

    async def send_message_to(
        self,
        target_runtime: "A2ARuntime",
        message: Message,
        task_id: Optional[str] = None,
        context_id: Optional[str] = None,
    ) -> Task:
        """
        Send an A2A message from this runtime's agent to another runtime's agent.

        In production this is an HTTP POST to the target agent's URL:
            POST {target.config.url}/a2a  (JSON-RPC 2.0: message/send)

        For Phase 1 we call the target runtime's handle_message directly,
        but the boundary is explicit: the two runtimes have separate
        StateManager instances and separate runtime_ids.
        """
        sender = self.config.name
        receiver = target_runtime.config.name

        # Extract message text for logging
        msg_text = ""
        if message.parts:
            part = message.parts[0]
            msg_text = part.text if hasattr(part, 'text') else (part.root.text if hasattr(part, 'root') and hasattr(part.root, 'text') else str(part))

        print("\n    ╭─── A2A message/send ───────────────────────────────────")
        print(f"    │ From: {sender} ({self.runtime_id})")
        print(f"    │ To:   {receiver} ({target_runtime.runtime_id})")
        print(f"    │ Msg:  \"{msg_text[:70]}{'…' if len(msg_text) > 70 else ''}\"")
        if task_id:
            print(f"    │ Task: {task_id[:24]}…")
        print("    ╰─────────────────────────────────────────────────────────")

        result = await target_runtime.agent.handle_message(
            message=message,
            task_id=task_id,
            context_id=context_id,
        )

        print("    ╭─── A2A response ────────────────────────────────────────")
        print(f"    │ {receiver} → {sender}: task {result.id[:16]}… = {result.status.state.value}")
        if result.artifacts:
            for art in result.artifacts:
                print(f"    │ Artifact: {art.name or 'unnamed'}")
        print("    ╰─────────────────────────────────────────────────────────")

        return result

    def inspect_chain_task(self, task_id: str):
        """Read a task directly from chain (no A2A — direct chain query)."""
        return self.state_manager.get_task(task_id)


def create_message(text: str, role: Role = Role.user,
                   reference_task_ids: list[str] = None) -> Message:
    """Helper to create an A2A Message."""
    return Message(
        message_id=str(uuid.uuid4()),
        role=role,
        parts=[TextPart(text=text)],
        reference_task_ids=reference_task_ids,
    )


def create_artifact(name: str, text: str, artifact_id: str = None) -> Artifact:
    """Helper to create an A2A Artifact."""
    return Artifact(
        artifact_id=artifact_id or str(uuid.uuid4()),
        name=name,
        parts=[TextPart(text=text)],
    )
