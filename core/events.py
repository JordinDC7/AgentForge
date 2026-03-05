"""Event system for AgentForge — webhooks, notifications, and hooks.

Emits events for task lifecycle, budget, and run state changes.
Listeners can be webhooks (HTTP POST), shell commands, or Python callbacks.
"""

import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError


class EventType(Enum):
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_RETRYING = "task.retrying"
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    BUDGET_WARNING = "budget.warning"
    BUDGET_EXHAUSTED = "budget.exhausted"
    DISCOVERY_COMPLETE = "discovery.complete"
    REVIEW_FINDINGS = "review.findings"
    HEALTH_UPDATE = "health.update"


@dataclass
class Event:
    type: EventType
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event": self.type.value,
            "timestamp": self.timestamp,
            **self.data,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


class EventBus:
    """Central event dispatcher with webhook, shell, and callback listeners.

    Configuration via forge.yaml:
    ```yaml
    events:
      webhooks:
        - url: https://hooks.slack.com/services/XXX
          events: [task.completed, task.failed, run.completed]
        - url: https://discord.com/api/webhooks/XXX
          events: ["*"]  # all events
      shell:
        - command: "notify-send 'Forge: {event}' '{message}'"
          events: [task.failed, budget.warning]
    ```
    """

    def __init__(self, forge_dir: Path):
        self.forge_dir = forge_dir
        self._webhooks: list[dict] = []
        self._shell_hooks: list[dict] = []
        self._callbacks: list[tuple[list[EventType] | None, Callable]] = []
        self._event_log: list[Event] = []
        self._lock = threading.Lock()

    def configure(self, config: dict):
        """Load event configuration from forge.yaml events section."""
        events_config = config.get("events", {})
        self._webhooks = events_config.get("webhooks", [])
        self._shell_hooks = events_config.get("shell", [])

    def on(self, event_types: list[EventType] | None, callback: Callable):
        """Register a Python callback for specific event types (None = all)."""
        self._callbacks.append((event_types, callback))

    def emit(self, event: Event):
        """Dispatch an event to all matching listeners."""
        with self._lock:
            self._event_log.append(event)

        # Persist to event log file
        self._persist_event(event)

        # Fire webhooks in background threads (non-blocking)
        for wh in self._webhooks:
            if self._matches(event, wh.get("events", ["*"])):
                threading.Thread(
                    target=self._fire_webhook,
                    args=(wh["url"], event),
                    daemon=True,
                ).start()

        # Fire shell hooks
        for sh in self._shell_hooks:
            if self._matches(event, sh.get("events", ["*"])):
                threading.Thread(
                    target=self._fire_shell,
                    args=(sh["command"], event),
                    daemon=True,
                ).start()

        # Fire Python callbacks
        for types, cb in self._callbacks:
            if types is None or event.type in types:
                try:
                    cb(event)
                except Exception:
                    pass

    def _matches(self, event: Event, patterns: list[str]) -> bool:
        if "*" in patterns:
            return True
        return event.type.value in patterns

    def _fire_webhook(self, url: str, event: Event):
        """POST event as JSON to a webhook URL."""
        try:
            payload = event.to_json().encode("utf-8")
            req = Request(url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("User-Agent", "AgentForge/0.2")
            with urlopen(req, timeout=10) as resp:
                resp.read()
        except (URLError, OSError, Exception):
            pass  # Webhook failures are non-fatal

    def _fire_shell(self, command_template: str, event: Event):
        """Run a shell command with event data substituted."""
        try:
            cmd = command_template.format(
                event=event.type.value,
                message=json.dumps(event.data.get("message", str(event.data))),
                task_id=event.data.get("task_id", ""),
                provider=event.data.get("provider", ""),
                cost=event.data.get("cost", 0),
            )
            subprocess.run(
                cmd, shell=True, timeout=30,
                capture_output=True, text=True,
            )
        except (subprocess.TimeoutExpired, Exception):
            pass

    def _persist_event(self, event: Event):
        """Append event to the event log file."""
        log_file = self.forge_dir / "logs" / "events.jsonl"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(log_file, "a") as f:
                f.write(event.to_json() + "\n")
        except Exception:
            pass

    def get_recent(self, count: int = 50, event_type: Optional[EventType] = None) -> list[Event]:
        """Get recent events, optionally filtered by type."""
        with self._lock:
            events = self._event_log
            if event_type:
                events = [e for e in events if e.type == event_type]
            return events[-count:]

    def format_slack(self, event: Event) -> dict:
        """Format event as a Slack-compatible message payload."""
        icons = {
            EventType.TASK_COMPLETED: ":white_check_mark:",
            EventType.TASK_FAILED: ":x:",
            EventType.TASK_STARTED: ":rocket:",
            EventType.BUDGET_WARNING: ":warning:",
            EventType.BUDGET_EXHAUSTED: ":no_entry:",
            EventType.RUN_COMPLETED: ":tada:",
            EventType.REVIEW_FINDINGS: ":mag:",
        }
        icon = icons.get(event.type, ":gear:")
        text = f"{icon} *{event.type.value}*"
        if "task_id" in event.data:
            text += f" | `{event.data['task_id']}`"
        if "message" in event.data:
            text += f"\n{event.data['message']}"
        return {"text": text}
