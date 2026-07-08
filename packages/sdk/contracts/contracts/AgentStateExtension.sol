// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "./IIdentityRegistry.sol";

/**
 * @title AgentStateExtension
 * @notice Extends ERC-8004 agents with a stateless execution layer.
 *
 *         Stores two fields per agent:
 *           - state_root (bytes32): SHA-256 hash pointing to the full off-chain state payload
 *           - active_runtime (address): which runtime currently holds execution
 *
 *         Permission: Only the ERC-8004 NFT owner can update their agent's state.
 *         This contract does NOT modify ERC-8004 — it references it via IIdentityRegistry.
 *
 *         On-chain footprint: ~84 bytes per agent (two slots + timestamp).
 */
contract AgentStateExtension {

    // ── State ──────────────────────────────────────────────────────

    struct AgentState {
        bytes32 stateRoot;       // SHA-256 hash → full off-chain payload
        address activeRuntime;   // address of current executing runtime
        uint256 updatedAt;       // block.timestamp of last state commit
    }

    /// @notice ERC-8004 Identity Registry (deployed by BNBChain, read-only)
    IIdentityRegistry public immutable identityRegistry;

    /// @notice agentId (ERC-8004 tokenId) → agent state
    mapping(uint256 => AgentState) public agents;

    // ── Events ─────────────────────────────────────────────────────

    event StateRootUpdated(
        uint256 indexed agentId,
        bytes32 stateRoot,
        address activeRuntime,
        uint256 timestamp
    );

    event RuntimeChanged(
        uint256 indexed agentId,
        address indexed oldRuntime,
        address indexed newRuntime
    );

    // ── Errors ─────────────────────────────────────────────────────

    error AgentNotRegistered(uint256 agentId);
    error NotAgentOwner(uint256 agentId, address caller);

    // ── Constructor ────────────────────────────────────────────────

    /**
     * @param _identityRegistry Address of deployed ERC-8004 IdentityRegistry
     *        BSC Testnet: 0x8004A818BFB912233c491871b3d84c89A494BD9e
     *        BSC Mainnet: 0xfA09B3397fAC75424422C4D28b1729E3D4f659D7
     */
    constructor(address _identityRegistry) {
        identityRegistry = IIdentityRegistry(_identityRegistry);
    }

    // ── Modifiers ──────────────────────────────────────────────────

    modifier onlyAgentOwner(uint256 agentId) {
        // ownerOf() reverts if tokenId doesn't exist (ERC-721 standard),
        // so this also serves as an existence check.
        address owner = identityRegistry.ownerOf(agentId);
        if (owner == address(0))
            revert AgentNotRegistered(agentId);
        if (owner != msg.sender)
            revert NotAgentOwner(agentId, msg.sender);
        _;
    }

    // ── Write Methods ──────────────────────────────────────────────

    /**
     * @notice Update the state root for an agent.
     * @dev Only callable by the ERC-8004 NFT owner.
     *      This is the critical BSC ↔ off-chain storage link:
     *      the stateRoot hash points to the full off-chain state payload.
     *
     * @param agentId     ERC-8004 tokenId
     * @param stateRoot   SHA-256 hash of the off-chain state payload
     * @param runtime     Address of the runtime committing this state
     */
    function updateStateRoot(
        uint256 agentId,
        bytes32 stateRoot,
        address runtime
    ) external onlyAgentOwner(agentId) {
        AgentState storage state = agents[agentId];

        address oldRuntime = state.activeRuntime;
        state.stateRoot = stateRoot;
        state.activeRuntime = runtime;
        state.updatedAt = block.timestamp;

        emit StateRootUpdated(agentId, stateRoot, runtime, block.timestamp);

        if (oldRuntime != runtime) {
            emit RuntimeChanged(agentId, oldRuntime, runtime);
        }
    }

    /**
     * @notice Transfer execution to a new runtime without changing state.
     * @param agentId  ERC-8004 tokenId
     * @param runtime  Address of the new runtime
     */
    function setActiveRuntime(
        uint256 agentId,
        address runtime
    ) external onlyAgentOwner(agentId) {
        AgentState storage state = agents[agentId];
        address oldRuntime = state.activeRuntime;
        state.activeRuntime = runtime;
        state.updatedAt = block.timestamp;

        emit RuntimeChanged(agentId, oldRuntime, runtime);
    }

    // ── Read Methods ───────────────────────────────────────────────

    /**
     * @notice Resolve the current state root for an agent.
     * @return stateRoot SHA-256 hash pointing to the off-chain payload
     */
    function resolveStateRoot(uint256 agentId) external view returns (bytes32) {
        return agents[agentId].stateRoot;
    }

    /**
     * @notice Get full agent state.
     */
    function getAgentState(uint256 agentId)
        external view
        returns (bytes32 stateRoot, address activeRuntime, uint256 updatedAt)
    {
        AgentState storage state = agents[agentId];
        return (state.stateRoot, state.activeRuntime, state.updatedAt);
    }

    /**
     * @notice Check if an agent has any state committed.
     */
    function hasState(uint256 agentId) external view returns (bool) {
        return agents[agentId].stateRoot != bytes32(0);
    }
}
