"""
Nexus — Framework Adapters.

Each adapter is a translation layer between a specific agent
framework and Nexus's provider interfaces.

Available adapters:
  - adk:           Google ADK
  - langgraph:     LangGraph
  - crewai:        CrewAI
"""

from .registry import AdapterRegistry

__all__ = ["AdapterRegistry"]
