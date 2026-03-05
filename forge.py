#!/usr/bin/env python3
"""AgentForge CLI — Provider-agnostic multi-agent orchestration."""

import json
import subprocess
import sys
import time
from pathlib import Path

# Simple CLI without click dependency for portability
FORGE_VERSION = "0.2.0"
FORGE_DIR = ".forge"


def print_banner():
    print("""
  ⚡ AgentForge v{ver}
  Provider-agnostic multi-agent orchestration
  Route any task to the cheapest capable AI
""".format(ver=FORGE_VERSION))


def cmd_init(args):
    """Initialize .forge/ in current project."""
    from core.orchestrator import Orchestrator, RunConfig
    from providers.registry import detect_available_providers

    providers = detect_available_providers()
    config = RunConfig()
    orch = Orchestrator(Path.cwd(), providers, config)
    orch.init_forge()

    # Detect available providers
    print(f"\n🔍 Detected providers:")
    if providers:
        for p in providers:
            print(f"  ✅ {p.name:<15} tier={p.config.cost_tier.value:<6} accuracy={p.config.accuracy_score}")
    else:
        print("  ⚠ No providers found. Install at least one:")
        print("    • Gemini CLI: npx @google/gemini-cli (FREE)")
        print("    • Claude Code: npm install -g @anthropic-ai/claude-code")
        print("    • Codex CLI:   npm install -g @openai/codex")
        print("    • Aider:       pip install aider-chat")

    # Auto-create instruction files if none exist
    has_instructions = False
    for inst_file in ["CLAUDE.md", "AGENTS.md", "GEMINI.md"]:
        if (Path.cwd() / inst_file).exists():
            print(f"\n📄 Found {inst_file}")
            has_instructions = True
            break

    if not has_instructions:
        project_name = Path.cwd().name
        instructions = f"""# {project_name}

## AgentForge Coordination
- Task board: `.forge/tasks/`
- Inter-agent mail: `.forge/mail/<agent-type>/`
- Shared context: `.forge/context/SHARED.md`
- Lock tasks: `.forge/locks/<task-id>.lock`
- Each agent works on branch: `forge/<type>/<task-id>`
- Run tests before every commit
"""
        for filename in ["CLAUDE.md", "AGENTS.md", "GEMINI.md"]:
            (Path.cwd() / filename).write_text(instructions)
        print(f"\n📄 Created CLAUDE.md, AGENTS.md, GEMINI.md (edit these with your project details)")

    # Create forge.yaml if it doesn't exist
    yaml_path = Path.cwd() / "forge.yaml"
    if not yaml_path.exists():
        yaml_path.write_text("""# AgentForge Config
budget: 10.00

# Override routing per task type (uncomment to customize):
# routing:
#   architecture: claude-opus
#   backend: codex
#   frontend: gemini
#   testing: gemini
#   review: claude
#   docs: gemini

escalation:
  - gemini
  - codex
  - aider
  - claude
  - claude-opus
""")
        print(f"📄 Created forge.yaml (edit to set budget + routing)")

    print(f"\n✅ Forge initialized. Next steps:")
    print(f"   forge plan 'describe your feature'")
    print(f"   forge run --budget 10")
    print(f"   # or: forge run --continuous --budget 30")


