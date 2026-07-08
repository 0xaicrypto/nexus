"""
nexus_core — Persistent, Verifiable Agent Infrastructure on BNBChain.

"Runtime is temporary; identity is eternal."

Quick start::

    import nexus_core

    rt = nexus_core.local()                          # Zero config (file-backed)
    rt = nexus_core.testnet(private_key="0x...")     # BSC testnet anchoring
    rt = nexus_core.builder().mock_backend().build() # Unit tests / custom config

Architecture:

  Entry points (top-level functions):
    - nexus_core.local() / testnet() / mainnet() / builder()

  Core types:
    - StorageBackend     — strategy pattern, pluggable persistence.
    - AgentRuntime       — facade returned by the entry-point functions;
                           bundles the 5 sub-providers below.
    - SessionProvider, ArtifactProvider, TaskProvider,
      ImpressionProvider — abstract interfaces for the 5 concerns.
    - Builder            — fluent runtime builder.

  Social Protocol:
    - social.gossip      — async/sync agent-to-agent gossip
    - social.profile     — agent profile generation + discovery
    - social.graph       — social graph queries + propagation

  Backends (Strategy implementations):
    - LocalBackend       — file-based (dev/demo)
    - ChainBackend       — local store + BSC anchoring (production)
    - MockBackend        — in-memory (unit tests)

  Framework adapters:
    - adapters.adk       — Google ADK
    - adapters.langgraph — LangGraph
    - adapters.crewai    — CrewAI
    - adapters.a2a       — A2A protocol (StatelessA2AAgent, A2ARuntime)
"""

__version__ = "0.5.0"

# ── Entry points + Builder ─────────────────────────────────────────────
from .builder import local, testnet, mainnet, builder, Builder

# ── Core abstractions ──────────────────────────────────────────────────
from .core.backend import StorageBackend
from .core.providers import (
    AgentRuntime,
    SessionProvider,
    ArtifactProvider,
    TaskProvider,
    ImpressionProvider,
)
from .core.models import (
    Checkpoint, Artifact,
    Impression, ImpressionDimensions, ImpressionSummary, NetworkStats,
    GossipMessage, GossipSession, AgentProfile,
)
from .core.flush import FlushPolicy, FlushBuffer, WriteAheadLog
from .utils import robust_json_parse, load_dotenv
from .tools import BaseTool, ToolResult, ToolCall, ToolRegistry, WebSearchTool, URLReaderTool
from .llm import LLMClient, LLMProvider
from .mcp import MCPClient, MCPServerConfig, MCPManager
from .skills import SkillManager

# ── Backends ───────────────────────────────────────────────────────────
from .backends.mock import MockBackend
from .backends.local import LocalBackend

try:
    from .backends.chain import ChainBackend
except ImportError:
    ChainBackend = None  # web3 not installed

# ── Provider implementations ───────────────────────────────────────────
from .providers import (
    SessionProviderImpl,
    ArtifactProviderImpl,
    TaskProviderImpl,
    ImpressionProviderImpl,
)

# ── Adapter registry ──────────────────────────────────────────────────
from .adapters.registry import AdapterRegistry

# ── Live thinking telemetry (server SSE / desktop live panel) ─────────
from .thinking import ThinkingEmitter, ThinkingEvent

# ── Social Protocol ───────────────────────────────────────────────────
from .social.gossip import GossipProtocol
from .social.profile import ProfileManager
from .social.graph import SocialGraph

# ── Infrastructure (used by ChainBackend) ──────────────────────────────
from .state import StateManager, ERC8004Identity, AgentStateRecord

# ── Generic LLM utilities ──────────────────────────────────────────────
# Reusable file-distillation pipeline (formerly server-only).
from .distiller import (
    distill,
    extract_text,
    DISTILL_INPUT_CHAR_BUDGET,
    DISTILL_OUTPUT_CHAR_BUDGET,
    DISTILL_SYSTEM_PROMPT,
)

try:
    from .chain import BSCClient
except ImportError:
    BSCClient = None

# ── A2A (Agent-to-Agent) ──────────────────────────────────────────────
# a2a_task_store + a2a depend on the optional a2a-sdk package (declared
# in pyproject as the ``a2a`` extra). Importing them unconditionally
# made the WHOLE SDK unimportable when a2a-sdk wasn't installed —
# chain.py couldn't even load. Treat the a2a layer as
# best-effort: callers that actually need it import directly from the
# adapter module and get a clean ImportError; everyone else still gets
# a working ``nexus_core`` package.
try:
    from .adapters.a2a_task_store import BNBChainTaskStore
    from .adapters.a2a import StatelessA2AAgent, A2ARuntime, A2AAgentConfig
    _A2A_AVAILABLE = True
