"""
Nexus — Adapter Registry (Registry Pattern).

Allows framework adapters to self-register and be discovered dynamically.
Third parties can register their own adapters without modifying core code.

Usage:
    # Discover available frameworks
    AdapterRegistry.available()  # ['adk', 'langgraph', 'crewai']

    # Register a custom adapter
    AdapterRegistry.register("autogen", MyAutoGenAdapter)
"""

from __future__ import annotations


class AdapterRegistry:
    """
    Registry for framework adapter classes.

    Adapters self-register when their module is imported.
    This enables dynamic discovery and loose coupling.
    """

    _adapters: dict[str, type] = {}

    @classmethod
    def register(cls, framework: str, adapter_class: type) -> None:
        """Register a framework adapter."""
        cls._adapters[framework] = adapter_class

    @classmethod
    def get(cls, framework: str) -> type:
        """Get adapter class by framework name."""
        if framework not in cls._adapters:
            available = ", ".join(cls._adapters.keys()) or "(none)"
            raise ValueError(
                f"Unknown framework: '{framework}'. Available: {available}"
            )
        return cls._adapters[framework]

    @classmethod
    def available(cls) -> list[str]:
        """List all registered framework names."""
        return list(cls._adapters.keys())

    @classmethod
    def clear(cls) -> None:
        """Clear all registrations (for testing)."""
        cls._adapters.clear()