def cmd_plan(args):
    """Decompose a feature into tasks."""
    if len(args) < 1:
        print("Usage: forge plan 'Add user authentication with OAuth2'")
        return

    description = " ".join(args)
    task_id_base = f"feat-{int(time.time())}"

    # Generate standard task decomposition
    tasks = [
        {
            "id": f"{task_id_base}-arch",
            "type": "architecture",
            "title": f"Architecture & Design: {description[:50]}",
            "description": f"Design the system architecture for: {description}\n\nWrite API contracts, data models, and component specs to .forge/context/SHARED.md",
            "priority": 100,
            "depends_on": [],
            "estimated_minutes": 15,
        },
        {
            "id": f"{task_id_base}-back",
            "type": "backend",
            "title": f"Backend Implementation",
            "description": f"Implement the backend for: {description}\n\nFollow the architecture spec in .forge/context/SHARED.md",
            "priority": 80,
            "depends_on": [f"{task_id_base}-arch"],
            "estimated_minutes": 45,
        },
        {
            "id": f"{task_id_base}-front",
            "type": "frontend",
            "title": f"Frontend Implementation",
            "description": f"Build the frontend for: {description}\n\nFollow the component spec in .forge/context/SHARED.md",
            "priority": 80,
            "depends_on": [f"{task_id_base}-arch"],
            "estimated_minutes": 45,
        },
        {
            "id": f"{task_id_base}-test",
            "type": "testing",
            "title": f"Test Suite",
            "description": f"Write comprehensive tests for: {description}\n\nUnit tests, integration tests, edge cases. Target >80% coverage.",
            "priority": 60,
            "depends_on": [f"{task_id_base}-back", f"{task_id_base}-front"],
            "estimated_minutes": 20,
        },
        {
            "id": f"{task_id_base}-review",
            "type": "review",
            "title": f"Code Review",
            "description": f"Review all code for: {description}\n\nCheck correctness, security, performance, style.",
            "priority": 40,
            "depends_on": [f"{task_id_base}-test"],
            "estimated_minutes": 15,
        },
    ]

    # Save tasks
    forge_dir = Path.cwd() / FORGE_DIR / "tasks"
    forge_dir.mkdir(parents=True, exist_ok=True)

    for task in tasks:
        task_file = forge_dir / f"{task['id']}.json"
        task_file.write_text(json.dumps(task, indent=2))

    # Show plan with cost estimates
    from providers.registry import detect_available_providers
    from core.cost_router import CostRouter

    providers = detect_available_providers()
    if providers:
        router = CostRouter(providers)
        print(f"\n📋 Plan: {description}\n")
        total_cost = 0.0
        for task in tasks:
            try:
                decision = router.route(task["type"], estimated_duration_minutes=task["estimated_minutes"])
                cost = decision.estimated_cost
                total_cost += cost
                deps = " → ".join(task["depends_on"]) if task["depends_on"] else "(none)"
                print(f"  {task['id']:<25} {task['type']:<14} → {decision.provider.name:<12} ~${cost:.2f}  deps: {deps}")
            except Exception:
                print(f"  {task['id']:<25} {task['type']:<14} → (no provider)")

        print(f"\n  💰 Estimated total: ${total_cost:.2f}")
    else:
        print(f"\n📋 Plan created with {len(tasks)} tasks")
        for task in tasks:
            print(f"  {task['id']}: {task['title']}")

    print(f"\n  Next: forge run --budget {max(5, int(total_cost * 2 + 1) if providers else 10)}")


