"""AgentTrafficCop — Multi-Agent Orchestration & Governance Middleware.

This package provides a runtime circuit breaker and schema-enforcement
shield that sits between communicating AI agents. It intercepts every
agent-to-agent handoff, validates the payload against a strict Pydantic
contract, fingerprints the payload with SHA-256, tracks per-session
history, and trips a circuit breaker before an infinite loop burns
your budget.
"""

__version__ = "1.0.0"
__author__ = "AgentTrafficCop"
__all__ = ["__version__", "__author__"]