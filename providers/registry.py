"""Provider implementations for all major AI coding agents."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from providers.base import (
    BaseProvider,
    Capability,
    CostTier,
    ProviderConfig,
    TaskResult,
)

# ============================================================================
# PROVIDER REGISTRY — All known providers with defaults
# ============================================================================

PROVIDER_DEFAULTS: dict[str, ProviderConfig] = {
    "gemini": ProviderConfig(
        name="gemini",
        command="gemini",
        model="gemini-2.5-pro",
        api_key_env="",  # Free with Google account
        cost_tier=CostTier.FREE,
        capabilities=[
            Capability.CODE_GENERATION,
            Capability.TESTING,
            Capability.DOCUMENTATION,
            Capability.WEB_SEARCH,
            Capability.LONG_CONTEXT,
            Capability.MULTI_FILE,
        ],
        max_concurrent=3,
        timeout_minutes=20,
        accuracy_score=0.64,  # SWE-bench
        tokens_per_second=80.0,
        cost_per_hour_usd=0.0,
        instruction_file="GEMINI.md",
    ),
    "codex": ProviderConfig(
        name="codex",
        command="codex",
        model="",  # Uses default from ChatGPT sub
        api_key_env="OPENAI_API_KEY",
        cost_tier=CostTier.SUBSCRIPTION,
        capabilities=[
            Capability.CODE_GENERATION,
            Capability.REFACTORING,
            Capability.MULTI_FILE,
            Capability.DEBUGGING,
            Capability.TESTING,
        ],
        max_concurrent=3,
        timeout_minutes=30,
        accuracy_score=0.70,
        tokens_per_second=240.0,  # Fastest
        cost_per_hour_usd=0.50,   # Amortized from $20/mo sub
        instruction_file="AGENTS.md",
    ),
    "codex-mini": ProviderConfig(
        name="codex-mini",
        command="codex",
        model="gpt-5.1-codex-mini",  # $0.25/$2 per MTok — confirmed working
        api_key_env="OPENAI_API_KEY",
        cost_tier=CostTier.LOW,
        capabilities=[
            Capability.CODE_GENERATION,
            Capability.REFACTORING,
            Capability.MULTI_FILE,
            Capability.DEBUGGING,
            Capability.TESTING,
            Capability.DOCUMENTATION,
        ],
        max_concurrent=5,
        timeout_minutes=25,
        accuracy_score=0.62,
        tokens_per_second=240.0,
        cost_per_hour_usd=0.20,   # $0.25/$2.00 per MTok — cheapest paid model
        instruction_file="AGENTS.md",
    ),
    "claude": ProviderConfig(
        name="claude",
        command="claude",
        model="sonnet",
        api_key_env="ANTHROPIC_API_KEY",
        cost_tier=CostTier.MEDIUM,
        capabilities=[
            Capability.CODE_GENERATION,
            Capability.CODE_REVIEW,
            Capability.ARCHITECTURE,
            Capability.TESTING,
            Capability.DOCUMENTATION,
            Capability.REFACTORING,
            Capability.DEBUGGING,
            Capability.MULTI_FILE,
            Capability.LONG_CONTEXT,
        ],
        max_concurrent=5,
        timeout_minutes=45,
        accuracy_score=0.81,  # SWE-bench (Opus)
        tokens_per_second=60.0,
        cost_per_hour_usd=2.00,  # Sonnet rate
        instruction_file="CLAUDE.md",
    ),
    "claude-haiku": ProviderConfig(
        name="claude-haiku",
        command="claude",
        model="haiku",
        api_key_env="ANTHROPIC_API_KEY",
        cost_tier=CostTier.LOW,
        capabilities=[
            Capability.CODE_GENERATION,
            Capability.TESTING,
            Capability.DOCUMENTATION,
            Capability.REFACTORING,
            Capability.DEBUGGING,
            Capability.MULTI_FILE,
        ],
        max_concurrent=5,
        timeout_minutes=20,
        accuracy_score=0.60,
        tokens_per_second=120.0,
        cost_per_hour_usd=0.50,  # $1/$5 per MTok — cheapest Claude
        instruction_file="CLAUDE.md",
    ),
    "claude-opus": ProviderConfig(
        name="claude-opus",
        command="claude",
        model="opus",
        api_key_env="ANTHROPIC_API_KEY",
        cost_tier=CostTier.HIGH,
        capabilities=[
            Capability.CODE_GENERATION,
            Capability.CODE_REVIEW,
            Capability.ARCHITECTURE,
            Capability.TESTING,
            Capability.DOCUMENTATION,
            Capability.REFACTORING,
            Capability.DEBUGGING,
            Capability.MULTI_FILE,
            Capability.LONG_CONTEXT,
        ],
        max_concurrent=2,
        timeout_minutes=60,
        accuracy_score=0.81,
        tokens_per_second=40.0,
        cost_per_hour_usd=8.00,
        instruction_file="CLAUDE.md",
    ),
    "aider": ProviderConfig(
        name="aider",
        command="aider",
        model="",  # Configured via --model flag
        api_key_env="",  # Uses provider's key
        cost_tier=CostTier.LOW,
        capabilities=[
            Capability.CODE_GENERATION,
            Capability.REFACTORING,
            Capability.MULTI_FILE,
            Capability.GIT_NATIVE,
            Capability.DEBUGGING,
        ],
        max_concurrent=2,
        timeout_minutes=30,
        accuracy_score=0.74,  # Polyglot benchmark
        tokens_per_second=60.0,
        cost_per_hour_usd=1.00,
        instruction_file=".aider.conf.yml",
    ),
    "opencode": ProviderConfig(
        name="opencode",
        command="opencode",
        model="",  # 75+ providers
        api_key_env="",
        cost_tier=CostTier.LOW,
        capabilities=[
            Capability.CODE_GENERATION,
            Capability.REFACTORING,
            Capability.MULTI_FILE,
            Capability.DEBUGGING,
        ],
        max_concurrent=4,
        timeout_minutes=30,
        accuracy_score=0.65,
        tokens_per_second=70.0,
        cost_per_hour_usd=0.50,
        instruction_file="",
    ),
    "ollama": ProviderConfig(
        name="ollama",
        command="ollama",
        model="qwen2.5-coder:32b",
        api_key_env="",
        cost_tier=CostTier.FREE,
        capabilities=[
            Capability.CODE_GENERATION,
            Capability.TESTING,
            Capability.DOCUMENTATION,
        ],
        max_concurrent=1,
        timeout_minutes=30,
        accuracy_score=0.35,
        tokens_per_second=30.0,
        cost_per_hour_usd=0.0,  # Local, just electricity
        instruction_file="",
    ),
}


# ============================================================================
# PROVIDER IMPLEMENTATIONS
# ============================================================================

class GeminiProvider(BaseProvider):
    """Google Gemini CLI — FREE tier, 1K requests/day, 1M token context."""

    def is_available(self) -> bool:
        return shutil.which("gemini") is not None or shutil.which("npx") is not None

    def get_version(self) -> Optional[str]:
        try:
            r = subprocess.run(["gemini", "--version"], capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

    def build_command(self, prompt: str, workdir: Path, role_instructions: str = "", allowed_tools=None, max_budget_usd=None, effort=None) -> list[str]:
        cmd = ["gemini"]
        if self.config.model:
            cmd.extend(["--model", self.config.model])
        # Gemini uses -p for non-interactive prompt mode
        full_prompt = f"{role_instructions}\n\n{prompt}" if role_instructions else prompt
        cmd.extend(["-p", full_prompt])
        return cmd

    def parse_output(self, stdout, stderr, returncode) -> TaskResult:
        result = TaskResult(
            success=returncode == 0,
            task_id="", agent_name="gemini", provider_name="gemini", branch="",
            error_message=stderr if returncode != 0 else "",
        )
        # Parse token usage from Gemini output
        token_match = re.search(r'(\d+)\s*tokens?\s*used', stdout, re.IGNORECASE)
        if token_match:
            result.total_tokens = int(token_match.group(1))
            result.input_tokens = int(result.total_tokens * 0.7)
            result.output_tokens = result.total_tokens - result.input_tokens
        # Extract files changed from git-style output
        file_matches = re.findall(r'(?:Created|Modified|Updated)\s+[`\'"]?([^\s`\'"]+\.\w+)', stdout)
        if file_matches:
            result.files_changed = file_matches
        return result


class CodexProvider(BaseProvider):
    """OpenAI Codex CLI — open source, fast, included in ChatGPT Plus."""

    def is_available(self) -> bool:
        return shutil.which("codex") is not None

    def get_version(self) -> Optional[str]:
        try:
            r = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

    def build_command(self, prompt: str, workdir: Path, role_instructions: str = "", allowed_tools=None, max_budget_usd=None, effort=None) -> list[str]:
        cmd = ["codex", "--dangerously-bypass-approvals-and-sandbox", "exec", "--skip-git-repo-check"]
        if self.config.model:
            cmd.extend(["--model", self.config.model])
        full_prompt = f"{role_instructions}\n\n{prompt}" if role_instructions else prompt
        cmd.append(full_prompt)
        return cmd

    def parse_output(self, stdout, stderr, returncode) -> TaskResult:
        result = TaskResult(
            success=returncode == 0,
            task_id="", agent_name=self.config.name, provider_name=self.config.name,
            branch="", error_message=stderr if returncode != 0 else "",
        )
        # Parse Codex token usage: "tokens used\n7,978"
        token_matches = re.findall(r'tokens\s+used\s*\n\s*([\d,]+)', stdout)
        if token_matches:
            result.total_tokens = int(token_matches[-1].replace(",", ""))
            result.input_tokens = int(result.total_tokens * 0.75)
            result.output_tokens = result.total_tokens - result.input_tokens
        # Parse files from Codex output
        file_matches = re.findall(r'(?:Wrote|Edited|Created)\s+([^\s]+\.\w+)', stdout)
        if file_matches:
            result.files_changed = file_matches
        return result


class ClaudeProvider(BaseProvider):
    """Anthropic Claude Code — highest accuracy, agent teams."""

    def is_available(self) -> bool:
        return shutil.which("claude") is not None

    def get_version(self) -> Optional[str]:
        try:
            r = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

    def build_command(self, prompt: str, workdir: Path, role_instructions: str = "", allowed_tools=None, max_budget_usd=None, effort=None) -> list[str]:
        cmd = ["claude", "--dangerously-skip-permissions"]
        if self.config.model:
            cmd.extend(["--model", self.config.model])
        full_prompt = f"{role_instructions}\n\n{prompt}" if role_instructions else prompt
        cmd.extend(["-p", full_prompt, "--output-format", "stream-json", "--verbose"])
        if max_budget_usd is not None:
            cmd.extend(["--max-budget-usd", str(max_budget_usd)])
        if effort and effort in ("low", "medium", "high"):
            cmd.extend(["--effort", effort])
        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])
        return cmd

    def parse_output(self, stdout, stderr, returncode) -> TaskResult:
        result = TaskResult(
            success=returncode == 0,
            task_id="", agent_name="claude", provider_name=self.config.name,
            branch="", error_message=stderr if returncode != 0 else "",
        )
        # Parse Claude stream-json: each line is a JSON object
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            # Result line has total cost
            if obj.get("type") == "result":
                if "total_cost_usd" in obj:
                    result.estimated_cost_usd = float(obj["total_cost_usd"])
                elif "cost_usd" in obj:
                    result.estimated_cost_usd = float(obj["cost_usd"])
                if "model" in obj:
                    result.model_used = obj["model"]
            # Usage stats from assistant messages
            msg = obj.get("message", {})
            usage = msg.get("usage") or obj.get("usage") or {}
            if "input_tokens" in usage:
                result.input_tokens += int(usage["input_tokens"])
            if "output_tokens" in usage:
                result.output_tokens += int(usage["output_tokens"])

        result.total_tokens = result.input_tokens + result.output_tokens
        return result


class AiderProvider(BaseProvider):
    """Aider — git-native, model-agnostic pair programmer."""

    def is_available(self) -> bool:
        return shutil.which("aider") is not None

    def get_version(self) -> Optional[str]:
        try:
            r = subprocess.run(["aider", "--version"], capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

    def build_command(self, prompt: str, workdir: Path, role_instructions: str = "", allowed_tools=None, max_budget_usd=None, effort=None) -> list[str]:
        cmd = ["aider", "--yes-always", "--no-auto-commits"]
        if self.config.model:
            cmd.extend(["--model", self.config.model])
        full_prompt = f"{role_instructions}\n\n{prompt}" if role_instructions else prompt
        cmd.extend(["--message", full_prompt])
        return cmd

    def parse_output(self, stdout, stderr, returncode) -> TaskResult:
        result = TaskResult(
            success=returncode == 0,
            task_id="", agent_name="aider", provider_name="aider",
            branch="", error_message=stderr if returncode != 0 else "",
        )
        # Parse Aider output: "Tokens: 5.2k sent, 1.1k received. Cost: $0.01"
        cost_match = re.search(r'Cost:\s*\$([0-9.]+)', stdout)
        if cost_match:
            result.estimated_cost_usd = float(cost_match.group(1))
        sent_match = re.search(r'([\d.]+)k?\s*sent', stdout)
        recv_match = re.search(r'([\d.]+)k?\s*received', stdout)
        if sent_match:
            val = float(sent_match.group(1))
            result.input_tokens = int(val * 1000) if val < 100 else int(val)
        if recv_match:
            val = float(recv_match.group(1))
            result.output_tokens = int(val * 1000) if val < 100 else int(val)
        result.total_tokens = result.input_tokens + result.output_tokens
        # Parse edited files
        file_matches = re.findall(r'Wrote\s+([^\s]+)', stdout)
        if file_matches:
            result.files_changed = file_matches
        return result


class OpenCodeProvider(BaseProvider):
    """OpenCode — 75+ providers, multi-session."""

    def is_available(self) -> bool:
        return shutil.which("opencode") is not None

    def get_version(self) -> Optional[str]:
        try:
            r = subprocess.run(["opencode", "--version"], capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

    def build_command(self, prompt: str, workdir: Path, role_instructions: str = "", allowed_tools=None, max_budget_usd=None, effort=None) -> list[str]:
        cmd = ["opencode"]
        full_prompt = f"{role_instructions}\n\n{prompt}" if role_instructions else prompt
        cmd.extend(["--message", full_prompt])
        return cmd

    def parse_output(self, stdout, stderr, returncode) -> TaskResult:
        return TaskResult(
            success=returncode == 0,
            task_id="", agent_name="opencode", provider_name="opencode",
            branch="", error_message=stderr if returncode != 0 else "",
        )


# ============================================================================
# REGISTRY
# ============================================================================

PROVIDER_CLASSES: dict[str, type[BaseProvider]] = {
    "gemini": GeminiProvider,
    "codex": CodexProvider,
    "codex-mini": CodexProvider,
    "claude": ClaudeProvider,
    "claude-haiku": ClaudeProvider,
    "claude-opus": ClaudeProvider,
    "aider": AiderProvider,
    "opencode": OpenCodeProvider,
}


def get_provider(name: str, config_override: Optional[dict] = None) -> BaseProvider:
    """Get a provider instance by name with optional config overrides.

    Checks built-in providers first, then loaded plugins.
    """
    if name in PROVIDER_DEFAULTS:
        config = PROVIDER_DEFAULTS[name]
        if config_override:
            for key, val in config_override.items():
                if hasattr(config, key):
                    setattr(config, key, val)
        cls = PROVIDER_CLASSES.get(name)
        if not cls:
            raise ValueError(f"No implementation for provider: {name}")
        return cls(config)

    # Check plugins
    try:
        from core.plugins import PluginLoader
        loader = PluginLoader()
        provider = loader.create_provider(name, config_override)
        if provider:
            return provider
    except ImportError:
        pass

    raise ValueError(f"Unknown provider: {name}. Available: {list(PROVIDER_DEFAULTS.keys())}")


def detect_available_providers(plugin_dirs: Optional[list[Path]] = None) -> list[BaseProvider]:
    """Scan system for all available/installed providers, including plugins."""
    available = []

    # Built-in providers
    for name in PROVIDER_DEFAULTS:
        try:
            provider = get_provider(name)
            if provider.is_available():
                available.append(provider)
        except Exception:
            continue

    # Plugin providers
    if plugin_dirs:
        try:
            from core.plugins import PluginLoader
            loader = PluginLoader()
            for d in plugin_dirs:
                plugins = loader.load_directory(d)
                for name, (config, cls) in plugins.items():
                    try:
                        provider = cls(config)
                        if provider.is_available():
                            available.append(provider)
                    except Exception:
                        continue
        except ImportError:
            pass

    return available
