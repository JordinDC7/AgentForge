"""Base provider interface. Every AI coding agent implements this."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class Capability(Enum):
    """What a provider can do. Used for task routing."""
    CODE_GENERATION = "code_generation"
    CODE_REVIEW = "code_review"
    ARCHITECTURE = "architecture"
    TESTING = "testing"
    DOCUMENTATION = "documentation"
    REFACTORING = "refactoring"
    DEBUGGING = "debugging"
    WEB_SEARCH = "web_search"          # Gemini has this built-in
    MULTI_FILE = "multi_file"
    GIT_NATIVE = "git_native"          # Aider excels here
    LONG_CONTEXT = "long_context"      # 1M+ token windows


class CostTier(Enum):
    """Cost classification for budget routing."""
    FREE = "free"            # Gemini CLI free tier, Ollama
    SUBSCRIPTION = "sub"     # Included in $20/mo sub (Codex, Claude Pro)
    LOW = "low"              # < $1/hr (Haiku, Codex mini)
    MEDIUM = "medium"        # $1-5/hr (Sonnet, GPT-5.1)
    HIGH = "high"            # $5+/hr (Opus, GPT-5.2)


@dataclass
class ProviderConfig:
    """Configuration for a provider instance."""
    name: str
    command: str                         # CLI command (e.g., "claude", "gemini", "codex")
    model: str = ""                      # Specific model override
    api_key_env: str = ""                # Env var name for API key
    cost_tier: CostTier = CostTier.MEDIUM
    capabilities: list[Capability] = field(default_factory=list)
    max_concurrent: int = 1              # How many parallel agents
    timeout_minutes: int = 30            # Kill agent after this
    max_retries: int = 3                 # Retries before escalation
    accuracy_score: float = 0.5          # 0-1, from benchmarks
    tokens_per_second: float = 50.0      # Throughput estimate
    cost_per_hour_usd: float = 1.0       # Estimated hourly cost
    extra_args: list[str] = field(default_factory=list)
    instruction_file: str = ""           # CLAUDE.md / AGENTS.md / GEMINI.md


@dataclass
class TaskResult:
    """Result from an agent completing a task."""
    success: bool
    task_id: str
    agent_name: str
    provider_name: str
    branch: str
    files_changed: list[str] = field(default_factory=list)
    tests_passed: bool = False
    tests_output: str = ""
    error_message: str = ""
    duration_seconds: float = 0.0
    estimated_cost_usd: float = 0.0
    retry_count: int = 0


class BaseProvider(ABC):
    """Abstract interface for AI coding agent providers.
    
    To add a new provider:
    1. Create a new file in providers/ (e.g., my_agent.py)
    2. Subclass BaseProvider
    3. Implement all abstract methods
    4. Register in providers/__init__.py
    """

    def __init__(self, config: ProviderConfig):
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this provider is installed and authenticated."""
        ...

    @abstractmethod
    def get_version(self) -> Optional[str]:
        """Return the installed version string, or None."""
        ...

    @abstractmethod
    def build_command(
        self,
        prompt: str,
        workdir: Path,
        role_instructions: str = "",
        allowed_tools: Optional[list[str]] = None,
        max_budget_usd: Optional[float] = None,
    ) -> list[str]:
        """Build the CLI command to spawn this agent.

        Args:
            prompt: The task description / instructions
            workdir: Working directory for the agent
            role_instructions: Agent role system prompt (from agents/*.md)
            allowed_tools: Optional tool restrictions
            max_budget_usd: Per-task cost cap in USD

        Returns:
            List of command parts (for subprocess)
        """
        ...

    @abstractmethod
    def parse_output(self, stdout: str, stderr: str, returncode: int) -> TaskResult:
        """Parse the agent's output into a structured result."""
        ...

    def estimate_cost(self, duration_seconds: float) -> float:
        """Estimate cost for a given duration based on provider pricing."""
        hours = duration_seconds / 3600
        return hours * self.config.cost_per_hour_usd

    def can_handle(self, required_capabilities: list[Capability]) -> bool:
        """Check if this provider has all required capabilities."""
        return all(cap in self.config.capabilities for cap in required_capabilities)

    def __repr__(self) -> str:
        return f"<{self.name} tier={self.config.cost_tier.value} accuracy={self.config.accuracy_score}>"
