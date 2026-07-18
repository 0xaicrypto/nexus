"""
nexus_core — Persistent Agent Infrastructure.

"Runtime is temporary; identity is eternal."

Quick start::

    import nexus_core

    rt = nexus_core.local()                          # Zero config (file-backed)
    rt = nexus_core.builder().mock_backend().build() # Unit tests / custom config

Architecture:

  Entry points (top-level functions):
    - nexus_core.local() / builder()

  Core types:
    - StorageBackend     — strategy pattern, pluggable persistence.
    - AgentRuntime       — facade returned by the entry-point functions.
    - SessionProvider, ArtifactProvider, TaskProvider
    - Builder            — fluent runtime builder.

  Backends (Strategy implementations):
    - LocalBackend       — file-based (dev/demo)
    - MockBackend        — in-memory (unit tests)

  Framework adapters:
    - adapters.adk       — Google ADK
    - adapters.langgraph — LangGraph
    - adapters.crewai    — CrewAI
"""

__version__ = "0.5.0"

# ── Entry points + Builder ─────────────────────────────────────────────
from .backends.local import LocalBackend
from .backends.mock import MockBackend
from .builder import Builder, builder, local

# ── Core abstractions ──────────────────────────────────────────────────
from .core.backend import StorageBackend
from .core.flush import FlushBuffer, FlushPolicy, WriteAheadLog
from .core.models import Artifact, Checkpoint
from .core.providers import (
    AgentRuntime,
    ArtifactProvider,
    SessionProvider,
    TaskProvider,
)
from .llm import LLMClient, LLMProvider
from .mcp import MCPClient, MCPManager, MCPServerConfig
from .skills import SkillManager
from .tools import BaseTool, ToolCall, ToolRegistry, ToolResult, URLReaderTool, WebSearchTool
from .utils import load_dotenv, robust_json_parse

# ── Adapter registry ──────────────────────────────────────────────────
from .adapters.registry import AdapterRegistry

# ── Generic LLM utilities ──────────────────────────────────────────────
from .distiller import (
    DISTILL_INPUT_CHAR_BUDGET,
    DISTILL_OUTPUT_CHAR_BUDGET,
    DISTILL_SYSTEM_PROMPT,
    distill,
    extract_text,
)
from .providers import (
    ArtifactProviderImpl,
    SessionProviderImpl,
    TaskProviderImpl,
)

# ── Live thinking telemetry ────────────────────────────────────────────
from .thinking import ThinkingEmitter, ThinkingEvent

# ── Contracts (ABC) ───────────────────────────────────────────────────
from .contracts import CheckResult, ContractEngine, ContractSpec, DriftScore, Rule

# ── Falsifiable evolution (Phase O) ────────────────────────────────────
from .evolution import (
    DriftThresholds,
    EvolutionProposal,
    EvolutionRevert,
    EvolutionVerdict,
    FixMatch,
    ObservedRegression,
    TaskKindPrediction,
    make_proposal_event,
    make_revert_event,
    make_verdict_event,
    score_verdict,
)
from .memory import (
    CuratedMemory,
    Episode,
    EpisodesStore,
    Event,
    EventLog,
    EventLogCompactor,
    Fact,
    FactsStore,
    KnowledgeArticle,
    KnowledgeStore,
    LearnedSkill,
    PersonaStore,
    PersonaVersion,
    SkillsStore,
)

# ── Recursive Language Model ───────────────────────────────────────────
from .rlm import (
    RLMConfig,
    RLMResult,
    RLMRunner,
    run_rlm,
)
from .rlm import (
    TrajectoryEntry as RLMTrajectoryEntry,
)

__all__ = [
    # Entry points + builder
    "local",
    "builder",
    "Builder",
    # Core types
    "StorageBackend",
    "AgentRuntime",
    "SessionProvider",
    "ArtifactProvider",
    "TaskProvider",
    "Checkpoint",
    "Artifact",
    "FlushPolicy",
    "FlushBuffer",
    "WriteAheadLog",
    "MockBackend",
    "LocalBackend",
    "SessionProviderImpl",
    "ArtifactProviderImpl",
    "TaskProviderImpl",
    "AdapterRegistry",
    # Tools & MCP
    "BaseTool",
    "ToolResult",
    "ToolCall",
    "ToolRegistry",
    "LLMClient",
    "LLMProvider",
    "MCPClient",
    "MCPServerConfig",
    "MCPManager",
    # Skills
    "SkillManager",
    # Built-in tools
    "WebSearchTool",
    "URLReaderTool",
    # Memory (DPM)
    "EventLog",
    "Event",
    "CuratedMemory",
    "EventLogCompactor",
    # Memory namespaces
    "Episode",
    "EpisodesStore",
    "Fact",
    "FactsStore",
    "LearnedSkill",
    "SkillsStore",
    "PersonaVersion",
    "PersonaStore",
    "KnowledgeArticle",
    "KnowledgeStore",
    # Contracts (ABC)
    "ContractEngine",
    "ContractSpec",
    "CheckResult",
    "DriftScore",
    "Rule",
    # Recursive Language Model
    "RLMRunner",
    "RLMConfig",
    "RLMResult",
    "RLMTrajectoryEntry",
    "run_rlm",
    # Falsifiable evolution
    "EvolutionProposal",
    "EvolutionVerdict",
    "EvolutionRevert",
    "TaskKindPrediction",
    "DriftThresholds",
    "FixMatch",
    "ObservedRegression",
    "score_verdict",
    "make_proposal_event",
    "make_verdict_event",
    "make_revert_event",
    # Utilities
    "robust_json_parse",
    "load_dotenv",
]
