# AgentForge Vision

## What AgentForge Is

A provider-agnostic autonomous development engine. It coordinates multiple AI coding agents (Claude, Codex, Gemini, Aider, Ollama) to build software — routing 80% of work to free or cheap models and only escalating to expensive ones when needed.

## Core Architecture

```
User -> forge run --budget N --continuous
  |
  Orchestrator (deterministic Python, NOT AI)
  |
  +-- Discovery Engine: scans codebase for TODOs, failing tests, lint, security, missing docs
  +-- Cost Router: picks cheapest capable provider per task type
  +-- DAG Scheduler: dependency-aware parallel dispatch
  +-- Task Lifecycle: BACKLOG -> READY -> IN_PROGRESS -> DONE/FAILED
  +-- Event Bus: webhooks, shell hooks, Python callbacks
  +-- Checkpoint/Resume: crash recovery, budget persistence
  |
  Providers (subprocess CLI agents):
    - Claude Code (claude) -- best accuracy, architecture + review
    - Codex CLI (codex/codex-mini) -- fastest, cheapest paid
    - Gemini CLI (gemini) -- free tier, 1K req/day
    - Aider (aider) -- git-native, auto-commits
    - Ollama (ollama) -- fully local, offline
    - Plugin system for custom providers
```

## Current State (v0.2)

### Working
- Multi-provider orchestration with cost routing
- DAG-based dependency graph with cycle detection
- Test-first (TDD) workflow: tests created before implementation
- Validation gate: verifies agents actually produced useful output
- Crash recovery via checkpoint/resume
- Event system with webhook support
- Plugin system for custom providers
- Structured output parsing (tokens, cost, files changed) per provider
- Context watcher: detects SHARED.md changes mid-run
- Dynamic accuracy scoring from observed success rates
- Conflict-aware parallel dispatch
- Inter-agent mail system
- File-based locking to prevent collisions
- Web dashboard (port 8420)

### Known Issues
- Discovery can be aggressive with test-related tasks
- Codex-mini sometimes exits without meaningful changes
- Windows encoding issues with agent output (mitigated with errors="replace")
- Health score can decrease when agents write failing tests

## Design Principles

1. **Deterministic orchestration** -- Python schedules agents, not AI coordinating AI
2. **Provider-agnostic** -- never locked to one vendor
3. **Cost-first routing** -- cheapest model that can handle the task
4. **Crash-tolerant** -- checkpoint everything, resume from where we left off
5. **Observable** -- heartbeat logging, event bus, web dashboard
6. **Autonomous discovery** -- find work to do, don't just wait for instructions

## Roadmap

### v0.3 - Quality & Reliability
- [ ] Smarter discovery: skip issues that were already attempted and not fixed
- [ ] Test impact analysis: only run tests relevant to changed files
- [ ] Branch health checks before auto-merge
- [ ] Per-provider rate limit tracking and backoff
- [ ] Agent output quality scoring (beyond pass/fail)

### v0.4 - Scale & Performance
- [ ] Parallel provider pools (e.g., 2 codex-mini + 1 claude simultaneously)
- [ ] Task prioritization based on dependency chain depth (critical path first)
- [ ] Incremental discovery (only scan changed files, not full codebase)
- [ ] Streaming agent output to dashboard

### v0.5 - Intelligence
- [ ] Cross-task learning: if approach X failed for task A, don't try it for similar task B
- [ ] Automatic prompt refinement based on success/failure patterns
- [ ] Multi-repo support: coordinate agents across multiple projects
- [ ] PR-based workflow: create PRs instead of direct branch merges

## File Structure

```
agent-forge/
  forge.py                 # CLI entry point
  core/
    orchestrator.py        # Main run loop, task dispatch, agent monitoring
    cost_router.py         # Routes tasks to cheapest capable provider
    discovery.py           # Scans codebase for work to do
    dag.py                 # Dependency graph with topological sort
    events.py              # Event bus (webhooks, shell hooks, callbacks)
    plugins.py             # Plugin loader for custom providers
  providers/
    base.py                # Abstract provider interface + capability model
    registry.py            # Built-in providers: Claude, Codex, Gemini, Aider, Ollama
  agents/                  # Role instructions (markdown) per agent type
  templates/               # Project scaffolding templates
  dashboard.py             # Web dashboard
  docs/                    # Documentation
```
