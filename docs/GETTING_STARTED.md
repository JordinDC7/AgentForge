# Getting Started with AgentForge

## Step 0: Install AgentForge (once, ever)

```bash
git clone https://github.com/YOUR_USERNAME/agent-forge.git ~/.agent-forge
bash ~/.agent-forge/install.sh
```

This creates the `forge` command globally. That's it — you never touch `~/.agent-forge` again.

### Install at least one AI provider

You need at least one. Start free:

```bash
# FREE — 1,000 requests/day, no credit card
npx @google/gemini-cli
```

Add more for better results (each one makes the team stronger):

```bash
npm i -g @anthropic-ai/claude-code     # $20/mo — best accuracy
npm i -g @openai/codex                  # $20/mo — fastest
pip install aider-chat                  # free tool, you pay API rates
```

Check what you've got:
```bash
forge providers
```

---

## Path A: Brand new project

```bash
forge new my-app --template python
cd my-app
```

That single command creates:
```
my-app/
├── src/main.py              # Starter code
├── tests/test_main.py       # Test scaffold
├── CLAUDE.md                # Instructions (Claude reads this)
├── AGENTS.md                # Instructions (Codex reads this)
├── GEMINI.md                # Instructions (Gemini reads this)
├── forge.yaml               # Your budget + routing config
├── .forge/
│   ├── tasks/               # Where task definitions live
│   ├── mail/                # How agents message each other
│   ├── locks/               # Prevents agents from colliding
│   ├── context/SHARED.md    # Shared architectural knowledge
│   ├── memory/              # Persists across sessions
│   └── budget/              # Cost tracking
├── requirements.txt
└── .gitignore
```

Templates available: `python`, `node`, `react`

**Optional — lay your own foundation first:**

Before agents touch anything, you can bootstrap with whatever tool you like:
```bash
# Use Claude
claude -p "Read CLAUDE.md. Set up a FastAPI app with SQLite and JWT auth"

# Use Codex
codex "Read AGENTS.md. Build a Next.js app with Tailwind and Prisma"

# Use Gemini (free)
gemini -p "Read GEMINI.md. Create the database models and API routes"

# Or just write code yourself
vim src/main.py
```

Agents pick up wherever you leave off. The instruction files tell every provider how your project works.

---

## Path B: Existing project

```bash
cd your-existing-project
forge init
```

This creates:
- `.forge/` directory (task board, mailbox, memory, budget tracking)
- `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` (if none exist)
- `forge.yaml` (budget and routing config)

It does NOT touch your existing code. Non-destructive.

---

## Step 2: Plan a feature

```bash
forge plan "Add user authentication with OAuth2, a dashboard, and Stripe billing"
```

This creates task files in `.forge/tasks/` with a standard decomposition:
```
ARCH-001  → Architecture & Design      (routes to Claude Opus)
BACK-001  → Backend Implementation     (routes to Codex, free tier)
FRNT-001  → Frontend Implementation    (routes to Gemini, free)
TEST-001  → Test Suite                 (routes to Gemini, free)
REVW-001  → Code Review               (routes to Claude Sonnet)
```

Preview what it'll cost:
```bash
forge status
```

---

## Step 3: Run the agents

### Basic run (execute planned tasks, stop when done)
```bash
forge run --budget 10
```

### Continuous run (keep finding and fixing things)
```bash
forge run --budget 50 --continuous
```
Agents complete tasks, then the discovery engine scans for TODOs, missing tests, lint errors, security issues — generates new tasks — agents work on those — discover more — and so on.

### Run until ship-ready
```bash
forge run --budget 30 --until-score 80
```
Keeps going until the project health score hits your target. Check the score anytime:
```bash
forge health
# Score: 62/100  🟡 Almost there
# Tests: 45%  |  TODOs: 12  |  Lint: 3  |  Security: 0
```

### With routing overrides
```bash
# Force all work to one provider
forge run --provider codex --budget 5

# Mix and match
forge run --override backend=codex,frontend=gemini,testing=gemini --budget 10

# Or set it permanently in forge.yaml:
```

```yaml
# forge.yaml
budget: 15.00
routing:
  architecture: claude-opus
  backend: codex
  frontend: gemini
  testing: gemini
  review: claude
  docs: gemini
```

---

## Step 4: Monitor (or walk away)

