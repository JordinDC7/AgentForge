# AgentForge

**Provider-agnostic multi-agent orchestration. Route any task to the cheapest capable AI.**

AgentForge is an autonomous development engine that coordinates multiple AI coding agents (Claude, Codex, Gemini, Aider, Ollama, OpenCode) to build software. It routes 80% of work to free or near-free models and only escalates to expensive ones when needed.

```bash
forge plan "Build a REST API with auth and a dashboard"
forge run --budget 10
# Walk away. Agents architect, implement, test, and review autonomously.
```

---

## Why AgentForge

Most AI coding tools lock you into one provider and one model. AgentForge treats AI providers like a team of specialists with different strengths and costs:

| Task | Provider | Cost | Why |
|------|----------|------|-----|
| Architecture & design | Claude Opus | ~$1.50 | Highest accuracy (80.8% SWE-bench) |
| Backend implementation | Codex CLI | $0 (sub) | Fastest (240 tok/s), included in $20/mo |
| Frontend & tests & docs | Gemini CLI | $0 | FREE tier, 1K req/day |
| Code review | Claude Sonnet | ~$0.50 | Strong reasoning, catches bugs |
| Git-heavy refactors | Aider | API costs | Git-native, auto-commits |
| Privacy-sensitive work | Ollama | $0 | Fully local, nothing leaves your machine |

**Typical feature cost: $2-4.** Architecture + review on Claude, everything else free.

### Key Differences from Agent Teams / Cursor / etc.

- **Provider-agnostic** — not locked to Claude, OpenAI, or any single provider
- **Cost-optimized** — cheapest capable model per task, not one-size-fits-all
- **Goal-driven discovery** — scans for TODOs, missing tests, lint errors, security issues, and generates tasks automatically
- **Persistent memory** — agents learn from past failures across sessions
- **Deterministic orchestration** — Python schedules agents, not AI coordinating itself
- **Escalation chain** — free model fails? Auto-retry with a smarter one

---

## Quick Start

### 1. Install AgentForge

```bash
git clone https://github.com/JordinDC7/AgentForge.git ~/.agent-forge
bash ~/.agent-forge/install.sh
```

This creates a global `forge` command. You never need to touch `~/.agent-forge` again.

### 2. Install at least one AI provider

Start free — no credit card needed:

```bash
npx @google/gemini-cli                          # FREE, 1K requests/day
```

Add more for better results (each makes the team stronger):

```bash
npm i -g @openai/codex                          # $20/mo — fastest implementation
npm i -g @anthropic-ai/claude-code              # $20/mo — best accuracy
pip install aider-chat                          # Free tool, API costs only
```

Verify what's available:

```bash
forge providers
```

### 3a. New project from scratch

```bash
forge new my-app --template python    # Also: node, react
cd my-app
```

This scaffolds:

```
my-app/
├── src/main.py                # Starter code
├── tests/test_main.py         # Test scaffold
├── CLAUDE.md                  # Instructions for Claude
├── AGENTS.md                  # Instructions for Codex
├── GEMINI.md                  # Instructions for Gemini
├── forge.yaml                 # Budget + routing config
└── .forge/
    ├── tasks/                 # Task definitions (JSON)
    ├── context/SHARED.md      # Shared architectural knowledge
    ├── mail/                  # Inter-agent messaging
    ├── locks/                 # Prevents agent collisions
    ├── memory/                # Persists across sessions
    ├── budget/                # Cost tracking + token ledger
    └── logs/                  # Agent output logs
```

### 3b. Add to an existing project

```bash
cd your-project
forge init
```

Creates `.forge/`, instruction files, and `forge.yaml`. Does **not** touch your existing code.

### 4. Plan and run

```bash
# Decompose a feature into tasks
forge plan "Add user authentication with OAuth2 and a settings page"

# Execute — agents work autonomously
forge run --budget 10

# Or run continuously: discover work → build → discover more → build more
forge run --budget 50 --continuous

# Or run until the project hits a quality target
forge run --budget 30 --until-score 80
```

---

## How It Works

