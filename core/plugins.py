"""Plugin system for custom providers.

Drop a .py file in providers/ (or a configured plugin directory) and it
auto-registers as a provider. No need to edit registry.py.

Plugin files must define:
  PROVIDER_CONFIG: ProviderConfig — the default configuration
  PROVIDER_CLASS: type[BaseProvider] — the implementation class

Example plugin (providers/my_agent.py):
```python
from providers.base import BaseProvider, ProviderConfig, Capability, CostTier, TaskResult
from pathlib import Path
from typing import Optional

PROVIDER_CONFIG = ProviderConfig(
    name="my-agent",
    command="my-agent",
    cost_tier=CostTier.LOW,
    capabilities=[Capability.CODE_GENERATION],
    accuracy_score=0.65,
    cost_per_hour_usd=0.50,
)

class MyAgentProvider(BaseProvider):
    def is_available(self) -> bool:
        import shutil
        return shutil.which("my-agent") is not None

    def get_version(self) -> Optional[str]:
        return "1.0.0"

    def build_command(self, prompt, workdir, **kwargs) -> list[str]:
        return ["my-agent", "--prompt", prompt]

    def parse_output(self, stdout, stderr, returncode) -> TaskResult:
        return TaskResult(success=returncode == 0, task_id="", agent_name="my-agent",
                         provider_name="my-agent", branch="")

PROVIDER_CLASS = MyAgentProvider
```
"""

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Optional

from providers.base import BaseProvider, ProviderConfig


class PluginLoader:
    """Discovers and loads provider plugins from filesystem directories."""

    def __init__(self):
        self._loaded_plugins: dict[str, tuple[ProviderConfig, type[BaseProvider]]] = {}

    def load_directory(self, directory: Path) -> dict[str, tuple[ProviderConfig, type[BaseProvider]]]:
        """Scan a directory for plugin files and load them.

        Plugin files must:
        1. Be .py files
        2. Not start with _ (skip __init__.py, etc.)
        3. Define PROVIDER_CONFIG and PROVIDER_CLASS at module level
        """
        if not directory.exists():
            return {}

        loaded = {}
        for py_file in sorted(directory.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            # Skip known non-plugin files
            if py_file.name in ("base.py", "registry.py"):
                continue

            result = self._load_plugin_file(py_file)
            if result:
                name, config, cls = result
                loaded[name] = (config, cls)
                self._loaded_plugins[name] = (config, cls)

        return loaded

    def load_file(self, filepath: Path) -> Optional[tuple[str, ProviderConfig, type[BaseProvider]]]:
        """Load a single plugin file."""
        return self._load_plugin_file(filepath)

    def _load_plugin_file(self, filepath: Path) -> Optional[tuple[str, ProviderConfig, type[BaseProvider]]]:
        """Load a plugin from a Python file. Returns (name, config, class) or None."""
        module_name = f"forge_plugin_{filepath.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, filepath)
            if not spec or not spec.loader:
                return None

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            config = getattr(module, "PROVIDER_CONFIG", None)
            cls = getattr(module, "PROVIDER_CLASS", None)

            if config is None or cls is None:
                # Not a plugin file — just a regular module
                del sys.modules[module_name]
                return None

            if not isinstance(config, ProviderConfig):
                del sys.modules[module_name]
                return None

            if not (isinstance(cls, type) and issubclass(cls, BaseProvider)):
                del sys.modules[module_name]
                return None

            return (config.name, config, cls)

        except Exception:
            # Plugin failed to load — skip it silently
            sys.modules.pop(module_name, None)
            return None

    def get_plugin(self, name: str) -> Optional[tuple[ProviderConfig, type[BaseProvider]]]:
        """Get a loaded plugin by name."""
        return self._loaded_plugins.get(name)

    def list_plugins(self) -> list[str]:
        """List all loaded plugin names."""
        return list(self._loaded_plugins.keys())

    def create_provider(self, name: str, config_override: Optional[dict] = None) -> Optional[BaseProvider]:
        """Instantiate a provider from a loaded plugin."""
        from copy import deepcopy
        plugin = self._loaded_plugins.get(name)
        if not plugin:
            return None

        config, cls = plugin
        config = deepcopy(config)  # Don't mutate the stored default (capabilities list is mutable)
        if config_override:
            for key, val in config_override.items():
                if hasattr(config, key):
                    setattr(config, key, val)

        return cls(config)
