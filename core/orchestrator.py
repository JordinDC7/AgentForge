"""The Autonomous Orchestrator v2.

What's different from Agent Teams and everything else:

1. GOAL-DRIVEN, not task-driven. You describe the end state.
   Agents figure out what to build, then keep discovering more work.

2. CONTINUOUS MODE. Doesn't stop when tasks are done.
   Runs discovery → plan → execute → evaluate → discover → repeat.

3. PROVIDER-AGNOSTIC with overrides. Any model, any provider.
   Override per task type, per run, or globally in forge.yaml.

4. PERSISTENT MEMORY. Agents learn what worked and what didn't
   across sessions. .forge/memory/ survives restarts.

5. SELF-EVALUATING. After each task, an eval checks quality.
   Bad work gets sent back, not merged.

6. SHIP-READINESS SCORING. Tracks how close to "done" you are.
   You say "run until score > 80" and walk away.

Modes:
  forge run --budget 10                    → Execute planned tasks, stop when done
  forge run --budget 50 --continuous       → Keep developing until budget or goal met
  forge run --budget 20 --until-score 80   → Run until ship-readiness hits 80
  forge run --provider codex               → Force all tasks to one provider
  forge run --override backend=codex       → Override routing per task type
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
import yaml
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from core.cost_router import CostRouter, TaskComplexity
from core.discovery import DiscoveryEngine, DiscoveredWork
from providers.base import BaseProvider, TaskResult


class TaskStatus(Enum):
    BACKLOG = "backlog"
    BLOCKED = "blocked"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Task:
    id: str
    type: str
    title: str
    description: str
    status: TaskStatus = TaskStatus.BACKLOG
    depends_on: list[str] = field(default_factory=list)
    assigned_provider: str = ""
    branch: str = ""
    priority: int = 0
    complexity: Optional[TaskComplexity] = None
    estimated_minutes: float = 30.0
    actual_cost_usd: float = 0.0
    retries: int = 0
    max_retries: int = 3
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[TaskResult] = None
    source: str = "manual"  # manual, discovery, goal


@dataclass
class RunConfig:
    """All settings for a forge run, merged from CLI + forge.yaml."""
    budget: float = 10.0
    continuous: bool = False
    until_score: Optional[int] = None       # Stop when health score >= this
    provider_override: Optional[str] = None  # Force ONE provider for everything
    routing_overrides: dict = field(default_factory=dict)  # {"backend": "codex", "frontend": "gemini"}
    dry_run: bool = False
    poll_interval: int = 10
    discovery_interval: int = 3              # Run discovery every N completed tasks
    max_iterations: int = 500                # Safety cap for continuous mode
    max_concurrent: int = 2                  # Max agents running at same time (rate limit safety)
    max_cost_per_task: float = 5.0           # Max USD per individual task (safety cap)
    goal: str = ""                           # High-level goal description


class Orchestrator:
    """The autonomous development engine.
    
    Usage:
        config = RunConfig(budget=10, continuous=True)
        orch = Orchestrator(Path.cwd(), providers, config)
        orch.run()
    """

    def __init__(self, project_dir: Path, providers: list[BaseProvider], config: RunConfig):
        self.project_dir = Path(project_dir)
        self.forge_dir = self.project_dir / ".forge"
        self.config = config
        self.providers = providers
        self.router = CostRouter(providers, config.budget)
        self.discovery = DiscoveryEngine(project_dir, self.forge_dir)
        self.tasks: list[Task] = []
        self.budget_spent: float = 0.0
        self.run_log: list[str] = []
        self._active_processes: dict[str, subprocess.Popen] = {}
        self._active_log_handles: dict[str, object] = {}  # Track log file handles to flush/close
        self._tasks_completed_since_discovery = 0
        self._running = True

        # Load routing overrides from forge.yaml if not set via CLI
        self._load_yaml_config()

        # Handle Ctrl+C gracefully
        signal.signal(signal.SIGINT, self._handle_shutdown)

    def _load_yaml_config(self):
        """Merge forge.yaml settings with CLI config (CLI wins)."""
        yaml_path = self.project_dir / "forge.yaml"
        if not yaml_path.exists():
            return

        try:
            data = yaml.safe_load(yaml_path.read_text()) or {}
        except Exception:
            return

        # Budget: CLI overrides yaml
        if self.config.budget == 10.0 and "budget" in data:
            self.config.budget = float(data["budget"])
            self.router.budget_remaining = self.config.budget

        # Routing overrides: yaml as defaults, CLI overrides
        yaml_routing = data.get("routing", {})
        if yaml_routing and not self.config.routing_overrides:
            self.config.routing_overrides = yaml_routing

        # Escalation chain (informational — router handles this)
        # Discovery interval
        agents_cfg = data.get("agents", {})
        if "discovery_interval" in agents_cfg:
            self.config.discovery_interval = agents_cfg["discovery_interval"]
        if "max_concurrent" in agents_cfg:
            self.config.max_concurrent = agents_cfg["max_concurrent"]
        if "max_cost_per_task" in data:
            self.config.max_cost_per_task = float(data["max_cost_per_task"])

    def _handle_shutdown(self, signum, frame):
        """Graceful shutdown on Ctrl+C."""
        self._log("\n⚠ Shutdown requested. Finishing active tasks...")
        self._running = False

    # ════════════════════════════════════════════════════════════
    # INIT
    # ════════════════════════════════════════════════════════════

    def init_forge(self):
        """Initialize .forge/ directory structure."""
        for d in ["tasks", "mail", "locks", "logs", "context", "budget", "memory"]:
            (self.forge_dir / d).mkdir(parents=True, exist_ok=True)

        for name, default in [
            ("context/SHARED.md", "# Shared Context\n\n## Architecture\n\n## API Contracts\n\n## Known Issues\n"),
            ("TASKBOARD.md", "# Task Board\n\n## Backlog\n\n## In Progress\n\n## Done\n"),
        ]:
            f = self.forge_dir / name
            if not f.exists():
                f.write_text(default)

        budget_file = self.forge_dir / "budget" / "spending.json"
        if not budget_file.exists():
            budget_file.write_text(json.dumps({"budget_total": self.config.budget, "budget_spent": 0.0, "transactions": []}, indent=2))

        self._log(f"Initialized .forge/ in {self.project_dir}")

    # ════════════════════════════════════════════════════════════
    # THE MAIN LOOP
    # ════════════════════════════════════════════════════════════

    def run(self):
        """THE MAIN LOOP.
        
        Standard mode: execute tasks → stop
        Continuous mode: execute → discover → execute → discover → ...
        Until-score mode: keep going until health score target met
        """
        self._log("═" * 60)
        mode = "CONTINUOUS" if self.config.continuous else ("UNTIL SCORE ≥ " + str(self.config.until_score) if self.config.until_score else "STANDARD")
        self._log(f"⚡ FORGE RUN — Mode: {mode} | Budget: ${self.config.budget:.2f}")
        if self.config.provider_override:
            self._log(f"   Provider override: ALL tasks → {self.config.provider_override}")
        if self.config.routing_overrides:
            self._log(f"   Routing overrides: {self.config.routing_overrides}")
        if self.config.goal:
            self._log(f"   Goal: {self.config.goal}")
        self._log(f"   Tasks loaded: {len(self.tasks)}")
        self._log("═" * 60)

        # Print how tasks will be routed
        task_types = list(set(t.type for t in self.tasks)) or ["architecture", "backend", "frontend", "testing", "review"]
        self._print_routing_table(task_types)

        # If no tasks, or all tasks from a prior run are already done, run discovery
        all_finished = self.tasks and all(
            t.status in (TaskStatus.DONE, TaskStatus.FAILED) for t in self.tasks
        )
        if not self.tasks or all_finished:
            reason = "All prior tasks finished" if all_finished else "No tasks found"
            self._log(f"\n📡 {reason}. Running discovery...")
            self._run_discovery_cycle()

        # Clean up stale locks from previous runs
        self._cleanup_stale_locks()

        # Always create an architecture assessment task at the start
        # This ensures Claude Sonnet runs to review the codebase and update shared context
        self._inject_architecture_task()
        # Create review tasks for any implementation tasks already in the queue
        self._inject_review_tasks()

        iteration = 0
        while self._running and iteration < self.config.max_iterations:
            iteration += 1

            # ── Budget check ──
            remaining = self.config.budget - self.budget_spent
            if remaining <= 0:
                self._log(f"\n💰 BUDGET EXHAUSTED (${self.budget_spent:.2f}/{self.config.budget:.2f})")
                break

            # ── Score check (until-score mode) ──
            if self.config.until_score is not None:
                health = self.discovery.get_project_health()
                score = health["score"]
                self._log(f"\n📊 Health score: {score}/100 {health['readiness']} (target: {self.config.until_score})")
                if score >= self.config.until_score:
                    self._log(f"\n🎯 TARGET SCORE REACHED! {score} ≥ {self.config.until_score}")
                    break

            # ── Update task statuses ──
            self._update_task_statuses()

            # ── Check if all tasks done ──
            active = [t for t in self.tasks if t.status not in (TaskStatus.DONE, TaskStatus.FAILED)]
            if not active:
                self._log("\n📡 All current tasks done. Running discovery for more work...")
                new_count = self._run_discovery_cycle()
                if new_count == 0:
                    self._log("   No new work discovered. Project looks complete!")
                    if self.config.until_score is not None:
                        # Keep checking in until-score mode
                        time.sleep(self.config.poll_interval * 3)
                        continue
                    self._log("\n✅ ALL TASKS COMPLETE")
                    break
                # New tasks found — inject review tasks for any new implementation work
                self._inject_review_tasks()

            # ── Discovery cycle (every N completed tasks) ──
            if self._tasks_completed_since_discovery >= self.config.discovery_interval:
                self._run_discovery_cycle()
                self._tasks_completed_since_discovery = 0

            # ── Find and dispatch ready tasks ──
            ready = [t for t in self.tasks if t.status == TaskStatus.READY]
            if ready:
                ready.sort(key=lambda t: t.priority, reverse=True)
                # Limit concurrent agents to avoid rate limits
                in_flight = len(self._active_processes)
                slots = self.config.max_concurrent - in_flight

                for task in ready[:slots]:
                    if self.config.budget - self.budget_spent <= 0:
                        break
                    self._dispatch_task(task)
                    time.sleep(3)  # Stagger spawns to spread token usage
            else:
                in_progress = [t for t in self.tasks if t.status == TaskStatus.IN_PROGRESS]
                if in_progress:
                    self._monitor_active_agents()
                    self._sync_budget()
                    time.sleep(self.config.poll_interval)
                    continue
                else:
                    # Check for deadlocks
                    blocked = [t for t in self.tasks if t.status == TaskStatus.BLOCKED]
                    if blocked:
                        self._log(f"⚠ {len(blocked)} blocked tasks. Breaking deadlock.")
                        self._set_status(blocked[0], TaskStatus.READY)
                    continue

            self._monitor_active_agents()
            self._sync_budget()
            time.sleep(self.config.poll_interval)

        self._print_summary()

    # ════════════════════════════════════════════════════════════
    # DISCOVERY
    # ════════════════════════════════════════════════════════════

    def _run_discovery_cycle(self) -> int:
        """Scan the project and generate new tasks from discovered work."""
        discovered = self.discovery.discover_all(self.config.goal)
        new_count = 0

        for work in discovered[:10]:  # Cap per cycle
            task_id = f"disc-{int(time.time())}-{new_count}"
            task = Task(
                id=task_id,
                type=work.task_type,
                title=work.title,
                description=work.description,
                priority=work.priority,
                source="discovery",
            )
            self.tasks.append(task)
            self._save_task(task)
            new_count += 1

        if new_count > 0:
            self._log(f"   📡 Discovered {new_count} new tasks")
            for work in discovered[:new_count]:
                self._log(f"      [{work.source}] {work.title[:60]} (priority: {work.priority})")

        # Save health score to memory
        health = self.discovery.get_project_health()
        memory_file = self.forge_dir / "memory" / "health_history.json"
        history = []
        if memory_file.exists():
            try:
                history = json.loads(memory_file.read_text())
            except json.JSONDecodeError:
                pass
        history.append({"timestamp": datetime.now().isoformat(), **health})
        memory_file.write_text(json.dumps(history[-50:], indent=2))  # Keep last 50

        return new_count

    # ════════════════════════════════════════════════════════════
    # ROUTING (with overrides)
    # ════════════════════════════════════════════════════════════

    def _resolve_provider(self, task: Task) -> Optional[str]:
        """Determine which provider to use, respecting overrides.
        
        Priority:
        1. CLI --provider flag (overrides everything)
        2. CLI --override or forge.yaml routing overrides per task type
        3. Cost router (cheapest capable)
        """
        # Global override
        if self.config.provider_override:
            return self.config.provider_override

        # Per-type override
        if task.type in self.config.routing_overrides:
            return self.config.routing_overrides[task.type]

        # Auto-route
        return None

    def _print_routing_table(self, task_types: list[str]):
        """Show how each task type will be routed."""
        self._log(f"\n{'Task Type':<15} {'→ Provider':<15} {'Source':<12} {'~Cost':<10}")
        self._log("─" * 52)
        for tt in task_types:
            override = self._resolve_provider(Task(id="", type=tt, title="", description=""))
            if override:
                provider_name = override
                source = "override"
                # Find provider cost
                p = next((p for p in self.providers if p.name == override), None)
                cost_str = f"${p.estimate_cost(30 * 60):.2f}" if p else "?"
            else:
                try:
                    decision = self.router.route(tt)
                    provider_name = decision.provider.name
                    source = "auto"
                    cost_str = f"${decision.estimated_cost:.2f}"
                except Exception:
                    provider_name = "none"
                    source = "—"
                    cost_str = "—"
            self._log(f"  {tt:<15} {provider_name:<15} {source:<12} {cost_str:<10}")

    # ════════════════════════════════════════════════════════════
    # TASK MANAGEMENT
    # ════════════════════════════════════════════════════════════

    def add_task(self, task: Task):
        self.tasks.append(task)
        self._save_task(task)

    def _inject_architecture_task(self):
        """Run Claude Sonnet to assess the project — but skip if context is already fresh."""
        # Don't duplicate if one already exists this run
        if any(t.type == "architecture" and t.status != TaskStatus.DONE for t in self.tasks):
            return

        # Skip if SHARED.md was updated recently (within last 30 min) and has real content
        context_file = self.forge_dir / "context" / "SHARED.md"
        if context_file.exists():
            content = context_file.read_text()
            age_seconds = time.time() - context_file.stat().st_mtime
            # If context has substantial content and is less than 30 min old, skip
            if len(content) > 500 and age_seconds < 1800:
                self._log("  🏗️ Shared context is fresh — skipping architecture task")
                return

        vision_file = self.project_dir / "VISION.md"
        claude_file = self.project_dir / "CLAUDE.md"
        vision_exists = "Read VISION.md and" if vision_file.exists() else ""
        claude_exists = "Read CLAUDE.md and" if claude_file.exists() else ""

        arch_task = Task(
            id=f"arch-{int(time.time())}",
            type="architecture",
            title="Assess project and update shared context",
            description=(
                f"You are the lead architect. {vision_exists} {claude_exists} scan the entire codebase.\n\n"
                "Update .forge/context/SHARED.md with:\n"
                "## Architecture\n"
                "- List every module and what it does\n"
                "- Document the data flow between components\n\n"
                "## API Contracts\n"
                "- List all API endpoints, function signatures, data models\n\n"
                "## Known Issues\n"
                "- List any bugs, incomplete features, missing error handling\n\n"
                "## Implementation Plan\n"
                "- Based on VISION.md, list the next 5 features to build in priority order\n"
                "- For each, describe the files to create/modify, data models, and approach\n\n"
                "Do NOT write implementation code. Only analyze and document."
            ),
            status=TaskStatus.READY,
            priority=95,  # Highest priority — runs first
            source="system",
        )
        self.add_task(arch_task)
        self._log(f"  🏗️ Created architecture assessment task → routes to Claude Sonnet")

    def _inject_review_tasks(self):
        """Create review tasks for implementation work in the queue."""
        impl_tasks = [t for t in self.tasks 
                      if t.type in ("backend", "frontend") 
                      and t.status in (TaskStatus.READY, TaskStatus.BACKLOG)]
        existing_reviews = {t.title for t in self.tasks if t.type == "review"}
        
        for impl_task in impl_tasks[:3]:  # Cap at 3 reviews per cycle
            review_title = f"Review: {impl_task.title[:60]}"
            if review_title in existing_reviews:
                continue
            review_task = Task(
                id=f"review-{impl_task.id}",
                type="review",
                title=review_title,
                description=(
                    f"Review the code for: {impl_task.title}\n\n"
                    f"After the implementation agent finishes, review its changes.\n"
                    f"Check for: bugs, edge cases, missing error handling, test coverage, "
                    f"code style, and security issues. Fix any issues you find directly.\n"
                    f"Update .forge/context/SHARED.md if you discover architectural concerns."
                ),
                status=TaskStatus.BACKLOG,
                depends_on=[impl_task.id],
                priority=65,
                source="review",
            )
            self.add_task(review_task)

    def _cleanup_stale_locks(self):
        """Remove lock files from previous runs that no longer have active processes."""
        locks_dir = self.forge_dir / "locks"
        if not locks_dir.exists():
            return
        cleaned = 0
        for lock_file in locks_dir.glob("*.lock"):
            try:
                lock_data = json.loads(lock_file.read_text())
                started = lock_data.get("started", "")
                if started:
                    lock_age = (datetime.now() - datetime.fromisoformat(started)).total_seconds()
                    if lock_age > self.config.timeout_minutes * 60:
                        lock_file.unlink()
                        cleaned += 1
                else:
                    lock_file.unlink()
                    cleaned += 1
            except Exception:
                lock_file.unlink()
                cleaned += 1
        if cleaned:
            self._log(f"  🔓 Cleaned {cleaned} stale lock(s) from previous run")

    def _set_status(self, task: Task, status: TaskStatus):
        """Change task status and sync to disk so dashboard can see it."""
        task.status = status
        self._save_task(task)

    def _update_task_statuses(self):
        done_ids = {t.id for t in self.tasks if t.status == TaskStatus.DONE}
        for task in self.tasks:
            if task.status == TaskStatus.BACKLOG:
                if not task.depends_on or all(dep in done_ids for dep in task.depends_on):
                    self._set_status(task, TaskStatus.READY)
            elif task.status == TaskStatus.BLOCKED:
                # Unblock tasks whose lock has been cleared
                lock_file = self.forge_dir / "locks" / f"{task.id}.lock"
                if not lock_file.exists():
                    self._set_status(task, TaskStatus.READY)

    def _dispatch_task(self, task: Task):
        self._log(f"\n🚀 Dispatching: {task.id} ({task.type}) — {task.title[:50]}")

        # Check lock — prevent double-dispatch
        lock_file = self.forge_dir / "locks" / f"{task.id}.lock"
        if lock_file.exists():
            try:
                lock_data = json.loads(lock_file.read_text())
                started = lock_data.get("started", "")
                # Check if this is a stale lock (older than timeout)
                if started:
                    from datetime import datetime as dt
                    try:
                        lock_age = (datetime.now() - dt.fromisoformat(started)).total_seconds()
                        if lock_age > self.config.timeout_minutes * 60:
                            self._log(f"  🔓 Clearing stale lock ({int(lock_age/60)}min old)")
                            lock_file.unlink()
                        else:
                            self._log(f"  ⚠ Skipping — locked by {lock_data.get('agent', '?')} ({int(lock_age/60)}min ago)")
                            self._set_status(task, TaskStatus.BLOCKED)
                            return
                    except (ValueError, TypeError):
                        lock_file.unlink()  # Corrupt timestamp, clear it
                else:
                    self._log(f"  ⚠ Skipping — already locked by {lock_data.get('agent', '?')}")
                    self._set_status(task, TaskStatus.BLOCKED)
                    return
            except Exception:
                lock_file.unlink()  # Corrupt lock, clear it

        # Resolve provider (with overrides)
        preferred = self._resolve_provider(task)

        # Load failure history for adaptive routing
        memory = self.load_memory()
        failure_history = memory.get("failed_approaches", {})

        try:
            decision = self.router.route(
                task_type=task.type,
                complexity_override=task.complexity,
                estimated_duration_minutes=task.estimated_minutes,
                preferred_provider=preferred,
                failure_history=failure_history,
            )
        except RuntimeError as e:
            self._log(f"  ❌ No provider: {e}")
            self._set_status(task, TaskStatus.FAILED)
            return

        source_label = "override" if preferred else "auto-routed"
        self._log(f"  → {decision.provider.name} ({source_label}, ~${decision.estimated_cost:.2f})")

        if self.config.dry_run:
            self._log(f"  [DRY RUN] Would spawn {decision.provider.name}")
            self._set_status(task, TaskStatus.DONE)
            self._tasks_completed_since_discovery += 1
            return

        # Setup
        branch = f"forge/{task.type}/{task.id}"
        task.branch = branch
        task.assigned_provider = decision.provider.name
        self._set_status(task, TaskStatus.IN_PROGRESS)
        task.started_at = datetime.now().isoformat()

        # Lock
        lock_file = self.forge_dir / "locks" / f"{task.id}.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text(json.dumps({"agent": decision.provider.name, "started": task.started_at}))

        # Build prompt with role instructions
        role_instructions = self._load_role_instructions(task.type)
        prompt = self._build_task_prompt(task, role_instructions)

        # Write prompt to a file — avoids Windows command line length limits
        # and gives agents a file to read (more reliable than huge CLI args)
        prompts_dir = self.forge_dir / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = prompts_dir / f"{task.id}.md"
        prompt_file.write_text(prompt, encoding="utf-8")

        # Give the agent a short instruction that points to the prompt file
        short_prompt = (
            f"Read the task file at .forge/prompts/{task.id}.md and execute it. "
            f"The file contains your full instructions, context, and acceptance criteria. "
            f"When done, commit your changes and exit."
        )

        # Calculate per-task cost cap: min of configured cap and remaining budget
        remaining_budget = self.config.budget - self.budget_spent
        task_cost_cap = min(self.config.max_cost_per_task, remaining_budget)

        # Map task complexity to effort level — saves tokens on simple work
        EFFORT_BY_TYPE = {
            "docs": "low",
            "testing": "medium",
            "backend": "high",
            "frontend": "high",
            "architecture": "high",
            "review": "medium",
        }
        effort = EFFORT_BY_TYPE.get(task.type, "medium")

        cmd = decision.provider.build_command(
            prompt=short_prompt, workdir=self.project_dir,
            role_instructions="", max_budget_usd=task_cost_cap, effort=effort,
        )

        try:
            log_file = self.forge_dir / "logs" / f"{task.id}_{decision.provider.name}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)

            import threading

            # Run subprocess with PIPE and use a thread to write output to file
            # This captures ALL output regardless of how the child process writes it
            proc = subprocess.Popen(
                cmd, cwd=self.project_dir,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0,  # Unbuffered — every byte available immediately
                env={**os.environ, "FORGE_TASK_ID": task.id, "FORGE_AGENT": task.type},
                shell=(sys.platform == "win32"),
            )

            def _capture_output(process, filepath):
                """Read from process stdout line-by-line and write to file in real-time."""
                try:
                    with open(filepath, "wb") as f:
                        for line in iter(process.stdout.readline, b''):
                            f.write(line)
                            f.flush()
                        process.stdout.close()
                except Exception:
                    pass

            t = threading.Thread(target=_capture_output, args=(proc, log_file), daemon=True)
            t.start()

            self._active_processes[task.id] = proc
            self._log(f"  ✅ Spawned (PID: {proc.pid})")
        except Exception as e:
            self._log(f"  ❌ Spawn failed: {e}")
            self._set_status(task, TaskStatus.FAILED)

    def _monitor_active_agents(self):
        completed = []

        # Flush all active log handles so dashboard sees live output
        for task_id, handle in self._active_log_handles.items():
            try:
                handle.flush()
            except Exception:
                pass

        for task_id, proc in self._active_processes.items():
            ret = proc.poll()
            if ret is None:
                # Check timeout — kill runaway agents
                task = next((t for t in self.tasks if t.id == task_id), None)
                if task and task.started_at:
                    start = datetime.fromisoformat(task.started_at)
                    elapsed = (datetime.now() - start).total_seconds()
                    provider = next((p for p in self.providers if p.name == task.assigned_provider), None)
                    timeout_secs = (provider.config.timeout_minutes if provider else 30) * 60
                    if elapsed > timeout_secs:
                        self._log(f"  ⏰ {task_id} TIMED OUT after {elapsed/60:.0f}min (limit: {timeout_secs/60:.0f}min) — killing")
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        # Don't continue — let the next poll() pick up the exit code
                continue

            # Close the log file handle so all data is written to disk
            handle = self._active_log_handles.pop(task_id, None)
            if handle:
                try:
                    handle.flush()
                    handle.close()
                except Exception:
                    pass

            task = next((t for t in self.tasks if t.id == task_id), None)
            if not task:
                completed.append(task_id)
                continue

            duration = 0.0
            if task.started_at:
                start = datetime.fromisoformat(task.started_at)
                duration = (datetime.now() - start).total_seconds()

            # Parse real cost from agent log output
            cost = self._parse_cost_from_log(task)
            cost_source = "tokens" if cost > 0 else "unknown"
            task.actual_cost_usd = cost
            self.budget_spent += cost

            if ret == 0:
                self._set_status(task, TaskStatus.DONE)
                task.completed_at = datetime.now().isoformat()
                self._tasks_completed_since_discovery += 1
                self._log(f"  ✅ {task_id} DONE (${cost:.4f} [{cost_source}], {duration:.0f}s, {task.assigned_provider})")
                self._save_to_memory(task, success=True)

                # Auto-merge the agent's branch back to main
                if task.branch:
                    self._try_auto_merge(task)

                # Auto-create review task for completed work (except reviews themselves)
                if task.type not in ("review", "docs", "architecture") and task.source != "review":
                    review_id = f"review-{task.id}"
                    if not any(t.id == review_id for t in self.tasks):
                        review_task = Task(
                            id=review_id,
                            type="review",
                            title=f"Review: {task.title[:60]}",
                            description=(
                                f"Review the code changes from task {task.id}.\n"
                                f"Original task: {task.title}\n"
                                f"Provider: {task.assigned_provider}\n"
                                f"Branch: {task.branch}\n\n"
                                f"Check for: bugs, edge cases, missing error handling, test coverage, "
                                f"code style, and security issues. If issues found, fix them directly.\n"
                                f"Update .forge/context/SHARED.md with any architectural notes."
                            ),
                            status=TaskStatus.READY,
                            priority=65,
                            source="review",
                            branch=task.branch,
                        )
                        self.add_task(review_task)
                        self._log(f"  📝 Created review task → routes to Claude Sonnet")
            else:
                task.retries += 1
                if task.retries < task.max_retries:
                    self._log(f"  ⚠ {task_id} FAILED (retry {task.retries}/{task.max_retries}) — escalating")
                    self._set_status(task, TaskStatus.READY)
                    task.complexity = TaskComplexity.HARD  # Triggers escalation
                    self._save_to_memory(task, success=False)
                else:
                    self._log(f"  ❌ {task_id} FAILED permanently after {task.max_retries} retries")
                    self._set_status(task, TaskStatus.FAILED)
                    self._save_to_memory(task, success=False)

            (self.forge_dir / "locks" / f"{task_id}.lock").unlink(missing_ok=True)
            self._save_task(task)
            self._sync_budget()
            completed.append(task_id)

        for tid in completed:
            self._active_processes.pop(tid, None)

    def _try_auto_merge(self, task: Task):
        """Try to merge the agent's branch back to main. Skip on conflict."""
        branch = task.branch
        try:
            # Check if branch exists and has commits ahead of main
            result = subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                cwd=self.project_dir, capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return  # Branch doesn't exist (agent may not have created it)

            # Get current branch to restore later
            current = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.project_dir, capture_output=True, text=True, timeout=10,
            ).stdout.strip()

            # Attempt merge with --no-edit (no interactive editor)
            merge = subprocess.run(
                ["git", "merge", branch, "--no-ff", "-m", f"forge: merge {task.id} ({task.type})"],
                cwd=self.project_dir, capture_output=True, text=True, timeout=30,
            )

            if merge.returncode == 0:
                self._log(f"  🔀 Auto-merged {branch} → {current}")
            else:
                # Conflict — abort and leave branch for manual merge
                subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=self.project_dir, capture_output=True, timeout=10,
                )
                self._log(f"  ⚠ Merge conflict on {branch} — left for manual merge")
        except (subprocess.TimeoutExpired, Exception) as e:
            self._log(f"  ⚠ Auto-merge skipped: {e}")

    # ════════════════════════════════════════════════════════════
    # MEMORY (persists across sessions)
    # ════════════════════════════════════════════════════════════

    def _save_to_memory(self, task: Task, success: bool, cost_source: str = ""):
        """Save what happened so future runs can learn from it."""
        memory_file = self.forge_dir / "memory" / "task_history.json"
        memory_file.parent.mkdir(parents=True, exist_ok=True)
        history = []
        if memory_file.exists():
            try:
                history = json.loads(memory_file.read_text())
            except json.JSONDecodeError:
                pass

        history.append({
            "task_id": task.id,
            "type": task.type,
            "title": task.title,
            "provider": task.assigned_provider,
            "success": success,
            "retries": task.retries,
            "cost": task.actual_cost_usd,
            "timestamp": datetime.now().isoformat(),
        })

        memory_file.write_text(json.dumps(history[-200:], indent=2))  # Keep last 200

    def load_memory(self) -> dict:
        """Load memory from previous sessions for smarter routing."""
        memory = {"task_history": [], "health_history": [], "failed_approaches": []}
        for name in ["task_history", "health_history"]:
            f = self.forge_dir / "memory" / f"{name}.json"
            if f.exists():
                try:
                    memory[name] = json.loads(f.read_text())
                except json.JSONDecodeError:
                    pass

        # Extract patterns: which providers fail on which task types
        failures = {}
        for entry in memory["task_history"]:
            if not entry.get("success"):
                key = f"{entry.get('type', '')}:{entry.get('provider', '')}"
                failures[key] = failures.get(key, 0) + 1
        memory["failed_approaches"] = failures

        return memory

    # ════════════════════════════════════════════════════════════
    # PROMPTS & INSTRUCTIONS
    # ════════════════════════════════════════════════════════════

    def _build_task_prompt(self, task: Task, role_instructions: str) -> str:
        # ── Context: only include what this task type actually needs ──
        context_file = self.forge_dir / "context" / "SHARED.md"
        shared_raw = context_file.read_text() if context_file.exists() else ""

        # Architecture & review get full context; implementation tasks get a summary
        if task.type in ("architecture", "review"):
            shared = shared_raw[:8000]  # Cap at ~2k tokens
        else:
            # Extract only the sections relevant to this task type
            shared = self._extract_relevant_context(shared_raw, task.type)

        # ── Mail: only recent, relevant messages (not entire history) ──
        mail = ""
        mail_dir = self.forge_dir / "mail" / task.type
        if mail_dir.exists():
            recent_mail = sorted(mail_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
            for f in recent_mail[:3]:  # Only last 3 messages, not all
                content = f.read_text()
                mail += content[:1000] + "\n---\n"  # Cap each message

        # ── Memory hints: only if actually relevant ──
        memory_hint = ""
        if task.retries > 0:
            memory = self.load_memory()
            failed = memory.get("failed_approaches", {})
            relevant = {k: v for k, v in failed.items() if k.startswith(task.type + ":")}
            if relevant:
                memory_hint = "\n## Known Issues from Past Runs\n"
                for k, count in relevant.items():
                    provider = k.split(":")[1]
                    memory_hint += f"- {provider} failed {count} times on {task.type} tasks\n"

        # ── Diff context for review tasks ──
        diff_context = ""
        if task.type == "review" and task.branch:
            diff_context = self._get_branch_diff(task.branch)

        # ── Focused file list for implementation tasks ──
        file_hint = ""
        if task.type in ("backend", "frontend", "testing") and task.description:
            file_hint = self._suggest_relevant_files(task)

        return f"""# Task: {task.title}
## ID: {task.id} | Type: {task.type} | Branch: {task.branch}

## Description
{task.description}
{file_hint}
{diff_context}

## Shared Context
{shared}
{"## Messages" + chr(10) + mail if mail else ""}
{memory_hint}

## Role Instructions
{role_instructions}

## Acceptance Criteria
- All existing tests must still pass after your changes
- New functionality must have at least one test
- No lint errors introduced
- Exit 0 when done, exit 1 if stuck

## Rules
1. Work on branch: {task.branch}
2. Run tests before committing. Fix failures.
3. Write to .forge/mail/<agent-type>/<timestamp>.md to message other agents.
4. Update .forge/context/SHARED.md with decisions.
5. Exit 0 when done. Exit 1 if stuck.
"""

    def _extract_relevant_context(self, shared: str, task_type: str) -> str:
        """Extract only the sections of SHARED.md relevant to this task type."""
        if not shared:
            return ""

        # Always include Architecture overview
        sections_wanted = {"## Architecture", "## Known Issues"}

        if task_type in ("backend", "testing"):
            sections_wanted.update({"## API Contracts", "## Data Models"})
        elif task_type == "frontend":
            sections_wanted.update({"## Component", "## API Contracts"})
        elif task_type == "docs":
            sections_wanted.update({"## API Contracts", "## Implementation Plan"})

        # Parse sections and keep only relevant ones
        lines = shared.split("\n")
        result = []
        include = False
        for line in lines:
            if line.startswith("## "):
                include = any(line.startswith(s) for s in sections_wanted)
            if include:
                result.append(line)

        extracted = "\n".join(result)
        return extracted[:4000] if extracted else shared[:2000]  # Fallback: truncated full context

    def _get_branch_diff(self, branch: str) -> str:
        """Get the git diff for a branch to give reviewers focused context."""
        try:
            # Get diff of what this branch changed vs main
            result = subprocess.run(
                ["git", "diff", "main..." + branch, "--stat"],
                cwd=self.project_dir, capture_output=True, text=True, timeout=15,
            )
            stat = result.stdout.strip() if result.returncode == 0 else ""

            result2 = subprocess.run(
                ["git", "diff", "main..." + branch],
                cwd=self.project_dir, capture_output=True, text=True, timeout=15,
            )
            diff = result2.stdout.strip() if result2.returncode == 0 else ""

            if not diff:
                return ""

            # Cap diff to avoid huge token usage
            if len(diff) > 6000:
                diff = diff[:6000] + "\n... (diff truncated, review the full branch)"

            return f"\n## Changes to Review\n```\n{stat}\n```\n\n```diff\n{diff}\n```\n"
        except (subprocess.TimeoutExpired, Exception):
            return ""

    def _suggest_relevant_files(self, task: Task) -> str:
        """Suggest which files the agent should focus on based on task description."""
        # Look for file paths mentioned in the description
        mentioned = re.findall(r'[\w/]+\.(?:py|js|ts|jsx|tsx|json|yaml|md)', task.description)
        if mentioned:
            return "\n## Relevant Files\n" + "\n".join(f"- {f}" for f in mentioned[:10])

        # For testing tasks, find the source files to test
        if task.type == "testing":
            # Extract module name from title like "Write tests for module_name"
            match = re.search(r'tests?\s+for\s+(\w+)', task.title, re.IGNORECASE)
            if match:
                module = match.group(1)
                candidates = list(self.project_dir.rglob(f"*{module}*"))
                candidates = [f for f in candidates if ".forge" not in str(f)
                             and "__pycache__" not in str(f) and "node_modules" not in str(f)]
                if candidates:
                    return "\n## Relevant Files\n" + "\n".join(
                        f"- {f.relative_to(self.project_dir)}" for f in candidates[:5])

        return ""

    def _load_role_instructions(self, task_type: str) -> str:
        # Check project-local agents first, then forge global
        for agents_dir in [self.project_dir / ".claude" / "agents", Path(__file__).parent.parent / "agents"]:
            role_file = agents_dir / f"{task_type}.md"
            if role_file.exists():
                return role_file.read_text()
        return f"You are the {task_type} agent. Complete your assigned task."

    # ════════════════════════════════════════════════════════════
    # PERSISTENCE
    # ════════════════════════════════════════════════════════════

    def _save_task(self, task: Task):
        f = self.forge_dir / "tasks" / f"{task.id}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({
            "id": task.id, "type": task.type, "title": task.title,
            "description": task.description, "status": task.status.value,
            "depends_on": task.depends_on, "assigned_provider": task.assigned_provider,
            "branch": task.branch, "priority": task.priority,
            "estimated_minutes": task.estimated_minutes,
            "actual_cost_usd": task.actual_cost_usd,
            "retries": task.retries, "source": task.source,
        }, indent=2))

    def _parse_cost_from_log(self, task: Task) -> float:
        """Parse tokens from log, record in ledger, calculate cost."""
        log_dir = self.forge_dir / "logs"
        log_files = list(log_dir.glob(f"{task.id}_*.log"))
        if not log_files:
            return 0.0

        try:
            content = log_files[0].read_text(errors="replace")
        except Exception:
            return 0.0

        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        cost = 0.0

        # ── Claude stream-json format ──
        # Each line is a JSON object. The "result" line has total_cost_usd (actual
        # cost reported by the CLI). Assistant messages have usage.input_tokens etc.
        claude_cost_found = False
        for line in content.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            # Result line: {"type":"result", ..., "total_cost_usd": 0.05, ...}
            if obj.get("type") == "result":
                if "total_cost_usd" in obj:
                    cost = float(obj["total_cost_usd"])
                    claude_cost_found = True
                elif "cost_usd" in obj:
                    cost = float(obj["cost_usd"])
                    claude_cost_found = True

            # Assistant messages carry per-turn usage stats
            msg = obj.get("message", {})
            usage = msg.get("usage") or obj.get("usage") or {}
            if "input_tokens" in usage:
                input_tokens += int(usage["input_tokens"])
            if "output_tokens" in usage:
                output_tokens += int(usage["output_tokens"])

        # ── Codex format: "tokens used\n7,978" ──
        if not claude_cost_found and input_tokens == 0 and output_tokens == 0:
            token_matches = re.findall(r'tokens\s+used\s*\n\s*([\d,]+)', content)
            if token_matches:
                total_tokens = int(token_matches[-1].replace(",", ""))
                input_tokens = int(total_tokens * 0.75)
                output_tokens = total_tokens - input_tokens

            # Fallback: bare "input_tokens"/"output_tokens" keys (verbose text output)
            if total_tokens == 0:
                input_matches = re.findall(r'"input_tokens"\s*:\s*(\d+)', content)
                output_matches = re.findall(r'"output_tokens"\s*:\s*(\d+)', content)
                if input_matches:
                    input_tokens = sum(int(x) for x in input_matches)
                if output_matches:
                    output_tokens = sum(int(x) for x in output_matches)

        total_tokens = input_tokens + output_tokens if total_tokens == 0 else total_tokens
        if total_tokens == 0 and not claude_cost_found:
            return 0.0

        # If Claude gave us the real cost, use it directly; otherwise calculate
        if not claude_cost_found:
            RATES = {
                "codex-mini": {"input": 0.25, "output": 2.00},
                "codex": {"input": 1.25, "output": 10.00},
                "claude": {"input": 3.00, "output": 15.00},
                "claude-haiku": {"input": 1.00, "output": 5.00},
                "claude-opus": {"input": 15.00, "output": 75.00},
            }
            provider = task.assigned_provider
            rate = RATES.get(provider, {"input": 1.00, "output": 5.00})
            cost = (input_tokens / 1_000_000) * rate["input"] + (output_tokens / 1_000_000) * rate["output"]

        cost = round(cost, 6)

        # Write to token ledger
        self._record_tokens(task, task.assigned_provider, input_tokens, output_tokens, total_tokens, cost)

        return cost

    def _record_tokens(self, task, provider, input_tokens, output_tokens, total_tokens, cost):
        """Append to the token ledger — single source of truth for all spending.

        Uses a .lock file to prevent concurrent writes from corrupting the ledger.
        """
        ledger_file = self.forge_dir / "budget" / "token_ledger.json"
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        lock = ledger_file.with_suffix(".lock")

        # Simple spinlock — wait up to 5s for other writers
        for _ in range(50):
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                time.sleep(0.1)
        else:
            # Lock held too long — remove stale lock and proceed
            lock.unlink(missing_ok=True)

        try:
            ledger = []
            if ledger_file.exists():
                try:
                    ledger = json.loads(ledger_file.read_text())
                except Exception:
                    ledger = []

            ledger.append({
                "task_id": task.id,
                "task_title": task.title[:60],
                "provider": provider,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cost_usd": cost,
                "timestamp": datetime.now().isoformat(),
            })

            ledger_file.write_text(json.dumps(ledger[-500:], indent=2))
        finally:
            lock.unlink(missing_ok=True)

    def _sync_budget(self):
        """Build spending summary from token ledger."""
        budget_file = self.forge_dir / "budget" / "spending.json"
        budget_file.parent.mkdir(parents=True, exist_ok=True)
        ledger_file = self.forge_dir / "budget" / "token_ledger.json"

        ledger = []
        if ledger_file.exists():
            try:
                ledger = json.loads(ledger_file.read_text())
            except Exception:
                pass

        # Aggregate by provider
        by_provider = {}
        total_cost = 0.0
        total_input = 0
        total_output = 0
        total_all = 0

        for entry in ledger:
            prov = entry.get("provider", "unknown")
            if prov not in by_provider:
                by_provider[prov] = {"tasks": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            by_provider[prov]["tasks"] += 1
            by_provider[prov]["cost"] = round(by_provider[prov]["cost"] + entry.get("cost_usd", 0), 6)
            by_provider[prov]["input_tokens"] += entry.get("input_tokens", 0)
            by_provider[prov]["output_tokens"] += entry.get("output_tokens", 0)
            by_provider[prov]["total_tokens"] += entry.get("total_tokens", 0)
            total_cost += entry.get("cost_usd", 0)
            total_input += entry.get("input_tokens", 0)
            total_output += entry.get("output_tokens", 0)
            total_all += entry.get("total_tokens", 0)

        done_tasks = [t for t in self.tasks if t.status == TaskStatus.DONE]
        failed_tasks = [t for t in self.tasks if t.status == TaskStatus.FAILED]

        budget_file.write_text(json.dumps({
            "budget_total": self.config.budget,
            "total_spent": round(total_cost, 4),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_all,
            "tasks_done": len(done_tasks),
            "tasks_failed": len(failed_tasks),
            "tasks_active": len(self._active_processes),
            "by_provider": by_provider,
        }, indent=2))

        # Keep orchestrator in sync
        self.budget_spent = round(total_cost, 4)

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        self.run_log.append(line)

    # ════════════════════════════════════════════════════════════
    # SUMMARY
    # ════════════════════════════════════════════════════════════

    def _print_summary(self):
        done = [t for t in self.tasks if t.status == TaskStatus.DONE]
        failed = [t for t in self.tasks if t.status == TaskStatus.FAILED]
        rest = [t for t in self.tasks if t.status not in (TaskStatus.DONE, TaskStatus.FAILED)]

        health = self.discovery.get_project_health()

        print("\n" + "═" * 60)
        print("⚡ FORGE RUN SUMMARY")
        print("═" * 60)
        print(f"  Tasks:    {len(done)} done, {len(failed)} failed, {len(rest)} remaining")
        print(f"  Budget:   ${self.budget_spent:.2f} / ${self.config.budget:.2f}")
        print(f"  Health:   {health['score']}/100 {health['readiness']}")
        print()

        # Provider breakdown
        provider_costs: dict[str, float] = {}
        provider_counts: dict[str, int] = {}
        for t in done:
            provider_costs[t.assigned_provider] = provider_costs.get(t.assigned_provider, 0) + t.actual_cost_usd
            provider_counts[t.assigned_provider] = provider_counts.get(t.assigned_provider, 0) + 1
        if provider_costs:
            print("  Cost by provider:")
            for p, c in sorted(provider_costs.items(), key=lambda x: x[1], reverse=True):
                print(f"    {p:<15} {provider_counts[p]} tasks  ${c:.2f}")
            print()

        if done:
            print("  Completed:")
            for t in done[-10:]:  # Last 10
                print(f"    ✅ {t.title[:50]} ({t.assigned_provider}, ${t.actual_cost_usd:.2f})")
        if failed:
            print("  Failed:")
            for t in failed[-5:]:
                print(f"    ❌ {t.title[:50]} ({t.retries} retries)")

        # Persist summary
        (self.forge_dir / "budget" / "run_summary.json").write_text(json.dumps({
            "completed": len(done), "failed": len(failed), "remaining": len(rest),
            "total_cost": self.budget_spent, "budget": self.config.budget,
            "health_score": health["score"], "health_readiness": health["readiness"],
            "provider_costs": provider_costs,
        }, indent=2))

        # Persist budget spending
        (self.forge_dir / "budget" / "spending.json").write_text(json.dumps({
            "budget_total": self.config.budget,
            "budget_spent": self.budget_spent,
            "transactions": [{"task": t.id, "provider": t.assigned_provider, "cost": t.actual_cost_usd}
                             for t in done],
        }, indent=2))
