# Multi-Agent Patterns

## Pattern 1: Fork-Join (DEFAULT — use this for most features)
```
          ┌→ Backend  (codex/$0) ──┐
Architect │                        ├→ Tester (gemini/$0) → Reviewer (claude)
 (opus)   └→ Frontend (gemini/$0) ─┘
```
Cost: ~$2-4 | Speed: Fast (parallel impl) | Best for: Most features

## Pattern 2: Swarm (for large scope)
```
         ┌→ Agent 1 (module A) ──┐
Planner  ├→ Agent 2 (module B) ──├→ Reconciler → Reviewer
         ├→ Agent 3 (module C) ──┤
         └→ Agent 4 (module D) ──┘
```
Cost: ~$5-15 | Speed: Fastest | Best for: Migrations, multi-module refactors
Limit: ~8 parallel agents before diminishing returns

## Pattern 3: Escalation Chain
```
Gemini (free) ──FAIL──→ Codex ($sub) ──FAIL──→ Claude Sonnet ──FAIL──→ Claude Opus
```
80% of work at $0. Only hard problems touch expensive models.

## Pattern 4: Self-Healing Loop
```
Agent writes code → runs tests → FAIL → reads error → fixes → runs tests → PASS
                                   └→ after 3 fails → escalate to better model
```

## Anti-Patterns
- **All-Opus Everything**: Using the most expensive model for tests/docs. Use Gemini ($0).
- **God Orchestrator**: One AI coordinating everything. Use deterministic Python instead.
- **No Feedback Loop**: Agents that don't run tests. Every agent must test before commit.
- **Chatty Agents**: 50 messages to clarify specs. Write good specs upfront.

## Provider Strengths (March 2026)
| Task              | Best Provider  | Why                                  |
|-------------------|----------------|--------------------------------------|
| Architecture      | Claude Opus    | Highest accuracy (80.8% SWE-bench)   |
| Implementation    | Codex CLI      | Fastest (240 tok/s), included in $20  |
| Tests & Docs      | Gemini CLI     | FREE, 1K req/day, good enough        |
| Git-heavy refactor| Aider          | Git-native, auto-commits             |
| Web research      | Gemini CLI     | Built-in Google Search grounding     |
| Privacy-sensitive | Ollama         | Fully local, zero data leaves machine|
| Complex debugging | Claude Sonnet  | Strong reasoning + large context      |