except Exception as _a2a_err:  # noqa: BLE001 — optional integration
    BNBChainTaskStore = None
    StatelessA2AAgent = None
    A2ARuntime = None
    A2AAgentConfig = None
    _A2A_AVAILABLE = False
    import logging as _logging
    _logging.getLogger("nexus_core").info(
        "A2A integration unavailable (%s) — install with [a2a] extra to enable.",
        _a2a_err,
    )

# ── Framework-specific services ────────────────────────────────────────
# session/artifact lean on google-adk (optional ``adk`` extra). Same
# softening pattern as A2A: don't make the whole SDK uninportable just
# because the operator hasn't pulled in the ADK integration.
try:
    from .session import BNBChainSessionService
    from .artifact import BNBChainArtifactService
    _ADK_AVAILABLE = True
except Exception as _adk_err:  # noqa: BLE001 — optional integration
    BNBChainSessionService = None
    BNBChainArtifactService = None
    _ADK_AVAILABLE = False
    import logging as _logging
    _logging.getLogger("nexus_core").info(
        "Google-ADK integration unavailable (%s) — install with [adk] extra to enable.",
        _adk_err,
    )

from .memory import (
    EventLog, Event, CuratedMemory, EventLogCompactor,
    Episode, EpisodesStore,
    Fact, FactsStore,
    LearnedSkill, SkillsStore,
    PersonaVersion, PersonaStore,
    KnowledgeArticle, KnowledgeStore,
)
from .contracts import ContractEngine, ContractSpec, CheckResult, DriftScore, Rule

# ── Anchor batch (BEP-Nexus §3) ───────────────────────────────────────
from .anchor import (
    AnchorBatch,
    build_anchor_batch,
    canonicalize as canonicalize_manifest,
    SCHEMA_V1 as ANCHOR_SCHEMA_V1,
    ZERO_DIGEST_HEX,
)

# ── Recursive Language Model (long-context projection primitive) ──────
# See `nexus_core.rlm` and `docs/design/nexus-architecture.md`.
from .rlm import (
    RLMRunner,
    RLMConfig,
    RLMResult,
    TrajectoryEntry as RLMTrajectoryEntry,
    run_rlm,
)

# ── Falsifiable evolution (Phase O — BEP-Nexus §3.4) ──────────────────
# Proposal / verdict / revert primitives + the normative verdict
# decision rules. See `nexus_core.evolution` and
# `docs/design/nexus-architecture.md`.
from .evolution import (
    EvolutionProposal,
    EvolutionVerdict,
    EvolutionRevert,
    TaskKindPrediction,
    DriftThresholds,
    FixMatch,
    ObservedRegression,
    score_verdict,
    make_proposal_event,
    make_verdict_event,
    make_revert_event,
)

try:
    from .keystore import Keystore
except ImportError:
    Keystore = None

__all__ = [
    # Entry points + builder
    "local",
    "testnet",
    "mainnet",
    "builder",
    "Builder",
    # Core types
    "StorageBackend",
    "AgentRuntime",
    "SessionProvider",
    "ArtifactProvider",
    "TaskProvider",
    "ImpressionProvider",
    "Checkpoint",
    "Artifact",
    "FlushPolicy",
    "FlushBuffer",
    "WriteAheadLog",
    "MockBackend",
    "LocalBackend",
    "ChainBackend",
    "SessionProviderImpl",
    "ArtifactProviderImpl",
    "TaskProviderImpl",
    "ImpressionProviderImpl",
    "AdapterRegistry",
    # Social Protocol
    "Impression",
    "ImpressionDimensions",
    "ImpressionSummary",
    "NetworkStats",
    "GossipMessage",
    "GossipSession",
    "AgentProfile",
    "GossipProtocol",
    "ProfileManager",
    "SocialGraph",
    # Infrastructure
    "StateManager",
    "ERC8004Identity",
    "AgentStateRecord",
    "BSCClient",
    # A2A
    "BNBChainTaskStore",
    "StatelessA2AAgent",
    "A2ARuntime",
    "A2AAgentConfig",
    # Framework services
    "BNBChainSessionService",
    "BNBChainArtifactService",
    "Keystore",
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
    # Phase J memory namespaces
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
    # Anchor batch (BEP-Nexus §3)
    "AnchorBatch",
    "build_anchor_batch",
    "canonicalize_manifest",
    "ANCHOR_SCHEMA_V1",
    "ZERO_DIGEST_HEX",
    # Recursive Language Model
    "RLMRunner",
    "RLMConfig",
    "RLMResult",
    "RLMTrajectoryEntry",
    "run_rlm",
    # Falsifiable evolution (Phase O)
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