def cmd_run(args):
    """Run the autonomous agent team."""
    import argparse
    parser = argparse.ArgumentParser(prog="forge run")
    parser.add_argument("--budget", type=float, default=10.0, help="Max spend in USD")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen")
    parser.add_argument("--provider", type=str, default=None,
                        help="Force ALL tasks to one provider (e.g. --provider codex)")
    parser.add_argument("--override", type=str, default=None,
                        help="Override routing per type (e.g. --override backend=codex,frontend=gemini)")
    parser.add_argument("--continuous", action="store_true",
                        help="Keep running: discover → build → discover → build ...")
    parser.add_argument("--until-score", type=int, default=None,
                        help="Keep running until project health score >= N (e.g. --until-score 80)")
    parser.add_argument("--goal", type=str, default=None,
                        help="High-level goal (e.g. --goal 'make this production ready')")
    parsed = parser.parse_args(args)

    from providers.registry import detect_available_providers
    from core.orchestrator import Orchestrator, Task, TaskStatus, RunConfig

    providers = detect_available_providers()
    if not providers:
        print("❌ No providers available. Install at least one AI coding agent.")
        return

    # Parse routing overrides: "backend=codex,frontend=gemini" → dict
    routing_overrides = {}
    if parsed.override:
        for pair in parsed.override.split(","):
            if "=" in pair:
                k, v = pair.strip().split("=", 1)
                routing_overrides[k.strip()] = v.strip()

    config = RunConfig(
        budget=parsed.budget,
        continuous=parsed.continuous or (parsed.until_score is not None),
        until_score=parsed.until_score,
        provider_override=parsed.provider,
        routing_overrides=routing_overrides,
        dry_run=parsed.dry_run,
        goal=parsed.goal or "",
    )

    orch = Orchestrator(Path.cwd(), providers, config)

    # Load existing tasks from .forge/tasks/ (including completed ones for dedup)
    tasks_dir = Path.cwd() / FORGE_DIR / "tasks"
    if tasks_dir.exists():
        for task_file in sorted(tasks_dir.glob("*.json")):
            data = json.loads(task_file.read_text())
            task = Task(
                id=data["id"], type=data["type"], title=data["title"],
                description=data["description"],
                priority=data.get("priority", 0),
                depends_on=data.get("depends_on", []),
                estimated_minutes=data.get("estimated_minutes", 30),
                source=data.get("source", "manual"),
            )
            # Restore persisted status
            persisted_status = data.get("status", "backlog")
            status_map = {
                "done": TaskStatus.DONE, "failed": TaskStatus.FAILED,
                "in_progress": TaskStatus.IN_PROGRESS, "in_review": TaskStatus.IN_REVIEW,
                "blocked": TaskStatus.BLOCKED, "ready": TaskStatus.READY,
                "backlog": TaskStatus.BACKLOG,
            }
            task.status = status_map.get(persisted_status, TaskStatus.BACKLOG)
            orch.add_task(task)

    mode_str = "CONTINUOUS" if config.continuous else ("UNTIL SCORE ≥ " + str(config.until_score) if config.until_score else "STANDARD")
    print(f"\n⚡ Starting forge run — {mode_str} — budget: ${config.budget:.2f}")
    if config.dry_run:
        print("   (DRY RUN)\n")

    orch.run()


def cmd_health(args):
    """Show project health score and ship-readiness."""
    from core.discovery import DiscoveryEngine

    forge_dir = Path.cwd() / FORGE_DIR
    if not forge_dir.exists():
        print("Not a forge project. Run 'forge init' first.")
        return

    engine = DiscoveryEngine(Path.cwd(), forge_dir)
    health = engine.get_project_health()

    print(f"\n⚡ Project Health\n{'━' * 40}")
    print(f"  Score:      {health['score']}/100  {health['readiness']}")
    print(f"  Tests:      {health.get('test_coverage_proxy', '?')}")
    print(f"  TODOs:      {health.get('todo_count', 0)}")
    print(f"  Lint issues:{health.get('lint_issues', 0)}")
    print(f"  Security:   {health.get('security_issues', 0)} issues")
    print(f"  README:     {'✅' if health.get('has_readme') else '❌'}")

    # Show trend if we have history
    history_file = forge_dir / "memory" / "health_history.json"
    if history_file.exists():
        try:
            history = json.loads(history_file.read_text())
            if len(history) >= 2:
                prev = history[-2]["score"]
                curr = health["score"]
                delta = curr - prev
                arrow = "📈" if delta > 0 else ("📉" if delta < 0 else "➡️")
                print(f"\n  Trend: {prev} → {curr} ({'+' if delta > 0 else ''}{delta}) {arrow}")
        except (json.JSONDecodeError, KeyError, IndexError):
            pass

    # Show what discovery would find
    discovered = engine.discover_all()
    if discovered:
        print(f"\n  📡 Discoverable work: {len(discovered)} items")
        for w in discovered[:5]:
            print(f"     [{w.source}] {w.title[:55]} (priority: {w.priority})")
        if len(discovered) > 5:
            print(f"     ... and {len(discovered) - 5} more")