```
YOU: forge run --budget 10 --continuous

  ORCHESTRATOR (deterministic Python, not AI):
  │
  ├──→ DISCOVERY ENGINE scans codebase
  │    Found: 3 TODOs, 2 missing tests, 1 lint error
  │    Creates 6 new tasks in .forge/tasks/
  │
  ├──→ COST ROUTER picks cheapest capable provider per task
  │    architecture → Claude Opus   (needs reasoning)    ~$1.50
  │    backend     → Codex CLI      (fast, $20 sub)      ~$0.00
  │    testing     → Gemini CLI     (free tier)           $0.00
  │    review      → Claude Sonnet                       ~$0.50
  │
  ├──→ SPAWNS agents as separate processes
  │    Each agent gets:
  │    - Its own git branch (forge/<type>/<task-id>)
  │    - Task lock file (prevents collisions)
  │    - Role instructions (from agents/*.md)
  │    - Shared context (.forge/context/SHARED.md)
  │    - Mail from other agents (.forge/mail/)
  │    - Memory of past failures (.forge/memory/)
  │
  ├──→ MONITORS: polls for completion, parses token usage from logs
  │    ✅ task done  → record cost, unlock, create review task
  │    ❌ task failed → retry up to 3x, then ESCALATE:
  │       Gemini($0) → Codex($sub) → Claude Sonnet → Claude Opus
  │
  ├──→ Every 3 completed tasks → run discovery again
  │    Found: 1 new TODO, 1 missing test → 2 more tasks
  │
  └──→ STOPS when: budget exhausted / health score met / no work / Ctrl+C
```

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `forge new <name> [--template T]` | Create a new project (`python`, `node`, `react`) |
| `forge init` | Add AgentForge to an existing project |
| `forge plan "<description>"` | Decompose a feature into tasks |
| `forge run [flags]` | Run the autonomous agent team |
| `forge status` | Show task board + progress |
| `forge health` | Project health score + ship-readiness |
| `forge discover` | Scan codebase for discoverable work |
| `forge providers` | List available AI providers |
| `forge cost` | Spending breakdown |

### Run Flags

```bash
forge run --budget 10                              # Max spend in USD
forge run --continuous                             # Keep discovering + building
forge run --until-score 80                         # Run until health >= 80
forge run --provider codex                         # Force ALL tasks to one provider
forge run --override backend=codex,frontend=gemini # Override routing per task type
forge run --goal "make this production ready"      # High-level goal for discovery
forge run --dry-run                                # Show what would happen
```

---

## Configuration

### forge.yaml

Every project gets a `forge.yaml` for budget, routing, and escalation:

```yaml
budget: 10.00

# Override routing per task type:
routing:
  architecture: claude-opus     # Best reasoning
  backend: codex-mini           # Cheapest paid ($0.25/$2 per MTok)
  frontend: gemini              # Free
  testing: gemini               # Free
  review: claude                # Claude Sonnet — catches bugs
  docs: gemini                  # Free

# Escalation chain (cheapest → most expensive):
escalation:
  - gemini        # FREE
  - codex-mini    # $0.25/$2 per MTok
  - haiku         # $1/$5 per MTok
  - sonnet        # $3/$15 per MTok
  - opus          # $5/$25 per MTok
```

### Provider Pricing

| Provider | Cost Tier | Input/Output per MTok | Accuracy | Speed |
|----------|-----------|----------------------|----------|-------|
| Gemini CLI | FREE | $0 / $0 | 0.64 | 80 tok/s |
| Codex Mini | $0.25/$2 | Cheapest paid | 0.62 | 240 tok/s |
| Codex | $1.25/$10 | Subscription | 0.70 | 240 tok/s |
| Claude Haiku | $1/$5 | Budget Claude | 0.60 | 120 tok/s |
| Claude Sonnet | $3/$15 | Balanced | 0.81 | 60 tok/s |
| Claude Opus | $5/$25 | Max intelligence | 0.81 | 40 tok/s |
| Aider | API costs | Varies by model | 0.74 | 60 tok/s |
| Ollama | FREE | Local, $0 | 0.35 | 30 tok/s |

---

## Reproducibility

AgentForge is designed so that any collaborator can reproduce your setup.

### What gets committed (safe to share)

```
forge.yaml          # Budget + routing config
CLAUDE.md           # Instructions for Claude
AGENTS.md           # Instructions for Codex
GEMINI.md           # Instructions for Gemini
.forge/tasks/*.json # Task definitions and status
.forge/context/     # Shared architectural knowledge
agents/             # Agent role instructions
```

### What stays local (gitignored)

```
.forge/locks/       # Active task locks
.forge/mail/        # Ephemeral inter-agent messages
.forge/logs/        # Agent output logs
.forge/memory/      # Session-specific learning
.forge/budget/      # Local spending data
.env                # API keys — NEVER commit this
```

### Cloning and running someone else's project

```bash
# Install AgentForge (once)
git clone https://github.com/JordinDC7/AgentForge.git ~/.agent-forge
bash ~/.agent-forge/install.sh

# Clone the project
git clone <project-repo>
cd <project>

# Install your providers
npx @google/gemini-cli        # Free
npm i -g @openai/codex        # Optional

# Run — forge reads forge.yaml for config
forge run --budget 10
```

