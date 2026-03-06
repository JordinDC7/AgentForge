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
import tempfile
import time
import threading
import yaml
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


def _kill_process_tree(proc):
    """Kill a process and all its children. On Windows, shell=True creates
    an intermediate cmd.exe — proc.kill() only kills cmd.exe, orphaning the
    real agent. Use taskkill /T to kill the entire process tree."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        else:
            proc.kill()
    except Exception:
        pass


def _atomic_write(path: Path, content: str):
    """Write content to file atomically via temp file + rename.

    Prevents file corruption if the process crashes mid-write.
    On Windows, os.replace() is atomic within the same volume.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        # On Windows, os.replace() fails if target is open by another process
        # (antivirus, file indexer, etc.) — retry with exponential backoff
        for _attempt in range(10):
            try:
                os.replace(tmp, str(path))
                break
            except PermissionError:
                if sys.platform != "win32" or _attempt == 9:
                    raise
                time.sleep(0.1 * (2 ** min(_attempt, 4)))  # 0.1s → 1.6s
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

from core.cost_router import CostRouter, TaskComplexity
from core.dag import DependencyGraph, CycleError
from core.discovery import DiscoveryEngine, DiscoveredWork
from core.events import EventBus, EventType, Event
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
    max_iterations: int = 500                # Safety cap for standard mode (continuous overrides to unlimited)
    max_concurrent: int = 2                  # Max agents running at same time (rate limit safety)
    max_cost_per_task: float = 5.0           # Max USD per individual task (safety cap)
    goal: str = ""                           # High-level goal description
    timeout_minutes: int = 30               # Default timeout for agents
    test_first: bool = True                  # Generate tests before implementation


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
        self._active_log_handles: dict[str, object] = {}  # Reserved for future use (log handle tracking)
        self._tasks_completed_since_discovery = 0
        self._running = True
        self._style_guide: str = ""  # Cached project style guide
        self._consecutive_failures = 0  # Track failures to throttle when things go wrong

        # Lock for git operations (merge, checkout) to prevent concurrent corruption
        self._git_lock = threading.Lock()

        # DAG for dependency tracking
        self._dag = DependencyGraph()

        # Event bus for webhooks/notifications
        self.events = EventBus(self.forge_dir)

        # Context watcher — tracks SHARED.md hash to detect mid-run changes
        self._shared_md_hash: str = ""
        self._context_watcher_running = False
        self._context_watcher_stop = threading.Event()

        # Dynamic provider accuracy (updated from memory)
        self._dynamic_accuracy: dict[str, float] = {}

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
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8", errors="replace")) or {}
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

        # Event/webhook configuration
        self.events.configure(data)

    def _handle_shutdown(self, signum, frame):
        """Graceful shutdown on Ctrl+C — sets flag, main loop saves checkpoint on exit."""
        self._log("\n⚠ Shutdown requested. Will save checkpoint and exit after current task...")
        self._running = False
        # Don't save checkpoint here — let the main loop exit cleanly and save.
        # Calling _save_checkpoint from a signal handler risks corrupting files
        # if the main loop is mid-write.

    # ════════════════════════════════════════════════════════════
    # INIT
    # ════════════════════════════════════════════════════════════

    def init_forge(self):
        """Initialize .forge/ directory structure."""
        for d in ["tasks", "locks", "logs", "context", "budget", "memory",
                  "state", "prompts", "plugins",
                  "mail/architecture", "mail/backend", "mail/frontend",
                  "mail/testing", "mail/review", "mail/docs", "mail/broadcast"]:
            (self.forge_dir / d).mkdir(parents=True, exist_ok=True)

        for name, default in [
            ("context/SHARED.md", "# Shared Context\n\n## Architecture\n\n## API Contracts\n\n## Known Issues\n"),
            ("TASKBOARD.md", "# Task Board\n\n## Backlog\n\n## In Progress\n\n## Done\n"),
        ]:
            f = self.forge_dir / name
            if not f.exists():
                f.write_text(default, encoding="utf-8")

        budget_file = self.forge_dir / "budget" / "spending.json"
        if not budget_file.exists():
            budget_file.write_text(json.dumps({"budget_total": self.config.budget, "budget_spent": 0.0, "transactions": []}, indent=2), encoding="utf-8")

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
            if not any(p.name == self.config.provider_override for p in self.providers):
                self._log(f"   ⚠ WARNING: Provider '{self.config.provider_override}' not found! Available: {', '.join(p.name for p in self.providers)}")
        if self.config.routing_overrides:
            self._log(f"   Routing overrides: {self.config.routing_overrides}")
            provider_names = {p.name for p in self.providers}
            for task_type, pname in self.config.routing_overrides.items():
                if pname not in provider_names:
                    self._log(f"   ⚠ WARNING: Override '{task_type} → {pname}' — provider '{pname}' not found! Available: {', '.join(sorted(provider_names))}")
        if self.config.goal:
            self._log(f"   Goal: {self.config.goal}")
        self._log(f"   Tasks loaded: {len(self.tasks)}")
        self._log("═" * 60)

        # Emit run started event
        self.events.emit(Event(type=EventType.RUN_STARTED, data={
            "mode": mode, "budget": self.config.budget, "tasks": len(self.tasks),
        }))

        # Restore checkpoint from previous crashed run
        self._restore_checkpoint()

        # Update dynamic provider accuracy from memory
        self._update_dynamic_accuracy()

        # Build the dependency DAG from all tasks
        self._rebuild_dag()

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

        # Clean up stale locks and stuck tasks from previous runs
        self._cleanup_stale_locks()
        self._reset_stuck_tasks()

        # Extract project coding style once at startup (cached for all agents)
        self._style_guide = self._extract_style_guide()
        if self._style_guide:
            self._log(f"  📐 Extracted project style guide ({len(self._style_guide)} chars)")

        # Start context watcher thread
        self._start_context_watcher()

        # Always create an architecture assessment task at the start
        self._inject_architecture_task()
        # Create test-first tasks (tests before implementation) + review tasks
        if self.config.test_first:
            self._inject_test_first_tasks()
        self._inject_review_tasks()

        # In continuous/until-score mode, run effectively forever (budget is the real limit)
        max_iter = self.config.max_iterations
        if self.config.continuous or self.config.until_score is not None:
            max_iter = 999_999_999  # ~317 years at 10s/iter — budget will stop it first

        iteration = 0
        self._last_discovery_time = time.time()
        while self._running and iteration < max_iter:
            iteration += 1
            self._current_iteration = iteration

            # ── Budget check ──
            remaining = self.config.budget - self.budget_spent
            if remaining <= 0:
                self._log(f"\n💰 BUDGET EXHAUSTED (${self.budget_spent:.2f}/{self.config.budget:.2f})")
                self.events.emit(Event(type=EventType.BUDGET_EXHAUSTED, data={
                    "spent": self.budget_spent, "budget": self.config.budget,
                }))
                break
            # Budget warning at 80%
            if self.budget_spent >= self.config.budget * 0.8 and not getattr(self, '_budget_warned', False):
                self._log(f"  ⚠ Budget 80% used: ${self.budget_spent:.2f}/${self.config.budget:.2f}")
                self.events.emit(Event(type=EventType.BUDGET_WARNING, data={
                    "spent": self.budget_spent, "budget": self.config.budget,
                    "message": f"80% of budget used (${self.budget_spent:.2f}/${self.config.budget:.2f})",
                }))
                self._budget_warned = True

            # ── Score check (until-score mode) ──
            if self.config.until_score is not None:
                health = self.discovery.get_project_health()
                score = health.get("score", 0)
                readiness = health.get("readiness", "unknown")
                self._log(f"\n📊 Health score: {score}/100 {readiness} (target: {self.config.until_score})")
                if score >= self.config.until_score:
                    self._log(f"\n🎯 TARGET SCORE REACHED! {score} ≥ {self.config.until_score}")
                    break

            # ── Update task statuses (DAG-aware) ──
            self._update_task_statuses()

            # ── Checkpoint save every iteration ──
            self._save_checkpoint()

            # ── Check if all tasks done ──
            active = [t for t in self.tasks if t.status not in (TaskStatus.DONE, TaskStatus.FAILED)]
            if not active:
                self._log("\n📡 All current tasks done. Running discovery for more work...")
                new_count = self._run_discovery_cycle()
                if new_count == 0:
                    self._log("   No new work discovered.")
                    if self.config.continuous or self.config.until_score is not None:
                        # Continuous/until-score: keep looping, re-discover periodically
                        self._log("   Continuous mode — sleeping then re-discovering...")
                        # Interruptible sleep so Ctrl+C is responsive
                        for _ in range(self.config.poll_interval * 6):
                            if not self._running:
                                break
                            time.sleep(1)
                        continue
                    self._log("\n✅ ALL TASKS COMPLETE")
                    break
                # New tasks found — inject review tasks for any new implementation work
                if self.config.test_first:
                    self._inject_test_first_tasks()
                self._inject_review_tasks()
                self._rebuild_dag()
                self._update_task_statuses()  # Promote new tasks so they're dispatchable this iteration

            # ── Discovery cycle (every N completed tasks OR every 5 min in continuous) ──
            time_since_discovery = time.time() - self._last_discovery_time
            discovery_due_by_count = self._tasks_completed_since_discovery >= self.config.discovery_interval
            discovery_due_by_time = (self.config.continuous and time_since_discovery > 300)  # 5 min

            if discovery_due_by_count or discovery_due_by_time:
                if discovery_due_by_time:
                    self._log(f"  📡 Periodic re-discovery ({int(time_since_discovery)}s since last scan)")
                self._run_discovery_cycle()
                self._tasks_completed_since_discovery = 0
                self._last_discovery_time = time.time()
                # Re-run architecture to update SHARED.md with what was built
                self._inject_architecture_task()
                # Inject docs task if we've done enough implementation work
                self._inject_docs_task()
                # Rebuild DAG with new tasks
                self._rebuild_dag()
                self._update_task_statuses()  # Promote new tasks immediately

            # ── Monitor active agents BEFORE dispatch to update budget/status ──
            # This ensures budget_spent is current before we decide to dispatch more
            self._monitor_active_agents()
            self._sync_budget()

            # ── Find and dispatch ready tasks (parallel, DAG-aware) ──
            # Use DAG to find tasks whose dependencies are all met
            done_ids = {t.id for t in self.tasks if t.status == TaskStatus.DONE}
            in_progress_ids = {t.id for t in self.tasks if t.status == TaskStatus.IN_PROGRESS}
            dag_ready_ids = set(self._dag.get_ready(done_ids, in_progress_ids))
            ready = [t for t in self.tasks if t.status == TaskStatus.READY and t.id in dag_ready_ids]
            # Fallback: also include READY tasks not in DAG (manually added)
            ready += [t for t in self.tasks if t.status == TaskStatus.READY
                      and t.id not in dag_ready_ids and not t.depends_on]
            # Deduplicate
            seen = set()
            unique_ready = []
            for t in ready:
                if t.id not in seen:
                    seen.add(t.id)
                    unique_ready.append(t)
            ready = unique_ready
            if ready:
                ready.sort(key=lambda t: t.priority, reverse=True)
                # Limit concurrent agents to avoid rate limits
                in_flight = len(self._active_processes)
                slots = max(0, max(1, self.config.max_concurrent) - in_flight)
                if slots <= 0:
                    pass  # All slots occupied — skip dispatch, monitor will free them

                # Health-aware throttling: reduce concurrency when agents keep failing
                if self._consecutive_failures >= 3:
                    slots = min(slots, 1)  # Serial mode — one at a time until a success
                    if self._consecutive_failures >= 3 and not getattr(self, '_failure_warned', False):
                        self._failure_warned = True
                        self._log(f"  🔴 {self._consecutive_failures} consecutive failures — throttling to 1 agent at a time")

                # Filter to tasks that don't conflict with each other (file overlap check)
                dispatchable = self._select_non_conflicting(ready[:slots * 2], slots)

                # If all tasks conflict with in-progress work, force dispatch highest-priority
                if not dispatchable and ready and slots > 0:
                    self._log(f"  ⚠ All {len(ready)} ready tasks conflict with in-progress work — forcing highest-priority")
                    dispatchable = [ready[0]]

                for task in dispatchable:
                    if self.config.budget - self.budget_spent <= 0:
                        break
                    self._dispatch_task(task)
                    time.sleep(1)  # Brief stagger to avoid API burst
            else:
                in_progress = [t for t in self.tasks if t.status == TaskStatus.IN_PROGRESS]
                # Throttle "no dispatchable" logging to once per 60s
                now = time.time()
                last_nodispatch = getattr(self, '_last_nodispatch_log', 0)
                if now - last_nodispatch >= 60:
                    self._last_nodispatch_log = now
                    backlog = [t for t in self.tasks if t.status == TaskStatus.BACKLOG]
                    ready_but_not_dag = [t for t in self.tasks if t.status == TaskStatus.READY and t.id not in dag_ready_ids]
                    if backlog or ready_but_not_dag:
                        status_counts = {}
                        for t in self.tasks:
                            status_counts[t.status.value] = status_counts.get(t.status.value, 0) + 1
                        self._log(f"  📊 No dispatchable tasks. Status: {status_counts}")
                        if ready_but_not_dag:
                            self._log(f"     {len(ready_but_not_dag)} READY but not DAG-ready (deps not met in DAG)")
                if in_progress:
                    time.sleep(self.config.poll_interval)
                    continue
                else:
                    # Check for deadlocks — only clear orphan locks (process dead but lock exists)
                    blocked = [t for t in self.tasks if t.status == TaskStatus.BLOCKED]
                    if blocked:
                        resolved = False
                        for bt in blocked:
                            lock_file = self.forge_dir / "locks" / f"{bt.id}.lock"
                            if lock_file.exists() and bt.id not in self._active_processes:
                                # Lock exists but no subprocess — the process died
                                lock_file.unlink(missing_ok=True)
                                self._set_status(bt, TaskStatus.READY)
                                self._log(f"  🔓 Cleared orphan lock for {bt.id}")
                                resolved = True
                                break
                            elif not lock_file.exists():
                                self._set_status(bt, TaskStatus.READY)
                                resolved = True
                                break
                        if not resolved:
                            self._log(f"  ⚠ {len(blocked)} blocked tasks — locks held by active processes")
                    # MUST sleep to avoid CPU spin when no tasks are actionable
                    # (e.g., all tasks BACKLOG with unmet deps, or waiting for discovery)
                    time.sleep(self.config.poll_interval)
                    continue

            # ── Heartbeat: periodic status line so user knows we're alive ──
            now = time.time()
            last_hb = getattr(self, '_last_heartbeat', 0)
            if now - last_hb >= 60:
                self._last_heartbeat = now
                self._print_heartbeat()

            time.sleep(self.config.poll_interval)

        # Stop context watcher (signal immediately instead of waiting up to 15s)
        self._context_watcher_running = False
        self._context_watcher_stop.set()

        # Terminate any still-running agent processes to prevent zombies
        for task_id, proc in list(self._active_processes.items()):
            try:
                if proc.poll() is None:
                    self._log(f"  🛑 Terminating running agent: {task_id}")
                    _kill_process_tree(proc)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass  # taskkill /T already sent
            except Exception:
                pass
        self._active_processes.clear()

        # Clean up lock files for terminated processes
        locks_dir = self.forge_dir / "locks"
        if locks_dir.exists():
            for lock_file in locks_dir.glob("*.lock"):
                lock_file.unlink(missing_ok=True)

        # Final checkpoint
        self._save_checkpoint()

        # Emit run completed event
        self.events.emit(Event(type=EventType.RUN_COMPLETED, data={
            "tasks_done": len([t for t in self.tasks if t.status == TaskStatus.DONE]),
            "tasks_failed": len([t for t in self.tasks if t.status == TaskStatus.FAILED]),
            "budget_spent": self.budget_spent, "budget": self.config.budget,
        }))

        self._print_summary()

    # ════════════════════════════════════════════════════════════
    # DISCOVERY
    # ════════════════════════════════════════════════════════════

    def _run_discovery_cycle(self) -> int:
        """Scan the project and generate new tasks from discovered work.

        Uses both filesystem-based discovery AND LLM-assisted planning.
        """
        # Prune completed/failed tasks to prevent unbounded list growth in continuous mode
        if len(self.tasks) > 200:
            active = [t for t in self.tasks if t.status not in (TaskStatus.DONE, TaskStatus.FAILED)]
            finished = [t for t in self.tasks if t.status in (TaskStatus.DONE, TaskStatus.FAILED)]
            # Keep the 100 most recent finished tasks (they're already persisted to disk)
            self.tasks = active + finished[-100:]

        # Don't flood the queue — cap total pending work before discovering more
        pending = [t for t in self.tasks if t.status in (TaskStatus.READY, TaskStatus.BACKLOG, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)]
        if len(pending) >= 15:
            self._log(f"  📡 Skipping discovery — {len(pending)} tasks already pending (cap: 15)")
            return 0

        discovered = self.discovery.discover_all(self.config.goal)
        new_count = 0

        # Build set of existing task titles for deduplication
        existing_titles = set()
        existing_prefixes = set()
        for t in self.tasks:
            tl = t.title.strip().lower()
            nl = self.discovery._normalize_title(tl)
            existing_titles.add(tl)
            existing_titles.add(nl)
            existing_prefixes.add(tl[:60])
            existing_prefixes.add(nl[:60])

        # How many slots are available for new tasks (total pending cap = 15)
        max_new = max(1, 15 - len(pending))
        # Track Design task IDs so Implement tasks can depend on them
        design_task_ids = {}  # feature_name -> task_id
        for work in discovered[:min(5, max_new)]:  # Cap per cycle: 5 (was 10)
            title_lower = work.title.strip().lower()
            normalized = self.discovery._normalize_title(title_lower)
            # Skip if we already have a task with this title (or very similar)
            if title_lower in existing_titles or title_lower[:60] in existing_prefixes:
                continue
            if normalized in existing_titles or normalized[:60] in existing_prefixes:
                continue
            # Substring match: skip if core of this title already exists
            core = normalized[:40]
            if len(core) >= 10 and any(core in et for et in existing_titles if len(et) > 10):
                continue
            task_id = f"disc-{int(time.time())}-{new_count}"
            # Wire up Design -> Implement dependencies
            deps = []
            if title_lower.startswith("implement:"):
                feature = title_lower.split(":", 1)[1].strip()
                if feature in design_task_ids:
                    deps = [design_task_ids[feature]]
            task = Task(
                id=task_id,
                type=work.task_type,
                title=work.title,
                description=work.description,
                priority=work.priority,
                depends_on=deps,
                source="discovery",
            )
            self.tasks.append(task)
            self._save_task(task)
            # Track Design tasks for dependency wiring
            if title_lower.startswith("design:"):
                feature = title_lower.split(":", 1)[1].strip()
                design_task_ids[feature] = task_id
            existing_titles.add(title_lower)
            existing_titles.add(normalized)
            existing_prefixes.add(title_lower[:60])
            existing_prefixes.add(normalized[:60])
            new_count += 1

        # LLM-assisted planning for the goal (only on first discovery with a goal)
        if self.config.goal and not getattr(self, '_llm_planned', False):
            self._llm_planned = True
            llm_tasks = self._llm_assisted_plan(self.config.goal)
            known_titles = {t.title for t in self.tasks}
            task_ids_map = {}  # index -> task_id for dependency resolution
            for i, td in enumerate(llm_tasks):
                title = td.get("title", "")
                if title in known_titles:
                    continue
                task_id = f"plan-{int(time.time())}-{new_count}"
                task_ids_map[i] = task_id
                # Resolve depends_on from indices to actual IDs
                deps = []
                for dep_idx in td.get("depends_on_index", []):
                    if dep_idx in task_ids_map:
                        deps.append(task_ids_map[dep_idx])
                task = Task(
                    id=task_id,
                    type=td.get("type", "backend"),
                    title=title,
                    description=td.get("description", ""),
                    priority=td.get("priority", 50),
                    depends_on=deps,
                    estimated_minutes=td.get("estimated_minutes", 30),
                    source="llm-plan",
                )
                self.tasks.append(task)
                self._save_task(task)
                new_count += 1

        if new_count > 0:
            self._log(f"   📡 Discovered {new_count} new tasks")
            for work in discovered[:min(new_count, len(discovered))]:
                self._log(f"      [{work.source}] {work.title[:60]} (priority: {work.priority})")
            self.events.emit(Event(type=EventType.DISCOVERY_COMPLETE, data={
                "new_tasks": new_count, "total_tasks": len(self.tasks),
            }))

        # Save health score to memory
        health = self.discovery.get_project_health()
        memory_file = self.forge_dir / "memory" / "health_history.json"
        history = []
        if memory_file.exists():
            try:
                history = json.loads(memory_file.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError:
                pass
        history.append({"timestamp": datetime.now().isoformat(), **health})
        _atomic_write(memory_file, json.dumps(history[-50:], indent=2))  # Keep last 50

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
        self._dag.add_task(task.id, task.depends_on)

    def _inject_architecture_task(self):
        """Run Claude Sonnet to assess the project — but skip if context is already fresh."""
        # Don't duplicate if one already exists this run
        if any(t.type == "architecture" and t.status != TaskStatus.DONE for t in self.tasks):
            return

        # Skip if SHARED.md was updated recently (within last 30 min) and has real content
        context_file = self.forge_dir / "context" / "SHARED.md"
        if context_file.exists():
            content = context_file.read_text(encoding="utf-8", errors="replace")
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
                "IMPORTANT: Read the existing .forge/context/SHARED.md first. "
                "MERGE your findings into it — preserve all existing sections and content. "
                "Add new information, update what has changed, and remove only what is clearly obsolete. "
                "Do NOT overwrite or replace the file from scratch.\n\n"
                "Ensure these sections exist and are up to date:\n"
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
                description=self._build_review_description(impl_task),
                status=TaskStatus.BACKLOG,
                depends_on=[impl_task.id],
                priority=65,
                source="review",
            )
            self.add_task(review_task)

    def _inject_docs_task(self):
        """Create a docs task after enough implementation work is done."""
        # Skip if there's already a pending docs task
        if any(t.type == "docs" and t.status not in (TaskStatus.DONE, TaskStatus.FAILED) for t in self.tasks):
            return

        # Only inject if we have completed implementation tasks to document
        completed_impl = [t for t in self.tasks
                          if t.type in ("backend", "frontend") and t.status == TaskStatus.DONE]
        if len(completed_impl) < 2:
            return  # Wait until there's enough to document

        # Build a description of what was implemented
        impl_summary = "\n".join(f"- {t.title}" for t in completed_impl[-5:])

        docs_task = Task(
            id=f"docs-{int(time.time())}",
            type="docs",
            title="Update documentation for recent changes",
            description=(
                f"Recent implementation work that needs documentation:\n{impl_summary}\n\n"
                "1. Update README.md with any new features, setup steps, or config options\n"
                "2. Add docstrings to new public functions/classes\n"
                "3. Update .forge/context/SHARED.md if architecture has changed\n"
                "4. Add usage examples where helpful\n\n"
                "Read the code changes and make docs match reality."
            ),
            status=TaskStatus.READY,
            priority=40,
            source="system",
        )
        self.add_task(docs_task)
        self._log(f"  📄 Created docs task for {len(completed_impl)} implemented features")

    def _inject_test_first_tasks(self):
        """TDD: create test tasks that run BEFORE implementation, not after.

        This is the single biggest quality improvement. When tests exist first:
        - The implementation agent has a concrete pass/fail target
        - Edge cases are caught before code is written
        - The validation gate can verify tests pass after implementation
        """
        impl_tasks = [t for t in self.tasks
                      if t.type in ("backend", "frontend")
                      and t.status in (TaskStatus.READY, TaskStatus.BACKLOG)]
        existing_tests = {t.title for t in self.tasks if t.type == "testing"}

        # Cap: only create test tasks for the highest-priority impl tasks
        created = 0
        for impl_task in impl_tasks[:5]:
            test_title = f"Tests: {impl_task.title[:60]}"
            if test_title in existing_tests:
                continue

            # Create a test task that depends on architecture but runs BEFORE implementation
            arch_deps = [d for d in impl_task.depends_on if d.startswith("arch")]

            test_task = Task(
                id=f"test-pre-{impl_task.id}",
                type="testing",
                title=test_title,
                description=(
                    f"Write tests FIRST for: {impl_task.title}\n\n"
                    f"Read .forge/context/SHARED.md for the API contracts and data models.\n"
                    f"Write tests that define the expected behavior BEFORE implementation.\n\n"
                    f"These tests SHOULD FAIL right now — that's correct. They define what\n"
                    f"the implementation agent needs to build.\n\n"
                    f"Write:\n"
                    f"- Unit tests for each function/endpoint in the spec\n"
                    f"- Edge case tests (empty input, invalid data, boundary values)\n"
                    f"- Integration test for the happy path\n\n"
                    f"Commit the tests even though they fail. The implementation agent\n"
                    f"will make them pass."
                ),
                status=TaskStatus.BACKLOG,
                depends_on=arch_deps,  # Depends on architecture, not implementation
                priority=impl_task.priority + 5,  # Slightly higher than impl
                source="test-first",
            )
            self.add_task(test_task)
            created += 1

            # Make the implementation task depend on the test task
            if test_task.id not in impl_task.depends_on:
                impl_task.depends_on.append(test_task.id)
                self._save_task(impl_task)

            # Update impl description to reference the pre-written tests
            if "make the pre-written tests pass" not in impl_task.description:
                impl_task.description += (
                    f"\n\n## Test-Driven Development\n"
                    f"Tests have already been written in task `{test_task.id}`.\n"
                    f"Your job is to make ALL pre-written tests pass.\n"
                    f"Run the test suite after implementation. Do not modify the test files\n"
                    f"unless a test is genuinely wrong (not matching the spec).\n"
                )
                self._save_task(impl_task)

        if created:
            self._log(f"  🧪 Test-first: created {created} test tasks (of {len(impl_tasks)} impl tasks, cap: 5)")

    def _extract_style_guide(self) -> str:
        """Scan the existing codebase and extract coding patterns.

        This makes agents write code that matches the project, not generic LLM style.
        Runs once at startup, cached for all agents.
        """
        style_file = self.forge_dir / "context" / "STYLE.md"

        # Use cached style if it's fresh (< 1 hour old)
        if style_file.exists():
            age = time.time() - style_file.stat().st_mtime
            if age < 3600:
                return style_file.read_text(encoding="utf-8", errors="replace")

        patterns = []

        # Detect language and framework — use targeted globs, not rglob("*")
        # which can hang on large repos with node_modules/venv
        skip_dirs = {".forge", "venv", ".venv", "node_modules", "__pycache__", ".git", "dist", "build"}

        def _safe_rglob(ext: str, limit: int = 20) -> list[Path]:
            """rglob with early termination and directory filtering."""
            results = []
            try:
                for f in self.project_dir.rglob(f"*{ext}"):
                    if any(part in skip_dirs for part in f.parts):
                        continue
                    results.append(f)
                    if len(results) >= limit:
                        break
            except (PermissionError, OSError):
                pass
            return results

        py_files = _safe_rglob(".py")
        js_files = _safe_rglob(".js") + _safe_rglob(".ts") + _safe_rglob(".tsx") + _safe_rglob(".jsx")

        if py_files:
            patterns.append("Language: Python")
            # Sample a few files for style
            for f in py_files[:5]:
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    lines = content.split("\n")

                    # Detect indentation
                    for line in lines[:50]:
                        if line.startswith("    "):
                            patterns.append("Indentation: 4 spaces")
                            break
                        elif line.startswith("\t"):
                            patterns.append("Indentation: tabs")
                            break

                    # Detect type hints
                    if "-> " in content or ": str" in content or ": int" in content:
                        patterns.append("Type hints: yes — use type annotations on all functions")

                    # Detect docstring style
                    if '"""' in content:
                        if ":param " in content:
                            patterns.append("Docstring style: Sphinx (:param, :returns:)")
                        elif "Args:" in content:
                            patterns.append("Docstring style: Google (Args:, Returns:)")
                        else:
                            patterns.append("Docstring style: simple triple-quote")

                    # Detect naming convention
                    if re.search(r'def [a-z]+_[a-z]+', content):
                        patterns.append("Naming: snake_case for functions")
                    if re.search(r'class [A-Z][a-z]+[A-Z]', content):
                        patterns.append("Naming: PascalCase for classes")

                    # Detect common frameworks
                    if "from fastapi" in content or "import fastapi" in content:
                        patterns.append("Framework: FastAPI")
                    elif "from flask" in content:
                        patterns.append("Framework: Flask")
                    elif "from django" in content:
                        patterns.append("Framework: Django")
                    if "import pytest" in content:
                        patterns.append("Testing: pytest")
                    if "from dataclasses" in content:
                        patterns.append("Data models: dataclasses")
                    if "from pydantic" in content:
                        patterns.append("Data models: Pydantic")

                    break  # One file is enough for style detection
                except Exception:
                    continue

        elif js_files:
            patterns.append("Language: JavaScript/TypeScript")
            for f in js_files[:3]:
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    if "import React" in content or "from 'react'" in content:
                        patterns.append("Framework: React")
                    if "export default function" in content:
                        patterns.append("Components: function components")
                    if f.suffix in (".ts", ".tsx"):
                        patterns.append("TypeScript: yes — use types on all exports")
                    if "const " in content and "var " not in content:
                        patterns.append("Variables: const/let (no var)")
                    break
                except Exception:
                    continue

        # Deduplicate
        patterns = list(dict.fromkeys(patterns))

        if not patterns:
            return ""

        guide = "## Project Style Guide (auto-detected)\n" + "\n".join(f"- {p}" for p in patterns)
        guide += "\n\nMatch these patterns exactly. Do NOT introduce new conventions."

        # Cache it
        style_file.parent.mkdir(parents=True, exist_ok=True)
        style_file.write_text(guide, encoding="utf-8")

        return guide

    def _cleanup_stale_locks(self):
        """Remove lock files from previous runs that no longer have active processes."""
        locks_dir = self.forge_dir / "locks"
        if not locks_dir.exists():
            return
        cleaned = 0
        for lock_file in locks_dir.glob("*.lock"):
            try:
                lock_data = json.loads(lock_file.read_text(encoding="utf-8", errors="replace"))
                started = lock_data.get("started", "")
                if started:
                    try:
                        lock_age = (datetime.now() - datetime.fromisoformat(started)).total_seconds()
                    except (ValueError, TypeError):
                        lock_age = float("inf")  # Corrupt timestamp — treat as stale
                    if lock_age > self.config.timeout_minutes * 60:
                        lock_file.unlink()
                        cleaned += 1
                else:
                    lock_file.unlink()
                    cleaned += 1
            except Exception:
                try:
                    lock_file.unlink()
                    cleaned += 1
                except PermissionError:
                    self._log(f"  ⚠ Cannot remove lock {lock_file.name} (file in use)")
                except OSError:
                    cleaned += 1  # Already gone
        if cleaned:
            self._log(f"  🔓 Cleaned {cleaned} stale lock(s) from previous run")

    def _reset_stuck_tasks(self):
        """Reset tasks stuck in IN_PROGRESS from a previous crashed run.

        A task is stuck if it's IN_PROGRESS but:
        - No lock file exists (process was never locked or lock was cleaned up)
        - Lock file exists but is stale (older than timeout — the process died)
        - Lock file exists but no matching entry in _active_processes (restart after crash)
        """
        reset_count = 0
        for task in self.tasks:
            if task.status == TaskStatus.IN_PROGRESS:
                lock_file = self.forge_dir / "locks" / f"{task.id}.lock"
                should_reset = False
                if not lock_file.exists():
                    should_reset = True
                elif task.id not in self._active_processes:
                    # Lock exists but no subprocess — this is a leftover from a crashed run
                    # Check if the lock is stale (older than timeout)
                    try:
                        lock_data = json.loads(lock_file.read_text(encoding="utf-8", errors="replace"))
                        started = lock_data.get("started", "")
                        if started:
                            try:
                                lock_age = (datetime.now() - datetime.fromisoformat(started)).total_seconds()
                            except (ValueError, TypeError):
                                lock_age = float("inf")
                            if lock_age > self.config.timeout_minutes * 60:
                                should_reset = True
                            else:
                                # Lock is recent but no subprocess — crashed mid-run
                                should_reset = True
                        else:
                            should_reset = True
                    except Exception:
                        should_reset = True  # Corrupt lock file
                if should_reset:
                    try:
                        lock_file.unlink(missing_ok=True)
                    except PermissionError:
                        self._log(f"  ⚠ Cannot remove lock for {task.id} (file in use) — skipping reset")
                        continue
                    self._set_status(task, TaskStatus.READY)
                    reset_count += 1
        if reset_count:
            self._log(f"  🔄 Reset {reset_count} stuck task(s) from previous run back to READY")

    def _print_heartbeat(self):
        """Print a one-line status so the user knows the orchestrator is alive."""
        counts = {}
        for t in self.tasks:
            counts[t.status.value] = counts.get(t.status.value, 0) + 1
        active = []
        for tid, proc in list(self._active_processes.items()):
            task = next((t for t in self.tasks if t.id == tid), None)
            if task:
                elapsed = 0
                if task.started_at:
                    try:
                        elapsed = (datetime.now() - datetime.fromisoformat(task.started_at)).total_seconds()
                    except (ValueError, TypeError):
                        elapsed = 0
                active.append(f"{task.id}({task.assigned_provider},{int(elapsed)}s)")
        done = counts.get("done", 0)
        total = len(self.tasks)
        progress = f"{done}/{total}" if total > 0 else "0/0"
        parts = [f"{k}:{v}" for k, v in sorted(counts.items())]
        status_str = " ".join(parts)
        active_str = ", ".join(active) if active else "none"
        self._log(f"  💓 [{progress} complete] [{status_str}] | running: {active_str} | spent: ${self.budget_spent:.2f}")

    def _set_status(self, task: Task, status: TaskStatus):
        """Change task status and sync to disk so dashboard can see it."""
        task.status = status
        self._save_task(task)

    def _update_task_statuses(self):
        done_ids = {t.id for t in self.tasks if t.status == TaskStatus.DONE}
        failed_ids = {t.id for t in self.tasks if t.status == TaskStatus.FAILED}
        in_progress_ids = {t.id for t in self.tasks if t.status == TaskStatus.IN_PROGRESS}
        # Use DAG to find tasks whose deps are met
        dag_ready = set(self._dag.get_ready(done_ids, in_progress_ids))
        promoted = 0
        backlog_stuck = 0
        for task in self.tasks:
            if task.status == TaskStatus.BACKLOG:
                # Check if any dependency has FAILED — if so, this task can never run
                if task.depends_on and any(dep in failed_ids for dep in task.depends_on):
                    failed_dep = next(d for d in task.depends_on if d in failed_ids)
                    self._log(f"  ⚠ {task.id} depends on failed task {failed_dep} — marking FAILED")
                    self._set_status(task, TaskStatus.FAILED)
                    continue
                # Check if dependency refers to a task that doesn't exist at all
                if task.depends_on:
                    missing_deps = [d for d in task.depends_on
                                    if d not in done_ids and d not in failed_ids
                                    and d not in in_progress_ids
                                    and not any(t.id == d for t in self.tasks)]
                    if missing_deps:
                        # Dependencies reference non-existent tasks — clear them
                        self._log(f"  🔗 {task.id}: clearing {len(missing_deps)} missing deps: {missing_deps[:3]}")
                        task.depends_on = [d for d in task.depends_on if d not in missing_deps]
                        self._save_task(task)
                # DAG-aware: check if all dependencies are done
                if task.id in dag_ready or (not task.depends_on or all(dep in done_ids for dep in task.depends_on)):
                    self._set_status(task, TaskStatus.READY)
                    promoted += 1
                else:
                    backlog_stuck += 1
            elif task.status == TaskStatus.BLOCKED:
                # Unblock tasks whose lock has been cleared
                lock_file = self.forge_dir / "locks" / f"{task.id}.lock"
                if not lock_file.exists():
                    # Check deps before moving to READY — task may have been blocked
                    # by a lock but also has unmet dependencies
                    if task.depends_on and not all(dep in done_ids for dep in task.depends_on):
                        self._set_status(task, TaskStatus.BACKLOG)  # Re-enter dep check
                    else:
                        self._set_status(task, TaskStatus.READY)
        if promoted > 0:
            self._log(f"  📋 Promoted {promoted} tasks BACKLOG → READY")
        if backlog_stuck > 0 and promoted == 0:
            # Throttle stuck logging to once per 60 seconds to reduce noise
            now = time.time()
            last_stuck_log = getattr(self, '_last_stuck_log_time', 0)
            if now - last_stuck_log >= 60:
                self._last_stuck_log_time = now
                stuck_examples = [t for t in self.tasks if t.status == TaskStatus.BACKLOG][:3]
                for t in stuck_examples:
                    pending_deps = [d for d in t.depends_on if d not in done_ids]
                    self._log(f"  🔒 {t.id} waiting on: {pending_deps[:5]}")

    def _build_review_description(self, impl_task: Task) -> str:
        """Build a review prompt that uses SHARED.md as the spec to validate against."""
        # Pull API contracts and architecture from SHARED.md as ground truth
        context_file = self.forge_dir / "context" / "SHARED.md"
        spec_section = ""
        if context_file.exists():
            shared = context_file.read_text(encoding="utf-8", errors="replace")
            # Extract architecture + API contracts sections for the reviewer
            for header in ["## Architecture", "## API Contracts", "## Data Models"]:
                start = shared.find(header)
                if start != -1:
                    # Find the next ## header or end of file
                    next_header = shared.find("\n## ", start + len(header))
                    section = shared[start:next_header] if next_header != -1 else shared[start:]
                    spec_section += section.strip() + "\n\n"

        spec_block = ""
        if spec_section:
            spec_block = (
                "\n## Architecture Spec (from SHARED.md — this is the source of truth)\n"
                f"{spec_section[:3000]}\n"
                "Verify the implementation matches these contracts. Flag any deviations.\n"
            )

        return (
            f"Review the code for: {impl_task.title}\n\n"
            f"Task ID: {impl_task.id}\n"
            f"Provider: {impl_task.assigned_provider}\n"
            f"Branch: {impl_task.branch}\n"
            f"{spec_block}\n"
            "## Review Checklist\n"
            "1. **Spec compliance**: Does it match the architecture and API contracts above?\n"
            "2. **Correctness**: Logic errors, off-by-ones, race conditions\n"
            "3. **Security**: Input validation, injection risks, auth checks, hardcoded secrets\n"
            "4. **Error handling**: Missing try/catch, unhelpful error messages, silent failures\n"
            "5. **Performance**: N+1 queries, unnecessary loops, missing indexes\n"
            "6. **Test coverage**: Are edge cases tested? Is behavior tested, not implementation?\n\n"
            "## Output\n"
            "- Fix issues directly in the code where possible.\n"
            "- For architectural concerns, update .forge/context/SHARED.md.\n"
            "- Write a summary to .forge/mail/review/ listing what you fixed and what needs attention.\n"
            "  Use format: `[CRITICAL]`, `[MAJOR]`, `[MINOR]` severity tags.\n"
        )

    def _create_fix_tasks_from_review(self, review_task: Task):
        """After a review completes, scan its mail output for issues and create fix tasks."""
        mail_dir = self.forge_dir / "mail" / "review"
        if not mail_dir.exists():
            return

        # Find the review mail for THIS specific review task (not just the most recent)
        review_output = None
        # First try to find a file named after this review task
        for candidate in mail_dir.glob("*.md"):
            if review_task.id in candidate.name:
                review_output = candidate.read_text(encoding="utf-8", errors="replace")
                break
        # Fall back to most recent file if no task-specific file found
        if review_output is None:
            recent = sorted(mail_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
            if not recent:
                return
            review_output = recent[0].read_text(encoding="utf-8", errors="replace")

        # Count severity tags to decide if we need a fix task
        critical = review_output.lower().count("[critical]")
        major = review_output.lower().count("[major]")

        if critical + major == 0:
            return  # Clean review, no follow-up needed

        # Extract the original task ID from the review task
        original_id = review_task.id.replace("review-", "", 1)
        fix_id = f"fix-{original_id}"

        # Prevent infinite fix→review→fix chains (cap at depth 2)
        if fix_id.count("fix-") >= 2:
            self._log(f"  ⏭ Skipping fix task — already at review depth 2")
            return

        if any(t.id == fix_id for t in self.tasks):
            return  # Already have a fix task

        fix_task = Task(
            id=fix_id,
            type="backend",  # Fixes go back to implementation
            title=f"Fix: {critical} critical, {major} major issues from review",
            description=(
                f"The code reviewer found issues that need fixing.\n\n"
                f"Review findings (from .forge/mail/review/):\n"
                f"{review_output[:3000]}\n\n"
                f"Fix all [CRITICAL] and [MAJOR] issues. [MINOR] issues are optional.\n"
                f"Run tests after fixing to ensure nothing is broken."
            ),
            status=TaskStatus.READY,
            priority=85,  # High priority — fixes should happen before new features
            source="review-fix",
            branch=review_task.branch or "",
        )
        self.add_task(fix_task)
        self._log(f"  🔧 Created fix task for {critical} critical + {major} major review findings")
        self.events.emit(Event(type=EventType.REVIEW_FINDINGS, data={
            "task_id": review_task.id, "critical": critical, "major": major,
            "message": f"Review found {critical} critical + {major} major issues",
        }))

    def _dispatch_task(self, task: Task):
        self._log(f"\n🚀 Dispatching: {task.id} ({task.type}) — {task.title[:50]}")

        # Check lock — prevent double-dispatch
        lock_file = self.forge_dir / "locks" / f"{task.id}.lock"
        if lock_file.exists():
            try:
                lock_data = json.loads(lock_file.read_text(encoding="utf-8", errors="replace"))
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
            self._log(f"  ⚠ No provider available: {e} — task stays READY for retry")
            # Don't permanently fail — leave as READY so next iteration can retry
            # after poll_interval (giving providers time to recover from transient issues)
            self._consecutive_failures += 1
            return

        source_label = "override" if preferred else "auto-routed"
        self._log(f"  → {decision.provider.name} ({source_label}, ~${decision.estimated_cost:.2f})")

        if self.config.dry_run:
            self._log(f"  [DRY RUN] Would spawn {decision.provider.name}")
            self._set_status(task, TaskStatus.DONE)
            self._tasks_completed_since_discovery += 1
            return

        # Conflict-aware branch strategy: check if in-progress branches touch overlapping files
        branch = f"forge/{task.type}/{task.id}"
        conflict_warning = self._check_branch_conflicts(task, branch)
        if conflict_warning:
            self._log(f"  ⚠ {conflict_warning}")

        task.branch = branch

        # Pre-create the branch so agents don't accidentally commit to main
        try:
            subprocess.run(
                ["git", "branch", branch],
                cwd=self.project_dir, capture_output=True, text=True, timeout=10,
            )
        except Exception:
            pass  # Non-fatal — agent can still create it
        task.assigned_provider = decision.provider.name
        self._set_status(task, TaskStatus.IN_PROGRESS)
        task.started_at = datetime.now().isoformat()

        # Lock — atomic create to prevent TOCTOU double-dispatch
        lock_file = self.forge_dir / "locks" / f"{task.id}.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_data = json.dumps({"agent": decision.provider.name, "started": task.started_at})
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(lock_data)
        except FileExistsError:
            self._log(f"  ⚠ Lock already exists for {task.id} — aborting dispatch")
            self._set_status(task, TaskStatus.BLOCKED)
            return

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
        remaining_budget = max(0.01, self.config.budget - self.budget_spent)
        task_cost_cap = min(self.config.max_cost_per_task, remaining_budget)

        # Smart effort scaling — based on actual task complexity, not just type
        effort = self._compute_effort(task)

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

            # Track immediately to prevent orphaned processes if code below raises
            self._active_processes[task.id] = proc

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
            self._log(f"  ✅ Spawned (PID: {proc.pid})")
            self.events.emit(Event(type=EventType.TASK_STARTED, data={
                "task_id": task.id, "title": task.title, "type": task.type,
                "provider": decision.provider.name, "estimated_cost": decision.estimated_cost,
            }))
        except Exception as e:
            self._log(f"  ❌ Spawn failed: {e}")
            # Clean up lock file so the task isn't permanently locked
            try:
                lock_file.unlink(missing_ok=True)
            except OSError:
                pass
            self._set_status(task, TaskStatus.FAILED)
            self.events.emit(Event(type=EventType.TASK_FAILED, data={
                "task_id": task.id, "title": task.title, "error": str(e),
            }))

    def _monitor_active_agents(self):
        completed = []

        # Flush all active log handles so dashboard sees live output
        for task_id, handle in list(self._active_log_handles.items()):
            try:
                handle.flush()
            except Exception:
                pass

        for task_id, proc in list(self._active_processes.items()):
            ret = proc.poll()
            if ret is None:
                # Check timeout — kill runaway agents
                task = next((t for t in self.tasks if t.id == task_id), None)
                if task and task.started_at:
                    try:
                        start = datetime.fromisoformat(task.started_at)
                        elapsed = (datetime.now() - start).total_seconds()
                    except (ValueError, TypeError):
                        elapsed = 0
                    provider = next((p for p in self.providers if p.name == task.assigned_provider), None)
                    timeout_secs = (provider.config.timeout_minutes if provider else 30) * 60
                    if elapsed > timeout_secs:
                        self._log(f"  ⏰ {task_id} TIMED OUT after {elapsed/60:.0f}min (limit: {timeout_secs/60:.0f}min) — killing")
                        _kill_process_tree(proc)
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
                try:
                    start = datetime.fromisoformat(task.started_at)
                    duration = (datetime.now() - start).total_seconds()
                except (ValueError, TypeError):
                    duration = 0.0

            # Parse real cost from agent log output
            cost = self._parse_cost_from_log(task)
            cost_source = "tokens" if cost > 0 else "unknown"
            task.actual_cost_usd = cost
            self.budget_spent += cost
            self.router.record_spend(cost)

            if ret == 0:
                # ── Validation gate: verify the agent actually did something useful ──
                try:
                    validation = self._validate_task_output(task)
                except Exception as val_err:
                    self._log(f"  ⚠ Validation error for {task_id} (accepting anyway): {val_err}")
                    validation = {"passed": True}

                if not validation["passed"]:
                    # Agent exited 0 but failed validation — retry with feedback
                    task.retries += 1
                    if task.retries < task.max_retries:
                        self._log(f"  ⚠ {task_id} passed but failed validation: {validation['reason']}")
                        self._log(f"    Retrying ({task.retries}/{task.max_retries}) with validation feedback...")
                        # Replace (not append) previous failure context to prevent description bloat
                        marker = "\n\n## PREVIOUS ATTEMPT FAILED VALIDATION\n"
                        if marker in task.description:
                            task.description = task.description[:task.description.index(marker)]
                        task.description += (
                            f"\n\n## PREVIOUS ATTEMPT FAILED VALIDATION\n"
                            f"Attempt {task.retries}/{task.max_retries}\n"
                            f"Reason: {validation['reason']}\n"
                            f"{validation.get('details', '')}\n"
                            f"Fix the issues above and try again.\n"
                        )
                        self._set_status(task, TaskStatus.READY)
                        task.complexity = TaskComplexity.HARD  # Escalate to more capable provider
                        self._consecutive_failures += 1
                        self._save_to_memory(task, success=False)
                    else:
                        self._log(f"  ❌ {task_id} failed validation after {task.max_retries} attempts: {validation['reason']}")
                        self._set_status(task, TaskStatus.FAILED)
                        self._consecutive_failures += 1
                        self._save_to_memory(task, success=False)
                        self.events.emit(Event(type=EventType.TASK_FAILED, data={
                            "task_id": task.id, "title": task.title, "type": task.type,
                            "provider": task.assigned_provider, "retries": task.retries,
                            "message": f"Failed validation after {task.retries} retries: {validation['reason']}",
                        }))
                else:
                    self._set_status(task, TaskStatus.DONE)
                    task.completed_at = datetime.now().isoformat()
                    self._tasks_completed_since_discovery += 1
                    self._log(f"  ✅ {task_id} DONE (${cost:.4f} [{cost_source}], {duration:.0f}s, {task.assigned_provider})")
                    self._consecutive_failures = 0  # Reset failure streak on success
                    self._failure_warned = False
                    self._save_to_memory(task, success=True)
                    self._write_completion_handoff(task, duration)
                    self.events.emit(Event(type=EventType.TASK_COMPLETED, data={
                        "task_id": task.id, "title": task.title, "type": task.type,
                        "provider": task.assigned_provider, "cost": cost,
                        "duration": duration, "message": f"{task.title} completed by {task.assigned_provider}",
                    }))

                # Auto-merge the agent's branch back to main
                if task.status == TaskStatus.DONE and task.branch:
                    self._try_auto_merge(task)

                # Auto-create review task — but skip for small/trivial changes
                # Also skip if this is a deeply nested fix task (prevent fix→review→fix→review chains)
                fix_depth = task.id.count("fix-")
                if task.status == TaskStatus.DONE and task.type not in ("review", "docs", "architecture") and task.source not in ("review", "review-fix") and fix_depth < 2:
                    if self._needs_review(task):
                        review_id = f"review-{task.id}"
                        if not any(t.id == review_id for t in self.tasks):
                            review_task = Task(
                                id=review_id,
                                type="review",
                                title=f"Review: {task.title[:60]}",
                                description=self._build_review_description(task),
                                status=TaskStatus.READY,
                                priority=65,
                                source="review",
                                branch=task.branch,
                            )
                            self.add_task(review_task)
                            self._log(f"  📝 Created review task")
                    else:
                        self._log(f"  ⏭ Skipping review — small change, tests passed")

                # After review completes, check if it flagged issues → create fix tasks
                if task.status == TaskStatus.DONE and task.type == "review":
                    self._create_fix_tasks_from_review(task)
            else:
                # ── Agent crashed (non-zero exit) — retry with error context ──
                task.retries += 1
                error_context = self._extract_error_from_log(task)
                if task.retries < task.max_retries:
                    self._log(f"  ⚠ {task_id} FAILED (retry {task.retries}/{task.max_retries}) — escalating")
                    self.events.emit(Event(type=EventType.TASK_RETRYING, data={
                        "task_id": task.id, "title": task.title, "retry": task.retries,
                        "max_retries": task.max_retries, "provider": task.assigned_provider,
                    }))
                    if error_context:
                        # Replace (not append) to prevent description bloat on repeated retries
                        marker = "\n\n## PREVIOUS ATTEMPT CRASHED\n"
                        if marker in task.description:
                            task.description = task.description[:task.description.index(marker)]
                        task.description += (
                            f"\n\n## PREVIOUS ATTEMPT CRASHED\n"
                            f"Attempt {task.retries}/{task.max_retries} | Exit code: {ret}\n"
                            f"Error output:\n```\n{error_context}\n```\n"
                            f"Do NOT repeat the same approach. Fix the error.\n"
                        )
                    self._set_status(task, TaskStatus.READY)
                    task.complexity = TaskComplexity.HARD  # Triggers escalation
                    self._save_to_memory(task, success=False)
                else:
                    self._log(f"  ❌ {task_id} FAILED permanently after {task.max_retries} retries")
                    self._set_status(task, TaskStatus.FAILED)
                    self._consecutive_failures += 1
                    self._save_to_memory(task, success=False)
                    self.events.emit(Event(type=EventType.TASK_FAILED, data={
                        "task_id": task.id, "title": task.title, "type": task.type,
                        "provider": task.assigned_provider, "retries": task.retries,
                        "message": f"{task.title} failed after {task.retries} retries",
                    }))

            try:
                (self.forge_dir / "locks" / f"{task_id}.lock").unlink(missing_ok=True)
                self._save_task(task)
                self._sync_budget()
                completed.append(task_id)
            except Exception as e:
                self._log(f"  ⚠ Failed to finalize {task_id}: {e} — keeping in-flight to retry next cycle")
                # Do NOT add to completed — task stays tracked in _active_processes

        for tid in completed:
            self._active_processes.pop(tid, None)

    def _needs_review(self, task: Task) -> bool:
        """Decide if a completed task needs a full review or can skip.

        Skipping review for trivial changes saves an entire agent call (~$0.50-2.00).
        Only skip when: small diff, tests pass, and not security-sensitive.
        """
        # Always review: retried tasks, fix tasks, anything security-related
        security_signals = ["auth", "login", "password", "token", "secret", "encrypt", "payment", "sql"]
        desc_lower = task.description.lower()
        if any(s in desc_lower for s in security_signals):
            return True
        if task.retries > 0 or task.source == "review-fix":
            return True

        # Check diff size
        if task.branch:
            try:
                result = subprocess.run(
                    ["git", "diff", "--stat", f"main...{task.branch}"],
                    cwd=self.project_dir, capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    lines = result.stdout.strip().split("\n")
                    # Parse the summary line: "3 files changed, 45 insertions(+), 12 deletions(-)"
                    summary = lines[-1] if lines else ""
                    import re as _re
                    insertions = _re.search(r'(\d+) insertion', summary)
                    ins = int(insertions.group(1)) if insertions else 0
                    # Small change = less than 50 lines inserted
                    if ins < 50:
                        return False
            except Exception:
                pass

        return True  # Default: review everything

    def _compute_effort(self, task: Task) -> str:
        """Dynamically size effort based on actual task complexity.

        This saves ~40% on tokens for simple tasks while ensuring complex
        tasks get full reasoning depth. Based on task description length,
        type, retry count, and estimated scope.
        """
        desc_len = len(task.description)
        is_retry = task.retries > 0
        is_fix = task.source == "review-fix"

        # Retries and fixes always get high effort — we already failed once
        if is_retry or is_fix:
            return "high"

        # Type-based baseline
        type_effort = {
            "docs": "low",
            "testing": "medium",
            "review": "medium",
            "architecture": "high",
            "backend": "medium",
            "frontend": "medium",
        }
        baseline = type_effort.get(task.type, "medium")

        # Upgrade based on description complexity signals
        complexity_signals = [
            "security", "authentication", "migration", "refactor",
            "database", "concurrent", "async", "websocket", "oauth",
            "encryption", "payment", "transaction",
        ]
        desc_lower = task.description.lower()
        signal_count = sum(1 for s in complexity_signals if s in desc_lower)

        if signal_count >= 2 or desc_len > 2000:
            return "high"
        if baseline == "low" and signal_count == 0 and desc_len < 500:
            return "low"
        return baseline

    def _validate_task_output(self, task: Task) -> dict:
        """Validate that the agent actually produced useful output before marking done."""
        # Architecture tasks: check that SHARED.md was updated
        if task.type == "architecture":
            ctx = self.forge_dir / "context" / "SHARED.md"
            if ctx.exists():
                content = ctx.read_text(encoding="utf-8", errors="replace")
                if len(content) < 200:
                    return {"passed": False, "reason": "SHARED.md is nearly empty", "details": "Write architecture docs, API contracts, and implementation plan to .forge/context/SHARED.md"}
            return {"passed": True}

        # Implementation tasks: check that files were actually changed
        if task.type in ("backend", "frontend") and task.branch:
            try:
                result = subprocess.run(
                    ["git", "diff", "--name-only", f"main...{task.branch}"],
                    cwd=self.project_dir, capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    changed = [f for f in result.stdout.strip().split("\n") if f.strip()]
                    if not changed:
                        return {"passed": False, "reason": "No files were changed", "details": "You must write code and commit it to your branch."}
            except Exception:
                pass

            # Run tests if a test runner is detected
            test_result = self._run_test_gate(task)
            if test_result and not test_result["passed"]:
                return test_result

        # Testing tasks: check that test files were created
        if task.type == "testing" and task.branch:
            try:
                result = subprocess.run(
                    ["git", "diff", "--name-only", f"main...{task.branch}"],
                    cwd=self.project_dir, capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    changed = result.stdout.strip().split("\n")
                    test_files = [f for f in changed if "test" in f.lower()]
                    if not test_files:
                        return {"passed": False, "reason": "No test files were created or modified", "details": "Write tests in test files."}
            except Exception:
                pass

        return {"passed": True}

    def _run_test_gate(self, task: Task) -> Optional[dict]:
        """Run tests on the agent's branch to verify changes don't break anything.

        Uses git stash + checkout to test the agent's actual branch, then
        restores the original state. Protected by _git_lock.
        """
        # Detect test runner
        runners = [
            (self.project_dir / "package.json", ["cmd", "/c", "npm", "test"] if sys.platform == "win32" else ["npm", "test"]),
            (self.project_dir / "pytest.ini", ["pytest", "--tb=short", "-q"]),
            (self.project_dir / "pyproject.toml", ["pytest", "--tb=short", "-q"]),
            (self.project_dir / "setup.py", ["pytest", "--tb=short", "-q"]),
        ]

        cmd = None
        for marker, test_cmd in runners:
            if marker.exists():
                cmd = test_cmd
                break

        if not cmd:
            return None  # No test runner found — skip

        branch = task.branch
        if not branch:
            return None

        # Acquire git lock to avoid conflicts with concurrent merges
        if not self._git_lock.acquire(timeout=30):
            self._log(f"  ⚠ Could not acquire git lock for test gate — skipping")
            return None

        stashed = False
        original_branch = None
        try:
            # Check for bad git state before mutations
            git_issue = self._check_git_health()
            if git_issue:
                self._log(f"  ⚠ Skipping test gate: {git_issue}")
                return None

            # Save current branch
            original_branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.project_dir, capture_output=True, text=True, timeout=10,
            ).stdout.strip()

            # Check if branch exists
            branch_check = subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                cwd=self.project_dir, capture_output=True, text=True, timeout=10,
            )
            if branch_check.returncode != 0:
                return None  # Branch doesn't exist

            # Stash any dirty state with named message for identification
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.project_dir, capture_output=True, text=True, timeout=10,
            )
            if status.stdout.strip():
                stash_result = subprocess.run(
                    ["git", "stash", "push", "--include-untracked", "-m", f"forge-test-gate-{task.id}"],
                    cwd=self.project_dir, capture_output=True, text=True, timeout=10,
                )
                stashed = stash_result.returncode == 0

            # Checkout the agent's branch
            checkout = subprocess.run(
                ["git", "checkout", branch],
                cwd=self.project_dir, capture_output=True, text=True, timeout=10,
            )
            if checkout.returncode != 0:
                self._log(f"  ⚠ Can't checkout {branch} for test gate — skipping")
                return None

            # Run tests on the agent's branch
            result = subprocess.run(
                cmd, cwd=self.project_dir, capture_output=True, text=True, timeout=120,
            )

            if result.returncode != 0:
                output = (result.stdout + result.stderr).strip()
                last_lines = "\n".join(output.split("\n")[-30:])
                return {
                    "passed": False,
                    "reason": "Tests failed after your changes",
                    "details": f"Test output:\n```\n{last_lines}\n```\nFix the failing tests before marking done.",
                }

        except subprocess.TimeoutExpired:
            return {"passed": False, "reason": "Tests timed out (>2min)", "details": "Tests are hanging. Check for infinite loops."}
        except FileNotFoundError:
            return None  # Test runner not installed
        except Exception as e:
            self._log(f"  ⚠ Test gate error: {e}")
            return None
        finally:
            # Always restore original branch and stash
            checkout_ok = False
            if original_branch and original_branch != "HEAD":
                restore = subprocess.run(
                    ["git", "checkout", original_branch],
                    cwd=self.project_dir, capture_output=True, text=True, timeout=10,
                )
                checkout_ok = restore.returncode == 0
            if stashed:
                if checkout_ok:
                    pop = subprocess.run(
                        ["git", "stash", "pop"],
                        cwd=self.project_dir, capture_output=True, text=True, timeout=10,
                    )
                    if pop.returncode != 0:
                        self._log(f"  ⚠ Stash pop failed (conflict?) — stash preserved in stash list")
                else:
                    # Can't restore to original branch — drop stash to avoid applying to wrong branch
                    self._log(f"  ⚠ Could not restore branch — dropping stash to prevent contamination")
                    subprocess.run(
                        ["git", "stash", "drop"],
                        cwd=self.project_dir, capture_output=True, timeout=10,
                    )
            self._git_lock.release()

        return {"passed": True}

    def _extract_error_from_log(self, task: Task) -> str:
        """Extract the last ~20 lines from an agent's log for error context on retries."""
        log_dir = self.forge_dir / "logs"
        log_files = list(log_dir.glob(f"{task.id}_*.log"))
        if not log_files:
            return ""
        try:
            content = log_files[0].read_text(encoding="utf-8", errors="replace")
            lines = content.strip().split("\n")
            return "\n".join(lines[-20:])
        except Exception:
            return ""

    def _check_branch_conflicts(self, task: Task, branch: str) -> str:
        """Check if this task's likely files overlap with any in-progress branch.

        Returns a warning string if conflict detected, empty string if clean.
        Injects the conflicting branch's diff context into the task prompt
        so the agent can write compatible code.
        """
        in_progress = [t for t in self.tasks if t.status == TaskStatus.IN_PROGRESS and t.branch]
        if not in_progress:
            return ""

        # Estimate which files this task will touch
        task_files = self._estimate_task_files(task)
        if not task_files:
            return ""

        for active_task in in_progress:
            try:
                result = subprocess.run(
                    ["git", "diff", "--name-only", f"main...{active_task.branch}"],
                    cwd=self.project_dir, capture_output=True, text=True, timeout=10,
                )
                if result.returncode != 0:
                    continue
                active_files = set(result.stdout.strip().split("\n"))
                overlap = task_files & active_files
                if overlap:
                    # Inject warning into task description (only once)
                    if "## CONFLICT WARNING" in task.description:
                        return f"File overlap with {active_task.branch}: {', '.join(sorted(overlap)[:3])}"
                    task.description += (
                        f"\n\n## CONFLICT WARNING\n"
                        f"Branch `{active_task.branch}` ({active_task.type}) is currently modifying "
                        f"overlapping files: {', '.join(sorted(overlap)[:5])}\n"
                        f"Be careful with these files. Avoid modifying them if possible, "
                        f"or ensure your changes are compatible.\n"
                    )
                    return f"File overlap with {active_task.branch}: {', '.join(sorted(overlap)[:3])}"
            except Exception:
                continue
        return ""

    def _estimate_task_files(self, task: Task) -> set[str]:
        """Estimate which files a task will touch based on description and type."""
        files = set()
        # Extract file paths from description
        file_refs = re.findall(r'[\w./]+\.(?:py|js|ts|jsx|tsx|json|yaml|md)', task.description)
        files.update(file_refs)
        # Add common files by type
        if task.type == "architecture":
            files.add(".forge/context/SHARED.md")
        elif task.type == "docs":
            files.add("README.md")
        return files

    def _check_git_health(self) -> str | None:
        """Check for bad git states that would cause operations to fail.
        Returns error message if unhealthy, None if OK."""
        git_dir = self.project_dir / ".git"
        bad_states = {
            "MERGE_HEAD": "merge in progress",
            "REVERT_HEAD": "revert in progress",
            "rebase-merge": "rebase in progress",
            "rebase-apply": "rebase in progress",
            "CHERRY_PICK_HEAD": "cherry-pick in progress",
        }
        for marker, description in bad_states.items():
            if (git_dir / marker).exists():
                return f"Git repo in bad state: {description} ({marker} exists)"
        return None

    def _try_auto_merge(self, task: Task):
        """Try to merge the agent's branch back to main. Skip on conflict.

        Uses _git_lock to prevent concurrent merge operations from corrupting
        the git index when multiple agents complete simultaneously.
        """
        branch = task.branch
        if not self._git_lock.acquire(timeout=30):
            self._log(f"  ⚠ Could not acquire git lock for merge — skipping")
            return
        stashed = False
        try:
            # Check for bad git state before attempting any mutations
            git_issue = self._check_git_health()
            if git_issue:
                self._log(f"  ⚠ Skipping merge: {git_issue}")
                return
            # Check if branch exists and has commits ahead of main
            result = subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                cwd=self.project_dir, capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return  # Branch doesn't exist (agent may not have created it)

            # Always merge into main — not whatever branch HEAD is on
            current = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.project_dir, capture_output=True, text=True, timeout=10,
            ).stdout.strip()

            # Detached HEAD returns literal "HEAD" — must checkout main
            if current == "HEAD":
                self._log(f"  ⚠ Detached HEAD detected — checking out main first")

            # Switch to main first if we're not already there
            if current != "main":
                # Stash any uncommitted changes before switching branches
                stashed = False
                stash_check = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=self.project_dir, capture_output=True, text=True, timeout=10,
                )
                if stash_check.stdout.strip():
                    stash_result = subprocess.run(
                        ["git", "stash", "push", "--include-untracked", "-m", f"forge-merge-{task.id}"],
                        cwd=self.project_dir, capture_output=True, text=True, timeout=10,
                    )
                    stashed = stash_result.returncode == 0

                checkout = subprocess.run(
                    ["git", "checkout", "main"],
                    cwd=self.project_dir, capture_output=True, text=True, timeout=10,
                )
                if checkout.returncode != 0:
                    self._log(f"  ⚠ Can't checkout main for merge — skipping (stderr: {checkout.stderr.strip()[:100]})")
                    # Restore stash if we stashed
                    if stashed:
                        pop = subprocess.run(
                            ["git", "stash", "pop"],
                            cwd=self.project_dir, capture_output=True, text=True, timeout=10,
                        )
                        if pop.returncode != 0:
                            self._log(f"  ⚠ Stash pop failed — stash preserved in stash list")
                    return

            # Attempt merge with --no-edit (no interactive editor)
            merge = subprocess.run(
                ["git", "merge", branch, "--no-ff", "-m", f"forge: merge {task.id} ({task.type})"],
                cwd=self.project_dir, capture_output=True, text=True, timeout=30,
            )

            if merge.returncode == 0:
                self._log(f"  🔀 Auto-merged {branch} → main")
                # Clean up the branch now that it's merged
                subprocess.run(
                    ["git", "branch", "-d", branch],
                    cwd=self.project_dir, capture_output=True, text=True, timeout=10,
                )
                # Post-merge safety: run tests to verify merge didn't break anything
                post_merge_ok = self._post_merge_test()
                if not post_merge_ok:
                    self._log(f"  🔴 Tests FAILED after merge — reverting merge to protect main")
                    # -m 1 is required for reverting merge commits (--no-ff creates merge commits)
                    revert = subprocess.run(
                        ["git", "revert", "--no-edit", "-m", "1", "HEAD"],
                        cwd=self.project_dir, capture_output=True, text=True, timeout=30,
                    )
                    if revert.returncode == 0:
                        self._log(f"  ↩ Reverted merge of {branch}. Branch preserved for manual fix.")
                    else:
                        self._log(f"  🔴 REVERT FAILED — resetting to pre-merge state: {revert.stderr.strip()[:200]}")
                        # Abort the failed revert first (clears .git/REVERT_HEAD)
                        subprocess.run(
                            ["git", "revert", "--abort"],
                            cwd=self.project_dir, capture_output=True, timeout=10,
                        )
                        # Fallback: hard reset to pre-merge commit to protect main
                        subprocess.run(
                            ["git", "reset", "--hard", "HEAD~1"],
                            cwd=self.project_dir, capture_output=True, timeout=10,
                        )
            else:
                # Conflict — abort and leave branch for manual merge
                abort = subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=self.project_dir, capture_output=True, text=True, timeout=10,
                )
                if abort.returncode == 0:
                    self._log(f"  ⚠ Merge conflict on {branch} — left for manual merge")
                else:
                    self._log(f"  ❌ Merge conflict + abort failed — repo may be in conflicted state")
                    self._log(f"     Run manually: git merge --abort")
        except (subprocess.TimeoutExpired, Exception) as e:
            self._log(f"  ⚠ Auto-merge skipped: {e}")
        finally:
            # Restore stashed changes if we stashed them
            if stashed:
                pop = subprocess.run(
                    ["git", "stash", "pop"],
                    cwd=self.project_dir, capture_output=True, text=True, timeout=10,
                )
                if pop.returncode != 0:
                    self._log(f"  ⚠ Stash pop failed after merge — stash preserved in stash list")
            self._git_lock.release()

    def _post_merge_test(self) -> bool:
        """Quick test run after merging to verify main isn't broken.

        Returns True if tests pass (or no test runner found). Returns False
        if tests fail, meaning the merge should be reverted.
        """
        runners = [
            (self.project_dir / "package.json", ["cmd", "/c", "npm", "test"] if sys.platform == "win32" else ["npm", "test"]),
            (self.project_dir / "pytest.ini", ["pytest", "--tb=short", "-q", "--timeout=60"]),
            (self.project_dir / "pyproject.toml", ["pytest", "--tb=short", "-q", "--timeout=60"]),
            (self.project_dir / "setup.py", ["pytest", "--tb=short", "-q", "--timeout=60"]),
        ]
        cmd = None
        for marker, test_cmd in runners:
            if marker.exists():
                cmd = test_cmd
                break
        if not cmd:
            return True  # No test runner — assume OK

        try:
            result = subprocess.run(
                cmd, cwd=self.project_dir, capture_output=True, text=True, timeout=120,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
            return True  # Can't run tests — don't block on infrastructure issues

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
                history = json.loads(memory_file.read_text(encoding="utf-8", errors="replace"))
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

        _atomic_write(memory_file, json.dumps(history[-200:], indent=2))  # Keep last 200

    def load_memory(self) -> dict:
        """Load memory from previous sessions for smarter routing."""
        memory = {"task_history": [], "health_history": [], "failed_approaches": {}}
        for name in ["task_history", "health_history"]:
            f = self.forge_dir / "memory" / f"{name}.json"
            if f.exists():
                try:
                    memory[name] = json.loads(f.read_text(encoding="utf-8", errors="replace"))
                except (json.JSONDecodeError, ValueError) as e:
                    self._log(f"  ⚠ Memory file corrupted ({name}.json): {e} — backing up")
                    try:
                        f.rename(str(f) + ".backup")
                    except OSError:
                        pass

        # Extract patterns: which providers fail on which task types
        failures = {}
        for entry in memory["task_history"]:
            if not isinstance(entry, dict):
                continue  # Skip corrupted entries (e.g., strings instead of dicts)
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
        shared_raw = context_file.read_text(encoding="utf-8", errors="replace") if context_file.exists() else ""

        # Architecture & review get full context; implementation tasks get a summary
        if task.type in ("architecture", "review"):
            shared = shared_raw[:8000]  # Cap at ~2k tokens
        else:
            # Extract only the sections relevant to this task type
            shared = self._extract_relevant_context(shared_raw, task.type)

        # ── Mail: messages addressed to this agent type + broadcast ──
        mail = ""
        mail_sources = [task.type, "broadcast"]
        for source in mail_sources:
            mail_dir = self.forge_dir / "mail" / source
            if mail_dir.exists():
                recent_mail = sorted(mail_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
                for f in recent_mail[:3]:  # Only last 3 messages per source
                    content = f.read_text(encoding="utf-8", errors="replace")
                    mail += content[:1000] + "\n---\n"  # Cap each message

        # ── Handoff context: what predecessor tasks produced ──
        handoff = self._build_handoff_context(task)

        # ── Memory hints: only if actually relevant ──
        memory_hint = ""
        if task.retries > 0:
            memory = self.load_memory()
            failed = memory.get("failed_approaches", {})
            relevant = {k: v for k, v in failed.items() if k.startswith(task.type + ":")}
            if relevant:
                memory_hint = "\n## Known Issues from Past Runs\n"
                for k, count in relevant.items():
                    parts = k.split(":")
                    provider = parts[1] if len(parts) >= 2 else parts[0]
                    memory_hint += f"- {provider} failed {count} times on {task.type} tasks\n"

        # ── Diff context for review tasks ──
        diff_context = ""
        if task.type == "review" and task.branch:
            diff_context = self._get_branch_diff(task.branch)

        # ── Focused file list for implementation tasks ──
        file_hint = ""
        if task.type in ("backend", "frontend", "testing") and task.description:
            file_hint = self._suggest_relevant_files(task)

        # ── Active task board summary ──
        board = self._build_task_board_summary(task)

        return f"""# Task: {task.title}
## ID: {task.id} | Type: {task.type} | Branch: {task.branch}

## Description
{task.description}
{file_hint}
{diff_context}
{handoff}

## Shared Context
{shared}
{"## Messages from Other Agents" + chr(10) + mail if mail else ""}
{memory_hint}
{board}

{"" if not self._style_guide else self._style_guide + chr(10)}

## Role Instructions
{role_instructions}

## Acceptance Criteria
- All existing tests must still pass after your changes
- New functionality must have at least one test
- No lint errors introduced
- Exit 0 when done, exit 1 if stuck

## Communication Rules
1. Work on branch: {task.branch}
2. Run tests before committing. Fix failures.
3. **Sending mail to other agents:**
   - To a specific agent type: write to `.forge/mail/<type>/<timestamp>.md`
     Types: architecture, backend, frontend, testing, review
   - To ALL agents: write to `.forge/mail/broadcast/<timestamp>.md`
   - Use timestamp format: `YYYYMMDD-HHMMSS`
4. **Mail format** (so other agents can parse it):
   ```
   FROM: {task.type}
   TO: <target-type or broadcast>
   RE: <one-line subject>
   ---
   <your message>
   ```
5. **When to send mail:**
   - When you complete your task (notify the next agent in the pipeline)
   - When you discover something that affects another agent's work
   - When you're blocked and need input from another agent type
6. Update .forge/context/SHARED.md with architectural decisions or API changes.
7. Exit 0 when done. Exit 1 if stuck.
"""

    def _write_completion_handoff(self, task: Task, duration: float):
        """Write a handoff mail when a task completes so downstream agents know what was done."""
        # Determine who needs to know
        pipeline = {
            "architecture": "broadcast",
            "backend": "testing",
            "frontend": "testing",
            "testing": "review",
            "review": "broadcast",
        }
        target = pipeline.get(task.type, "broadcast")
        mail_dir = self.forge_dir / "mail" / target
        mail_dir.mkdir(parents=True, exist_ok=True)

        # Get list of files changed (from git)
        files_changed = ""
        if task.branch:
            try:
                result = subprocess.run(
                    ["git", "diff", "--name-only", f"main...{task.branch}"],
                    cwd=self.project_dir, capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    files_changed = "\nFiles changed:\n" + "\n".join(
                        f"  - {f}" for f in result.stdout.strip().split("\n")[:20]
                    )
            except Exception:
                pass

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        mail_content = (
            f"FROM: {task.type}\n"
            f"TO: {target}\n"
            f"RE: Task complete — {task.title[:60]}\n"
            f"---\n"
            f"Task `{task.id}` is done.\n"
            f"Provider: {task.assigned_provider}\n"
            f"Branch: {task.branch}\n"
            f"Duration: {duration:.0f}s | Cost: ${task.actual_cost_usd:.4f}\n"
            f"{files_changed}\n"
        )
        (mail_dir / f"{timestamp}.md").write_text(mail_content, encoding="utf-8")

    def _build_handoff_context(self, task: Task) -> str:
        """Build context about what predecessor tasks produced — the handoff."""
        if not task.depends_on:
            return ""

        handoff_parts = []
        for dep_id in task.depends_on:
            dep_task = next((t for t in self.tasks if t.id == dep_id), None)
            if not dep_task or dep_task.status != TaskStatus.DONE:
                continue

            # Check if the predecessor left a mail message
            predecessor_mail = ""
            for mail_type in [task.type, "broadcast"]:
                mail_dir = self.forge_dir / "mail" / mail_type
                if mail_dir.exists():
                    for f in sorted(mail_dir.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
                        content = f.read_text(encoding="utf-8", errors="replace")
                        if dep_task.type in content.lower() or dep_id in content:
                            predecessor_mail = content[:800]
                            break
                if predecessor_mail:
                    break

            summary = f"- **{dep_task.title}** ({dep_task.type}) — completed by {dep_task.assigned_provider}"
            if dep_task.branch:
                summary += f" on branch `{dep_task.branch}`"
            if predecessor_mail:
                summary += f"\n  Handoff note:\n  > {predecessor_mail[:300]}"
            handoff_parts.append(summary)

        if not handoff_parts:
            return ""

        return "\n## Predecessor Tasks (completed before you)\n" + "\n".join(handoff_parts) + "\n"

    def _build_task_board_summary(self, task: Task) -> str:
        """Give agents awareness of the overall project state."""
        done = [t for t in self.tasks if t.status == TaskStatus.DONE]
        in_progress = [t for t in self.tasks if t.status == TaskStatus.IN_PROGRESS]
        ready = [t for t in self.tasks if t.status == TaskStatus.READY and t.id != task.id]

        if not done and not in_progress and not ready:
            return ""

        lines = ["\n## Project Status Board"]
        if done:
            lines.append(f"Completed ({len(done)}):")
            for t in done[-5:]:  # Last 5 completed
                lines.append(f"  - ✅ {t.title[:50]} ({t.type}, by {t.assigned_provider})")
        if in_progress:
            lines.append(f"In Progress ({len(in_progress)}):")
            for t in in_progress:
                lines.append(f"  - 🔄 {t.title[:50]} ({t.type}, by {t.assigned_provider})")
        if ready:
            lines.append(f"Up Next ({len(ready)}):")
            for t in ready[:3]:
                lines.append(f"  - 📋 {t.title[:50]} ({t.type})")

        return "\n".join(lines) + "\n"

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
        # Filter out path traversal attempts
        mentioned = [f for f in mentioned if '..' not in f and not f.startswith('/')]
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
                return role_file.read_text(encoding="utf-8", errors="replace")
        return f"You are the {task_type} agent. Complete your assigned task."

    # ════════════════════════════════════════════════════════════
    # PERSISTENCE
    # ════════════════════════════════════════════════════════════

    def _save_task(self, task: Task):
        f = self.forge_dir / "tasks" / f"{task.id}.json"
        _atomic_write(f, json.dumps({
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
            content = log_files[0].read_text(encoding="utf-8", errors="replace")
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
                    ledger = json.loads(ledger_file.read_text(encoding="utf-8", errors="replace"))
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

            _atomic_write(ledger_file, json.dumps(ledger[-500:], indent=2))
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
                ledger = json.loads(ledger_file.read_text(encoding="utf-8", errors="replace"))
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

        try:
            _atomic_write(budget_file, json.dumps({
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
        except PermissionError:
            self._log("  ⚠ Could not write spending.json (file locked) — will retry next cycle")

        # Keep orchestrator in sync
        self.budget_spent = round(total_cost, 4)

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        self.run_log.append(line)
        # Prevent unbounded memory growth in continuous mode
        if len(self.run_log) > 5000:
            self.run_log = self.run_log[-3000:]

    # ════════════════════════════════════════════════════════════
    # DAG — DEPENDENCY GRAPH
    # ════════════════════════════════════════════════════════════

    def _rebuild_dag(self, _depth: int = 0):
        """Rebuild the dependency DAG from all current tasks."""
        self._dag = DependencyGraph()
        for task in self.tasks:
            self._dag.add_task(task.id, task.depends_on)
        try:
            self._dag.validate()
        except CycleError as e:
            self._log(f"  ⚠ Circular dependency detected: {e}")
            if _depth >= 10:
                self._log("  ❌ Too many cycle-breaking attempts — giving up")
                return
            # Break the cycle by removing the last dependency
            cycle = e.cycle
            if len(cycle) >= 2:
                breaker_id = cycle[-2]
                breaker = next((t for t in self.tasks if t.id == breaker_id), None)
                if breaker and cycle[-1] in breaker.depends_on:
                    breaker.depends_on.remove(cycle[-1])
                    self._save_task(breaker)
                    self._log(f"    Broke cycle by removing {breaker_id} → {cycle[-1]} dependency")
                    self._rebuild_dag(_depth=_depth + 1)  # Retry with depth guard

    # ════════════════════════════════════════════════════════════
    # PARALLEL DISPATCH — CONFLICT-AWARE
    # ════════════════════════════════════════════════════════════

    def _select_non_conflicting(self, candidates: list[Task], max_slots: int) -> list[Task]:
        """Select tasks that won't conflict with each other or in-progress work.

        Conflict = same files likely touched. Uses task type and description
        heuristics since we don't know exact files before the agent runs.
        """
        if not candidates:
            return []

        selected = []
        # Track which "areas" are claimed by in-progress tasks
        claimed_areas = set()
        for t in self.tasks:
            if t.status == TaskStatus.IN_PROGRESS:
                claimed_areas.update(self._task_areas(t))

        for task in candidates:
            if len(selected) >= max_slots:
                break
            areas = self._task_areas(task)
            # Check if this task overlaps with any claimed area
            if areas & claimed_areas:
                continue  # Skip — would conflict
            claimed_areas.update(areas)
            selected.append(task)

        return selected

    def _task_areas(self, task: Task) -> set[str]:
        """Estimate which code areas a task will touch based on description.

        NOTE: task.type is intentionally NOT included — multiple tasks of the
        same type (e.g., two backend tasks working on different features)
        should be allowed to run in parallel. Only specific file/module
        references cause conflicts.
        """
        areas = set()
        desc = task.description.lower()

        # Extract mentioned file paths
        file_refs = re.findall(r'[\w/]+\.(?:py|js|ts|jsx|tsx)', desc)
        areas.update(file_refs)

        # Extract mentioned modules/components
        for pattern in [r'(?:module|component|file)\s+[`\'"]?(\w+)', r'(\w+\.py)', r'(\w+\.ts)']:
            for match in re.findall(pattern, desc):
                areas.add(match.lower())

        return areas

    # ════════════════════════════════════════════════════════════
    # CONTEXT WATCHER — DETECT SHARED.MD CHANGES MID-RUN
    # ════════════════════════════════════════════════════════════

    def _start_context_watcher(self):
        """Start a background thread that watches SHARED.md for changes."""
        context_file = self.forge_dir / "context" / "SHARED.md"
        if context_file.exists():
            self._shared_md_hash = self._file_hash(context_file)

        self._context_watcher_running = True
        watcher = threading.Thread(target=self._context_watcher_loop, daemon=True)
        watcher.start()

    def _context_watcher_loop(self):
        """Background loop that checks for SHARED.md changes every 15s."""
        context_file = self.forge_dir / "context" / "SHARED.md"
        while self._context_watcher_running:
            # Use Event.wait() instead of sleep() so shutdown can interrupt immediately
            if self._context_watcher_stop.wait(timeout=15):
                break  # Stop signal received
            if not context_file.exists():
                continue
            new_hash = self._file_hash(context_file)
            if new_hash != self._shared_md_hash and self._shared_md_hash:
                self._shared_md_hash = new_hash
                self._log("  📝 SHARED.md changed mid-run — context delta will be injected into next prompts")
                # Write a broadcast mail so in-flight agents get notified on next prompt
                # Debounce: skip if a broadcast was written in the last 60 seconds
                mail_dir = self.forge_dir / "mail" / "broadcast"
                mail_dir.mkdir(parents=True, exist_ok=True)
                recent_broadcasts = sorted(mail_dir.glob("*-context-update.md"), key=lambda f: f.stat().st_mtime, reverse=True)
                if recent_broadcasts:
                    last_mtime = recent_broadcasts[0].stat().st_mtime
                    if (datetime.now().timestamp() - last_mtime) < 60:
                        continue  # Debounce — too soon since last broadcast
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                (mail_dir / f"{ts}-context-update.md").write_text(
                    f"FROM: system\nTO: broadcast\nRE: SHARED.md updated\n---\n"
                    f"The shared context (.forge/context/SHARED.md) was updated at {ts}.\n"
                    f"Re-read it before making assumptions about the architecture.\n",
                    encoding="utf-8",
                )
            elif not self._shared_md_hash:
                self._shared_md_hash = new_hash

    @staticmethod
    def _file_hash(path: Path) -> str:
        """Quick hash of file contents for change detection."""
        import hashlib
        try:
            return hashlib.md5(path.read_bytes()).hexdigest()
        except Exception:
            return ""

    # ════════════════════════════════════════════════════════════
    # DYNAMIC PROVIDER ACCURACY
    # ════════════════════════════════════════════════════════════

    def _update_dynamic_accuracy(self):
        """Update provider accuracy scores based on actual performance from memory.

        Blends the benchmark score with real observed success rates:
        effective_score = 0.5 * benchmark + 0.5 * observed
        (Only adjusts after 3+ data points to avoid noise)
        """
        memory = self.load_memory()
        history = memory.get("task_history", [])
        if not history:
            return

        # Aggregate success rates per provider per task type
        stats: dict[str, dict] = {}  # provider -> {successes, total}
        for entry in history:
            provider = entry.get("provider", "")
            if not provider:
                continue
            if provider not in stats:
                stats[provider] = {"successes": 0, "total": 0}
            stats[provider]["total"] += 1
            if entry.get("success"):
                stats[provider]["successes"] += 1

        # Update provider accuracy scores
        for p in self.providers:
            if p.name in stats and stats[p.name]["total"] >= 3:
                observed = stats[p.name]["successes"] / stats[p.name]["total"]
                benchmark = p.config.accuracy_score
                # Blend: half benchmark, half observed
                effective = 0.5 * benchmark + 0.5 * observed
                old = p.config.accuracy_score
                p.config.accuracy_score = round(effective, 3)
                self._dynamic_accuracy[p.name] = effective
                if abs(old - effective) > 0.05:
                    self._log(f"  📊 {p.name} accuracy: {old:.2f} → {effective:.2f} (observed: {observed:.0%} over {stats[p.name]['total']} tasks)")

    # ════════════════════════════════════════════════════════════
    # CHECKPOINT / RESUME
    # ════════════════════════════════════════════════════════════

    def _save_checkpoint(self):
        """Save orchestrator state for crash recovery."""
        checkpoint = {
            "timestamp": datetime.now().isoformat(),
            "budget_spent": self.budget_spent,
            "tasks_completed_since_discovery": self._tasks_completed_since_discovery,
            "active_task_ids": list(self._active_processes.keys()),
            "iteration": getattr(self, '_current_iteration', 0),
        }
        checkpoint_file = self.forge_dir / "state" / "checkpoint.json"
        checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            _atomic_write(checkpoint_file, json.dumps(checkpoint, indent=2))
        except Exception:
            pass

    def _restore_checkpoint(self):
        """Restore state from a previous checkpoint if it exists."""
        checkpoint_file = self.forge_dir / "state" / "checkpoint.json"
        if not checkpoint_file.exists():
            return

        try:
            data = json.loads(checkpoint_file.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, Exception):
            return

        # Restore budget tracking from the ledger (more accurate than checkpoint)
        ledger_file = self.forge_dir / "budget" / "token_ledger.json"
        if ledger_file.exists():
            try:
                ledger = json.loads(ledger_file.read_text(encoding="utf-8", errors="replace"))
                total = sum(e.get("cost_usd", 0) for e in ledger)
                if total > 0:
                    self.budget_spent = round(total, 4)
                    self._log(f"  🔄 Restored budget: ${self.budget_spent:.2f} spent (from ledger)")
            except Exception:
                pass

        # Show resume info
        done_count = sum(1 for t in self.tasks if t.status == TaskStatus.DONE)
        total_count = len(self.tasks)
        if total_count > 0:
            self._log(f"  🔄 Resuming from checkpoint: {done_count}/{total_count} tasks complete, ${self.budget_spent:.2f} spent")

        # Mark previously active tasks for reset
        active_ids = set(data.get("active_task_ids", []))
        if active_ids:
            self._log(f"  🔄 Resetting {len(active_ids)} previously active tasks to READY")

        # Clean up the checkpoint now that we've restored
        try:
            checkpoint_file.unlink()
        except Exception:
            pass

    # ════════════════════════════════════════════════════════════
    # LLM-ASSISTED PLANNING
    # ════════════════════════════════════════════════════════════

    def _llm_assisted_plan(self, goal: str) -> list[dict]:
        """Use a cheap LLM to decompose a complex goal into structured tasks.

        Tries Gemini (free) first, falls back to cheapest available provider.
        Returns a list of task dicts or empty list on failure.
        """
        if not goal:
            return []

        # Build a planning prompt
        skip_dirs = {".forge", "venv", ".venv", "node_modules", "__pycache__", ".git", "dist", "build"}
        existing_files = []
        for ext in ["*.py", "*.js", "*.ts", "*.jsx", "*.tsx"]:
            for f in self.project_dir.rglob(ext):
                if any(part in skip_dirs for part in f.parts):
                    continue
                existing_files.append(str(f.relative_to(self.project_dir)))
                if len(existing_files) >= 30:
                    break
            if len(existing_files) >= 30:
                break
        file_list = "\n".join(f"  - {f}" for f in existing_files[:30])

        planning_prompt = f"""You are a software architect planning work for a multi-agent coding team.

Goal: {goal}

Existing files:
{file_list}

Decompose this goal into 3-8 concrete tasks. Each task should be a single focused unit of work.

Output ONLY a JSON array. Each task object has:
- "title": short title (50 chars max)
- "type": one of "architecture", "backend", "frontend", "testing", "docs"
- "description": what to implement (2-3 sentences)
- "depends_on_index": array of task indices this depends on (0-based), or empty array
- "priority": 0-100 (higher = more important)
- "estimated_minutes": rough time estimate

Example:
[
  {{"title": "Design auth API", "type": "architecture", "description": "Design OAuth2 flow...", "depends_on_index": [], "priority": 90, "estimated_minutes": 15}},
  {{"title": "Implement auth backend", "type": "backend", "description": "Build the auth endpoints...", "depends_on_index": [0], "priority": 80, "estimated_minutes": 45}}
]

Output ONLY valid JSON, no markdown fences, no explanation."""

        # Find cheapest provider for planning
        planning_provider = None
        for p in sorted(self.providers, key=lambda x: x.config.cost_per_hour_usd):
            if p.is_available():
                planning_provider = p
                break

        if not planning_provider:
            return []

        try:
            cmd = planning_provider.build_command(
                prompt=planning_prompt, workdir=self.project_dir,
                effort="low",
            )
            result = subprocess.run(
                cmd, cwd=self.project_dir, capture_output=True, text=True,
                timeout=120, env={**os.environ, "FORGE_PLANNING": "1"},
            )

            if result.returncode != 0:
                return []

            # Parse JSON from output (handle stream-json from Claude)
            output = result.stdout.strip()

            # Try to parse JSON — first try direct parse, then extract array
            tasks_data = None
            # Strip markdown fences if present
            clean = re.sub(r'```(?:json)?\s*', '', output).strip()
            try:
                tasks_data = json.loads(clean)
            except json.JSONDecodeError:
                # Find the outermost balanced JSON array using bracket counting
                start = clean.find('[')
                if start != -1:
                    depth = 0
                    for i in range(start, len(clean)):
                        if clean[i] == '[':
                            depth += 1
                        elif clean[i] == ']':
                            depth -= 1
                            if depth == 0:
                                try:
                                    tasks_data = json.loads(clean[start:i+1])
                                except json.JSONDecodeError:
                                    pass
                                break

            if not isinstance(tasks_data, list):
                return []

            # Validate each task dict has required keys
            required_keys = {"title", "type", "description"}
            valid_tasks = [t for t in tasks_data if isinstance(t, dict) and required_keys.issubset(t.keys())]

            self._log(f"  🧠 LLM planner generated {len(valid_tasks)} tasks via {planning_provider.name}")
            return valid_tasks

        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
            self._log(f"  ⚠ LLM planning failed: {e}")
            return []

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
        _atomic_write(self.forge_dir / "budget" / "run_summary.json", json.dumps({
            "completed": len(done), "failed": len(failed), "remaining": len(rest),
            "total_cost": self.budget_spent, "budget": self.config.budget,
            "health_score": health["score"], "health_readiness": health["readiness"],
            "provider_costs": provider_costs,
        }, indent=2))

        # Persist budget spending
        _atomic_write(self.forge_dir / "budget" / "spending.json", json.dumps({
            "budget_total": self.config.budget,
            "budget_spent": self.budget_spent,
            "transactions": [{"task": t.id, "provider": t.assigned_provider, "cost": t.actual_cost_usd}
                             for t in done],
        }, indent=2))
