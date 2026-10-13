# Changelog

All notable changes to the AgentTrafficCop SDK are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-13

### Added

- `Guard` client class for talking to an AgentTrafficCop server
  - `check()` — route an agent-to-agent message
  - `health()` — check server health
  - `reset_session()` — delete a session
  - Async context manager support (`async with Guard(...) as g:`)
- Typed exceptions: `LoopDetected`, `StepLimitExceeded`, `SchemaRejected`,
  `AuthError`, `ConnectionError`, and the base `AgentTrafficCopError`
- `RouteResult` dataclass mirroring the server's approval response
- LangGraph integration via `agenttrafficcop.langgraph.guarded_node`
  - Sync and async node support
  - Static or callable extractors for `session_id`, `payload`, `metadata`
  - `raise_on_trip` flag to convert trips into no-ops
- Multi-loop safety: the client rebuilds its HTTP transport if the
  running event loop changes (needed for LangGraph and some test setups)

### Notes

- Requires Python 3.11+
- Requires `httpx` 0.27.x
- LangGraph is an optional extra: `pip install agenttrafficcop[langgraph]`

[0.1.0]: https://github.com/belalshafeq/Agent_Cop/releases/tag/v0.1.0