The task board, routing, and escalation config travel with the repo. Each developer only needs AgentForge installed and at least one provider.

---

## Multi-Agent Patterns

### Fork-Join (default for most features)

```
          ┌→ Backend  (codex/$0) ──┐
Architect │                        ├→ Tester (gemini/$0) → Reviewer (claude)
 (opus)   └→ Frontend (gemini/$0) ─┘
```

### Escalation Chain

```
Gemini (free) ──FAIL──→ Codex ($sub) ──FAIL──→ Claude Sonnet ──FAIL──→ Claude Opus
```

80% of work completes at $0. Only genuinely hard problems hit expensive models.

### Self-Healing Loop

```
Agent writes code → runs tests → FAIL → reads error → fixes → tests → PASS
                                   └→ after 3 fails → escalate to better model
```

---

## Customizing Agent Roles

Agent role instructions are markdown files in `agents/`:

```
agents/
├── architecture.md    # System architect — designs, doesn't implement
├── backend.md         # Backend developer — follows architect specs
├── frontend.md        # Frontend developer — builds UI components
├── testing.md         # Test engineer — writes tests, reports failures
├── review.md          # Code reviewer — 9-point quality gate
└── docs.md            # Documentation writer — README, docstrings, API docs
```

Override per-project by creating:

```
your-project/.claude/agents/backend.md
```

Project-local agents take priority over global ones.

---

## Project Structure

```
agent-forge/
├── forge.py               # CLI entry point (init, plan, run, status, health, etc.)
├── core/
│   ├── orchestrator.py    # Main autonomous run loop + task dispatch
│   ├── cost_router.py     # Routes tasks to cheapest capable provider
│   └── discovery.py       # Scans codebase for TODOs, missing tests, lint, security
├── providers/
│   ├── base.py            # Abstract provider interface + capability model
│   └── registry.py        # Implementations: Gemini, Codex, Claude, Aider, OpenCode
├── agents/                # Role instructions for each agent type (markdown)
├── templates/
│   └── forge.yaml         # Default config template
├── dashboard.py           # Web dashboard (port 8420) — live task board + spending
├── docs/
│   ├── GETTING_STARTED.md # Detailed onboarding guide
│   └── PATTERNS.md        # Multi-agent collaboration patterns
├── install.sh             # One-line installer
└── pyproject.toml         # Python package config (pyyaml, rich, gitpython)
```

---

## Monitoring

### CLI

```bash
forge status     # Task board with provider assignments and costs
forge health     # Health score: tests, TODOs, lint, security
forge discover   # What agents would work on next
forge cost       # Spending breakdown by provider
```

### Web Dashboard

```bash
python dashboard.py
# Opens http://localhost:8420
```

Live view of task board, agent logs, shared context, spending by provider, and health metrics.

---

## FAQ

**How much does a typical feature cost?**
10$ per 1-3 hours depending on escalations. using this setup:

```budget: 195.00

routing:
  architecture: claude
  backend: codex-mini
  frontend: codex-mini
  testing: codex-mini
  review: claude
  docs: codex-mini

escalation:
  - codex-mini
  - claude-haiku
  - claude
  - claude-opus

agents:
  max_retries: 4
  timeout_minutes: 25
  discovery_interval: 4
  poll_interval_seconds: 30
  max_concurrent: 2
```

**Can I use just one provider?**
Yes. `forge run --provider gemini --budget 0` runs everything on the free tier.

**What if agents break something?**
Every agent works on its own git branch (`forge/<type>/<task-id>`). Nothing touches main until review passes. Worst case: `git checkout main`.

**Does it work offline?**
With Ollama and a local model, yes. `forge run --provider ollama` runs entirely on your machine.

**How is this different from Claude Agent Teams?**
Agent Teams is Claude-only, ephemeral, and task-driven. AgentForge is provider-agnostic, persistent, goal-driven with autonomous discovery, and routes 80% of work to free models.

**Can I add tasks manually?**
Drop a JSON file in `.forge/tasks/`:

```json
{
  "id": "custom-001",
  "type": "backend",
  "title": "Add rate limiting to API",
  "description": "Implement rate limiting middleware",
  "priority": 70,
  "depends_on": [],
  "estimated_minutes": 30
}
```

---

## Requirements

- Python 3.11+
- Git
- At least one AI provider installed (Gemini CLI recommended to start — it's free)

## License

MIT