def cmd_discover(args):
    """Run discovery and show what agents would work on next."""
    from core.discovery import DiscoveryEngine

    forge_dir = Path.cwd() / FORGE_DIR
    if not forge_dir.exists():
        print("Not a forge project. Run 'forge init' first.")
        return

    engine = DiscoveryEngine(Path.cwd(), forge_dir)
    discovered = engine.discover_all()

    if not discovered:
        print("✅ No new work discovered. Project looks clean!")
        return

    print(f"\n📡 Discovered {len(discovered)} items:\n")
    for i, w in enumerate(discovered, 1):
        icon = {"todo": "📝", "missing_test": "🧪", "lint": "⚠️", "failed_test": "❌",
                "missing_docs": "📄", "security": "🔒"}.get(w.source, "📋")
        print(f"  {icon} [{w.priority:>3}] {w.title[:60]}")
        if w.file_path:
            print(f"         {w.file_path}:{w.line_number}" if w.line_number else f"         {w.file_path}")

    print(f"\nRun 'forge run --continuous' to have agents work on these automatically.")


def cmd_status(args):
    """Show current task board and agent status."""
    forge_dir = Path.cwd() / FORGE_DIR
    if not forge_dir.exists():
        print("Not a forge project. Run 'forge init' first.")
        return

    print("⚡ AgentForge Status\n" + "━" * 50)

    # Show tasks
    tasks_dir = forge_dir / "tasks"
    if tasks_dir.exists():
        for task_file in sorted(tasks_dir.glob("*.json")):
            data = json.loads(task_file.read_text())
            status = data.get("status", "backlog")
            icon = {"done": "✅", "in_progress": "🔄", "failed": "❌", "blocked": "⏳"}.get(status, "📋")
            provider = data.get("assigned_provider", "—")
            cost = data.get("actual_cost_usd", 0)
            print(f"  {icon} {data['id']:<25} {data['type']:<12} {status:<12} {provider:<12} ${cost:.2f}")

    # Show active locks
    locks_dir = forge_dir / "locks"
    if locks_dir.exists():
        locks = list(locks_dir.glob("*.lock"))
        if locks:
            print(f"\n🔒 Active locks: {len(locks)}")
            for lock in locks:
                print(f"  {lock.stem}: {lock.read_text()[:50]}")

    # Show budget
    budget_file = forge_dir / "budget" / "spending.json"
    if budget_file.exists():
        budget = json.loads(budget_file.read_text())
        print(f"\n💰 Budget: ${budget.get('budget_spent', 0):.2f} / ${budget.get('budget_total', 0):.2f}")


def cmd_providers(args):
    """Show available providers and their status."""
    from providers.registry import detect_available_providers, PROVIDER_DEFAULTS

    print("⚡ Provider Status\n" + "━" * 70)
    print(f"{'Provider':<15} {'Status':<12} {'Cost Tier':<12} {'Accuracy':<10} {'Speed':<10}")
    print("─" * 70)

    available = {p.name for p in detect_available_providers()}

    for name, config in PROVIDER_DEFAULTS.items():
        status = "✅ Ready" if name in available else "❌ Missing"
        print(
            f"{name:<15} {status:<12} {config.cost_tier.value:<12} "
            f"{config.accuracy_score:<10.2f} {config.tokens_per_second:<10.0f} tok/s"
        )

    print(f"\nInstall missing providers:")
    print(f"  gemini:     npx @google/gemini-cli         (FREE — start here)")
    print(f"  codex:      npm i -g @openai/codex          ($20/mo sub or API key)")
    print(f"  codex-mini: ^ same install, uses cheaper model ($0.25/$2 per MTok)")
    print(f"  claude:     npm i -g @anthropic-ai/claude-code  ($20-200/mo or API)")
    print(f"  aider:      pip install aider-chat           (API costs only)")
    print(f"  opencode:   go install github.com/opencode-ai/opencode  (free)")
    print(f"  ollama:     ollama pull qwen2.5-coder:32b   (local, free)")