```bash
# Task progress
forge status

# Project quality
forge health

# What agents would work on next
forge discover

# Spending
forge cost

# Full git history of every change
git log --all --oneline
```

Ctrl+C gracefully stops the run. All progress is saved. Resume anytime with `forge run`.

---

## How it actually works under the hood

```
YOU: forge run --budget 10 --continuous

  ORCHESTRATOR (deterministic Python, not AI):
  │
  │  while budget > 0 and running:
  │
  ├─→ DISCOVERY ENGINE scans codebase
  │   Found: 3 TODOs, 2 missing tests, 1 lint error
  │   Creates 6 new tasks in .forge/tasks/
  │
  ├─→ COST ROUTER picks cheapest capable provider per task
  │   architecture → Claude Opus (needs reasoning)  $1.50
  │   backend     → Codex CLI (fast, $20 sub)       $0.00
  │   testing     → Gemini CLI (free tier)           $0.00
  │   review      → Claude Sonnet                    $0.50
  │
  ├─→ SPAWNS agents as separate processes
  │   Each agent gets:
  │   - Its own git branch
  │   - Task lock file (prevents collisions)
  │   - Role instructions (from agents/*.md)
  │   - Shared context (.forge/context/SHARED.md)
  │   - Mail from other agents (.forge/mail/)
  │   - Memory of past failures (.forge/memory/)
  │
  ├─→ MONITORS: polls for completion
  │   ✅ task done → merge, record cost, unlock
  │   ❌ task failed → retry up to 3x
  │   ❌ still failing → ESCALATE to better model
  │       Gemini($0) → Codex($sub) → Claude → Opus
  │
  ├─→ After every 3 completed tasks → run discovery again
  │   Found: 1 new TODO from agent's code, 1 missing test
  │   Creates 2 more tasks → loop continues
  │
  └─→ STOPS when: budget exhausted / health score met / no more work / Ctrl+C
```

---

## Customizing agent roles

Agent instructions live in `~/.agent-forge/agents/`. Override per-project by creating:

```
your-project/.claude/agents/backend.md
```

Project-local agents override global ones. Format is simple markdown:

```markdown
# Role: Backend Developer

You implement server-side code based on the architect's specs.

## Workflow
1. Read .forge/context/SHARED.md for API contracts
2. Check .forge/mail/backend/ for messages
3. Implement code following contracts
4. Run tests before committing
5. Mail .forge/mail/testing/ when done

## Rules
- Follow the architect's spec exactly
- Write tests for every public function
- Don't introduce new dependencies without justification
```

---

## Day-to-day workflow

```
Morning:
  forge health              # Check where things stand
  forge discover            # See what agents would work on

Start a feature:
  forge plan "add X"        # Break it down
  forge run --budget 10     # Launch agents

Walk away, come back:
  forge status              # What got done?
  forge cost                # What did it cost?
  git log --all             # See every change

Keep improving:
  forge run --continuous --budget 20 --until-score 85

Ship it:
  forge health              # 🟢 Score: 87/100 — Ready to ship
  git merge forge/...       # Merge agent branches
  git push                  # Deploy
```

---

## FAQ

**How much does it cost?**
Typical feature: $2-4. With Gemini free tier handling tests/docs/frontend, you only pay for architecture (Opus ~$1.50) and review (Sonnet ~$0.50). The rest is $0.

**Can I use just one provider?**
Yes. `forge run --provider gemini --budget 0` runs everything on the free tier. Less accurate but zero cost.

**What if agents break something?**
Every agent works on its own git branch. Nothing touches main until the reviewer approves. If everything fails, `git checkout main` and you're back to where you started.

**Does it work offline?**
If you have Ollama with a local model, yes. `forge run --provider ollama` runs entirely on your machine.

**Can I add tasks manually?**
Create a JSON file in `.forge/tasks/`:
```json
{
  "id": "custom-001",
  "type": "backend",
  "title": "Add rate limiting to API",
  "description": "Implement rate limiting...",
  "priority": 70,
  "depends_on": [],
  "estimated_minutes": 30
}
```

**How is this different from Agent Teams?**
Agent Teams is Claude-only, ephemeral, task-driven, and has no memory. AgentForge is provider-agnostic, persistent, goal-driven, self-discovering, and routes 80% of work to free models.
