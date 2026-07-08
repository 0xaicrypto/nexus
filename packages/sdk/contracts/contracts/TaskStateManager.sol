// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "./IIdentityRegistry.sol";

/**
 * @title TaskStateManager
 * @notice On-chain task lifecycle state machine for AI agents.
 *
 *         Tracks per-task: state_hash (→ off-chain payload), version (optimistic concurrency),
 *         and status (pending/running/completed/failed).
 *
 *         Permission: Only the ERC-8004 NFT owner of the assigned agent can update tasks.
 *         Uses version-based optimistic concurrency to prevent multi-runtime conflicts.
 *
 *         On-chain footprint: ~161 bytes per task (5 slots).
 */
contract TaskStateManager {

    // ── Types ──────────────────────────────────────────────────────

    enum TaskStatus { Pending, Running, Completed, Failed }

    struct TaskRecord {
        uint256 agentId;     // FK to ERC-8004 tokenId
        bytes32 stateHash;   // SHA-256 hash → off-chain task payload
        uint256 version;     // Monotonic counter for optimistic concurrency
        TaskStatus status;   // Current lifecycle state
        uint256 updatedAt;   // block.timestamp of last update
    }

    // ── State ──────────────────────────────────────────────────────

    IIdentityRegistry public immutable identityRegistry;

    /// @notice taskId (bytes32) → task record
    mapping(bytes32 => TaskRecord) public tasks;

    /// @notice agentId → list of taskIds (for enumeration)
    mapping(uint256 => bytes32[]) public agentTasks;

    // ── Events ─────────────────────────────────────────────────────

    event TaskCreated(
        bytes32 indexed taskId,
        uint256 indexed agentId,
        uint256 timestamp
    );

    event TaskUpdated(
        bytes32 indexed taskId,
        uint256 indexed agentId,
        bytes32 stateHash,
        uint256 version,
        TaskStatus status,
        uint256 timestamp
    );

    // ── Errors ─────────────────────────────────────────────────────

    error AgentNotRegistered(uint256 agentId);
    error NotAgentOwner(uint256 agentId, address caller);
    error TaskAlreadyExists(bytes32 taskId);
    error TaskNotFound(bytes32 taskId);
    error VersionConflict(bytes32 taskId, uint256 expected, uint256 actual);

    // ── Constructor ────────────────────────────────────────────────

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
     * @notice Create a new task assigned to an agent.
     * @param taskId   Unique task identifier (e.g., keccak256 of A2A task ID)
     * @param agentId  ERC-8004 tokenId of the executing agent
     */
    function createTask(
        bytes32 taskId,
        uint256 agentId
    ) external onlyAgentOwner(agentId) {
        if (tasks[taskId].agentId != 0)
            revert TaskAlreadyExists(taskId);

        tasks[taskId] = TaskRecord({
            agentId: agentId,
            stateHash: bytes32(0),
            version: 0,
            status: TaskStatus.Pending,
            updatedAt: block.timestamp
        });

        agentTasks[agentId].push(taskId);

        emit TaskCreated(taskId, agentId, block.timestamp);
    }

    /**
     * @notice Update task state with optimistic concurrency control.
     * @dev Reverts if expectedVersion doesn't match current version.
     *      This prevents two runtimes from overwriting each other's state.
     *
     * @param taskId           Task to update
     * @param stateHash        New SHA-256 hash of the off-chain task payload
     * @param status           New task status
     * @param expectedVersion  Must match current version (optimistic concurrency)
     */
    function updateTask(
        bytes32 taskId,
        bytes32 stateHash,
        TaskStatus status,
        uint256 expectedVersion
    ) external {
        TaskRecord storage task = tasks[taskId];

        if (task.agentId == 0)
            revert TaskNotFound(taskId);

        // Permission: only agent owner can update
        if (identityRegistry.ownerOf(task.agentId) != msg.sender)
            revert NotAgentOwner(task.agentId, msg.sender);

        // Optimistic concurrency check
        if (task.version != expectedVersion)
            revert VersionConflict(taskId, expectedVersion, task.version);

        task.stateHash = stateHash;
        task.status = status;
        task.version += 1;
        task.updatedAt = block.timestamp;

        emit TaskUpdated(
            taskId, task.agentId, stateHash,
            task.version, status, block.timestamp
        );
    }

    // ── Read Methods ───────────────────────────────────────────────

    /**
     * @notice Get full task record.
     */
    function getTask(bytes32 taskId)
        external view
        returns (
            uint256 agentId,
            bytes32 stateHash,
            uint256 version,
            TaskStatus status,
            uint256 updatedAt
        )
    {
        TaskRecord storage task = tasks[taskId];
        return (task.agentId, task.stateHash, task.version, task.status, task.updatedAt);
    }

    /**
     * @notice Get all task IDs for an agent.
     */
    function getAgentTaskIds(uint256 agentId) external view returns (bytes32[] memory) {
        return agentTasks[agentId];
    }

    /**
     * @notice Get task count for an agent.
     */
    function getAgentTaskCount(uint256 agentId) external view returns (uint256) {
        return agentTasks[agentId].length;
    }

    /**
     * @notice Check if a task exists.
     */
    function taskExists(bytes32 taskId) external view returns (bool) {
        return tasks[taskId].agentId != 0;
    }
}