def cmd_cost(args):
    """Show cost tracking."""
    budget_file = Path.cwd() / FORGE_DIR / "budget" / "spending.json"
    if not budget_file.exists():
        print("No budget data. Run 'forge init' first.")
        return

    data = json.loads(budget_file.read_text())
    print(f"💰 Budget: ${data.get('budget_spent', 0):.2f} / ${data.get('budget_total', 0):.2f}")

    summary_file = Path.cwd() / FORGE_DIR / "budget" / "run_summary.json"
    if summary_file.exists():
        summary = json.loads(summary_file.read_text())
        print(f"   Tasks completed: {summary.get('completed', 0)}")
        print(f"   Tasks failed: {summary.get('failed', 0)}")


def cmd_new(args):
    """Create a brand new project with AgentForge wired in."""
    if len(args) < 1:
        print("Usage: forge new <project-name> [--template python|node|react]")
        return

    project_name = args[0]
    template = "python"  # default
    for i, a in enumerate(args):
        if a == "--template" and i + 1 < len(args):
            template = args[i + 1]

    project_dir = Path.cwd() / project_name

    if project_dir.exists():
        print(f"❌ Directory '{project_name}' already exists.")
        return

    print(f"\n⚡ Creating new project: {project_name} (template: {template})")
    project_dir.mkdir(parents=True)

    # --- Git init ---
    subprocess.run(["git", "init", "--quiet"], cwd=project_dir)

    # --- Template files based on project type ---
    templates = {
        "python": {
            "src/__init__.py": "",
            "src/main.py": '"""Main entry point."""\n\ndef main():\n    print("Hello from {name}!")\n\nif __name__ == "__main__":\n    main()\n',
            "tests/__init__.py": "",
            "tests/test_main.py": 'from src.main import main\n\ndef test_main(capsys):\n    main()\n    assert "{name}" in capsys.readouterr().out\n',
            "requirements.txt": "pytest>=7.4\nruff>=0.1\n",
            ".gitignore": "__pycache__/\n*.py[cod]\nvenv/\n.venv/\n.env\n.forge/locks/*\n.forge/mail/*\n.forge/logs/*\n.DS_Store\n.pytest_cache/\n",
        },
        "node": {
            "src/index.js": 'console.log("Hello from {name}!");\n',
            "package.json": '{{"name": "{name}", "version": "0.1.0", "main": "src/index.js", "scripts": {{"test": "jest", "start": "node src/index.js"}}}}\n',
            ".gitignore": "node_modules/\n.env\n.forge/locks/*\n.forge/mail/*\n.forge/logs/*\n.DS_Store\n",
        },
        "react": {
            "src/App.jsx": 'export default function App() {{\n  return <div><h1>{name}</h1></div>;\n}}\n',
            "src/index.jsx": 'import React from "react";\nimport ReactDOM from "react-dom/client";\nimport App from "./App";\nReactDOM.createRoot(document.getElementById("root")).render(<App />);\n',
            "package.json": '{{"name": "{name}", "version": "0.1.0", "scripts": {{"dev": "vite", "build": "vite build"}}}}\n',
            ".gitignore": "node_modules/\ndist/\n.env\n.forge/locks/*\n.forge/mail/*\n.forge/logs/*\n.DS_Store\n",
        },
    }

    tmpl = templates.get(template, templates["python"])
    for filepath, content in tmpl.items():
        full_path = project_dir / filepath
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content.format(name=project_name))

    # --- Universal instruction files (every provider reads one of these) ---
    instructions = f"""# {project_name}

## Project Type
{template} project scaffolded with AgentForge.

## Structure
{chr(10).join(f"- {fp}" for fp in tmpl.keys())}

## AgentForge Coordination
- Task board: `.forge/tasks/`
- Inter-agent mail: `.forge/mail/<agent-type>/`
- Shared context: `.forge/context/SHARED.md`
- Lock tasks: `.forge/locks/<task-id>.lock`
- Each agent works on branch: `forge/<type>/<task-id>`

## Testing
{"pytest tests/ -v" if template == "python" else "npm test"}

## Running
{"python src/main.py" if template == "python" else "npm start"}
"""
    for filename in ["CLAUDE.md", "AGENTS.md", "GEMINI.md"]:
        (project_dir / filename).write_text(instructions)

    # --- forge.yaml config ---
    (project_dir / "forge.yaml").write_text("""# AgentForge Config
budget: 10.00

# Uncomment to force specific providers per task type:
# routing:
#   architecture: claude-opus
#   backend: codex
#   frontend: gemini
#   testing: gemini
#   review: claude

escalation:
  - gemini       # Free
  - codex        # $20/mo subscription
  - aider        # API costs
  - claude       # Sonnet
  - claude-opus  # Nuclear option
""")

    # --- Initialize .forge/ ---
    import os
    original_dir = os.getcwd()
    os.chdir(project_dir)
    cmd_init([])
    os.chdir(original_dir)

    # --- Initial commit ---
    subprocess.run(["git", "add", "-A"], cwd=project_dir, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial scaffold via AgentForge", "--quiet"], cwd=project_dir, capture_output=True)

    print(f"\n{'━' * 50}")
    print(f"✅ Project created: {project_name}/")
    print(f"{'━' * 50}")
    print(f"\n  Next steps:\n")
    print(f"  cd {project_name}")
    print(f"  # Optional: lay foundation yourself with any AI tool")
    print(f"  # Then hand it off to the agent team:")
    print(f"  forge plan 'describe what you want built'")
    print(f"  forge run --budget 10")
    print(f"\n  Or run a single agent manually:")
    print(f"  claude -p 'Read CLAUDE.md and build the backend'")
    print(f"  gemini -p 'Read GEMINI.md and write tests'")


def cmd_help(args):
    print_banner()
    print("Commands:")
    print("  new <n> [--template T]     Create new project (python|node|react)")
    print("  init                       Add AgentForge to existing project")
    print("  plan <description>         Decompose a feature into tasks")
    print("  run [flags]                Run the autonomous agent team")
    print("  health                     Project health + ship-readiness score")
    print("  discover                   Scan codebase for discoverable work")
    print("  status                     Task board + progress")
    print("  providers                  List available AI providers")
    print("  cost                       Spending breakdown")
    print()
    print("Run flags:")
    print("  --budget N                 Max spend in USD (default: 10)")
    print("  --continuous               Keep running: discover > build > discover ...")
    print("  --until-score N            Run until health score >= N")
    print("  --provider NAME            Force ALL tasks to one provider")
    print("  --override TYPE=PROV,...   Override routing per task type")
    print("  --goal 'text'              Set high-level goal for discovery")
    print("  --dry-run                  Show what would happen")
    print()
    print("Examples:")
    print("  forge run --budget 10")
    print("  forge run --budget 50 --continuous")
    print("  forge run --budget 20 --until-score 80")
    print("  forge run --provider codex --budget 5")
    print("  forge run --override backend=codex,frontend=gemini")


def cli():
    """Main CLI entry point."""
    if len(sys.argv) < 2:
        cmd_help([])
        return

    command = sys.argv[1]
    args = sys.argv[2:]

    commands = {
        "new": cmd_new,
        "init": cmd_init,
        "plan": cmd_plan,
        "run": cmd_run,
        "status": cmd_status,
        "health": cmd_health,
        "discover": cmd_discover,
        "providers": cmd_providers,
        "cost": cmd_cost,
        "help": cmd_help,
        "-h": cmd_help,
        "--help": cmd_help,
    }

    if command in commands:
        commands[command](args)
    else:
        print(f"Unknown command: {command}")
        cmd_help([])


if __name__ == "__main__":
    cli()
