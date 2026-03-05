"""Discovery Engine — The thing Agent Teams doesn't have.

Agent Teams and every other system is TASK-DRIVEN: you tell it what to do.
This is GOAL-DRIVEN: you describe the end state, it figures out what to build.

The discovery engine:
1. Scans the codebase for what exists
2. Compares against the goal
3. Generates tasks for what's missing
4. After each cycle, discovers MORE work (TODOs, bugs, missing tests, improvements)
5. Feeds new tasks back into the orchestrator continuously
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class DiscoveredWork:
    """A piece of work discovered by scanning the codebase."""
    source: str          # "todo", "missing_test", "lint", "coverage_gap", "goal_gap", "bug"
    title: str
    description: str
    task_type: str       # architecture, backend, frontend, testing, review, docs
    priority: int        # 0-100
    file_path: str = ""
    line_number: int = 0


class DiscoveryEngine:
    """Scans the project and discovers what needs to be done.
    
    This runs between orchestrator cycles. After agents complete work,
    discovery runs again to find NEW work created by the changes.
    
    Discovery sources:
    1. Goal gap analysis — what's described in the goal but doesn't exist yet
    2. TODO/FIXME/HACK comments in code
    3. Missing test coverage
    4. Lint/type errors
    5. Dead code and unused imports
    6. Security issues (hardcoded secrets, missing input validation)
    7. Missing documentation
    8. Failed tests from previous runs
    """

    def __init__(self, project_dir: Path, forge_dir: Path):
        self.project_dir = project_dir
        self.forge_dir = forge_dir
        self.memory_dir = forge_dir / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def discover_all(self, goal: str = "") -> list[DiscoveredWork]:
        """Run all discovery sources and return prioritized work items."""
        work: list[DiscoveredWork] = []

        work.extend(self._scan_vision_gaps())
        work.extend(self._scan_todos())
        work.extend(self._scan_missing_tests())
        work.extend(self._scan_lint_errors())
        work.extend(self._scan_failed_tests())
        work.extend(self._scan_missing_docs())
        work.extend(self._scan_security_issues())

        # Deduplicate against already-known tasks
        known_ids = self._load_known_task_titles()
        work = [w for w in work if w.title not in known_ids]

        # Sort by priority
        work.sort(key=lambda w: w.priority, reverse=True)

        return work

    def _scan_vision_gaps(self) -> list[DiscoveredWork]:
        """Read VISION.md and find features that don't exist in the codebase yet.
        
        This is what lets agents INVENT features. It parses the vision doc for
        feature descriptions and checklist items, then searches the codebase for
        evidence each one is implemented. Missing features become tasks.
        
        No AI API call needed — pure filesystem matching.
        """
        items = []
        vision_file = self.project_dir / "VISION.md"
        if not vision_file.exists():
            return items

        vision_text = vision_file.read_text()

        # --- Parse unchecked checklist items: "- [ ] something" ---
        checklist_items = re.findall(r'-\s*\[\s*\]\s*(.+)', vision_text)
        for item in checklist_items:
            item = item.strip()
            # Check if this is already a known task
            if not self._evidence_exists(item):
                items.append(DiscoveredWork(
                    source="vision",
                    title=f"Build: {item[:80]}",
                    description=f"From VISION.md checklist (not yet implemented):\n\n{item}",
                    task_type=self._infer_task_type(item),
                    priority=75,
                ))

        # --- Parse feature sections: "### N. Feature Name" ---
        feature_sections = re.findall(
            r'###\s*\d+\.\s*(.+?)(?:\n)(.*?)(?=###|\n##\s|$)',
            vision_text,
            re.DOTALL,
        )
        for feature_name, feature_body in feature_sections:
            feature_name = feature_name.strip()
            feature_body = feature_body.strip()

            # Look for keywords from the feature in the codebase
            keywords = self._extract_keywords(feature_name, feature_body)
            if not self._keywords_in_codebase(keywords):
                # Build a description from the first few bullet points
                bullets = re.findall(r'-\s+(.+)', feature_body)
                desc_lines = bullets[:5] if bullets else [feature_body[:200]]
                description = (
                    f"Feature from VISION.md: {feature_name}\n\n"
                    f"Requirements:\n" +
                    "\n".join(f"- {b.strip()}" for b in desc_lines)
                )

                # Architecture task first — Sonnet designs it
                items.append(DiscoveredWork(
                    source="vision",
                    title=f"Design: {feature_name[:70]}",
                    description=(
                        f"Design the architecture for: {feature_name}\n\n"
                        f"Requirements:\n" +
                        "\n".join(f"- {b.strip()}" for b in desc_lines) +
                        "\n\nWrite the design to .forge/context/SHARED.md under ## Architecture. "
                        "Define data models, API contracts, file structure, and implementation plan. "
                        "Do NOT implement — just design."
                    ),
                    task_type="architecture",
                    priority=72,
                ))

                # Implementation task — Codex builds it
                items.append(DiscoveredWork(
                    source="vision",
                    title=f"Implement: {feature_name[:70]}",
                    description=description,
                    task_type=self._infer_task_type(feature_name + " " + feature_body),
                    priority=70,
                ))

        # --- Parse "## Future Features" section for lower-priority ideas ---
        future_match = re.search(
            r'##\s*Future Features.*?\n(.*?)(?=\n##\s|$)',
            vision_text,
            re.DOTALL,
        )
        if future_match:
            future_items = re.findall(r'-\s+(.+)', future_match.group(1))
            for item in future_items[:5]:  # Cap at 5 future features
                item = item.strip()
                if not self._evidence_exists(item):
                    items.append(DiscoveredWork(
                        source="vision_future",
                        title=f"Future: {item[:70]}",
                        description=f"Future feature from VISION.md:\n\n{item}",
                        task_type=self._infer_task_type(item),
                        priority=35,  # Lower priority than core features
                    ))

        return items[:15]  # Cap per cycle

    def _extract_keywords(self, name: str, body: str) -> list[str]:
        """Extract searchable keywords from a feature description."""
        text = f"{name} {body}".lower()
        # Remove common words
        stopwords = {"the", "a", "an", "is", "are", "for", "to", "in", "of", "and", "or",
                     "with", "that", "this", "from", "by", "on", "at", "per", "all", "each",
                     "every", "when", "not", "but", "should", "must", "can", "will", "using"}
        words = re.findall(r'[a-z_][a-z_0-9]{2,}', text)
        keywords = [w for w in words if w not in stopwords]
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for k in keywords:
            if k not in seen:
                seen.add(k)
                unique.append(k)
        return unique[:10]  # Top 10 keywords

    def _keywords_in_codebase(self, keywords: list[str], threshold: float = 0.4) -> bool:
        """Check if enough keywords appear in the codebase to suggest implementation exists."""
        if not keywords:
            return False

        found = 0
        for kw in keywords[:8]:
            try:
                result = subprocess.run(
                    ["grep", "-rl", "--include=*.py", "--include=*.js", "--include=*.ts",
                     "--exclude-dir=node_modules", "--exclude-dir=.forge",
                     "--exclude-dir=venv", "--exclude-dir=__pycache__",
                     "-i", kw],
                    cwd=self.project_dir, capture_output=True, text=True, timeout=10,
                )
                if result.stdout.strip():
                    found += 1
            except (subprocess.TimeoutExpired, Exception):
                pass

        return (found / len(keywords)) >= threshold

    def _evidence_exists(self, description: str) -> bool:
        """Quick check if a feature description matches anything in the codebase."""
        keywords = self._extract_keywords(description, "")
        return self._keywords_in_codebase(keywords, threshold=0.5)

    def _infer_task_type(self, text: str) -> str:
        """Guess what type of task this is based on keywords."""
        text_lower = text.lower()

        frontend_signals = ["overlay", "widget", "ui", "button", "panel", "display",
                           "drag", "calibrate", "opacity", "hotkey", "settings", "wizard",
                           "dashboard", "component", "render", "style", "css", "layout"]
        backend_signals = ["database", "sqlite", "api", "riot", "cache", "rate limit",
                          "endpoint", "model", "engine", "coaching", "analysis", "compute",
                          "generate", "scouting", "matchup", "subscription", "billing"]
        testing_signals = ["test", "coverage", "assert", "verify", "validate"]
        docs_signals = ["readme", "documentation", "guide", "tutorial", "install"]
        arch_signals = ["architecture", "design", "system", "integration", "refactor",
                       "restructure", "migrate", "infrastructure"]

        scores = {
            "frontend": sum(1 for s in frontend_signals if s in text_lower),
            "backend": sum(1 for s in backend_signals if s in text_lower),
            "testing": sum(1 for s in testing_signals if s in text_lower),
            "docs": sum(1 for s in docs_signals if s in text_lower),
            "architecture": sum(1 for s in arch_signals if s in text_lower),
        }

        best = max(scores, key=scores.get)
        return best if scores[best] > 0 else "backend"  # Default to backend

    def _scan_todos(self) -> list[DiscoveredWork]:
        """Find TODO, FIXME, HACK, XXX comments in code."""
        items = []
        patterns = {
            "TODO": 60,
            "FIXME": 80,
            "HACK": 70,
            "XXX": 75,
            "BUG": 90,
        }

        try:
            for pattern, priority in patterns.items():
                result = subprocess.run(
                    ["grep", "-rn", pattern, "--include=*.py", "--include=*.js",
                     "--include=*.ts", "--include=*.jsx", "--include=*.tsx",
                     "--exclude-dir=node_modules", "--exclude-dir=.forge",
                     "--exclude-dir=venv", "--exclude-dir=__pycache__"],
                    cwd=self.project_dir, capture_output=True, text=True, timeout=30,
                )
                for line in result.stdout.strip().split("\n"):
                    if not line:
                        continue
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        filepath, linenum, content = parts[0], parts[1], parts[2]
                        # Extract the actual comment
                        comment = content.strip()
                        # Remove the pattern prefix to get the description
                        for p in patterns:
                            comment = re.sub(rf'{p}[:\s]*', '', comment, flags=re.IGNORECASE)
                        comment = comment.strip().strip('#').strip('//').strip('/*').strip('*/').strip()

                        if len(comment) > 5:  # Skip empty TODOs
                            items.append(DiscoveredWork(
                                source="todo",
                                title=f"[{pattern}] {comment[:80]}",
                                description=f"Found {pattern} at {filepath}:{linenum}\n\n{content.strip()}",
                                task_type="backend",  # Will be refined
                                priority=priority,
                                file_path=filepath,
                                line_number=int(linenum) if linenum.isdigit() else 0,
                            ))
        except (subprocess.TimeoutExpired, Exception):
            pass

        return items[:20]  # Cap at 20 to avoid flooding

    def _scan_missing_tests(self) -> list[DiscoveredWork]:
        """Find source files that don't have corresponding test files."""
        items = []

        src_files = list(self.project_dir.rglob("*.py"))
        src_files = [f for f in src_files if "test" not in f.name.lower()
                     and "__pycache__" not in str(f)
                     and ".forge" not in str(f)
                     and "venv" not in str(f)
                     and f.name != "__init__.py"]

        test_dir = self.project_dir / "tests"
        existing_tests = set()
        if test_dir.exists():
            existing_tests = {f.stem.replace("test_", "") for f in test_dir.rglob("test_*.py")}

        for src in src_files:
            module_name = src.stem
            if module_name not in existing_tests and module_name != "main":
                rel_path = src.relative_to(self.project_dir)
                items.append(DiscoveredWork(
                    source="missing_test",
                    title=f"Write tests for {module_name}",
                    description=f"No test file found for {rel_path}. Create tests/test_{module_name}.py",
                    task_type="testing",
                    priority=55,
                    file_path=str(rel_path),
                ))

        # Also check JS/TS files
        for ext in ["*.js", "*.ts", "*.jsx", "*.tsx"]:
            js_files = list(self.project_dir.rglob(ext))
            js_files = [f for f in js_files if "test" not in f.name.lower()
                        and "node_modules" not in str(f)
                        and ".forge" not in str(f)
                        and f.name not in ("index.js", "index.ts")]

            for src in js_files:
                test_variants = [
                    src.with_name(f"{src.stem}.test{src.suffix}"),
                    src.with_name(f"{src.stem}.spec{src.suffix}"),
                ]
                if not any(t.exists() for t in test_variants):
                    rel_path = src.relative_to(self.project_dir)
                    items.append(DiscoveredWork(
                        source="missing_test",
                        title=f"Write tests for {src.stem}",
                        description=f"No test file for {rel_path}",
                        task_type="testing",
                        priority=50,
                        file_path=str(rel_path),
                    ))

        return items[:15]

    def _scan_lint_errors(self) -> list[DiscoveredWork]:
        """Run linter and collect errors."""
        items = []

        # Try Python linting with ruff
        try:
            result = subprocess.run(
                ["ruff", "check", "--output-format=json", "."],
                cwd=self.project_dir, capture_output=True, text=True, timeout=30,
            )
            if result.stdout:
                errors = json.loads(result.stdout)
                if len(errors) > 0:
                    # Group by file
                    files_with_errors = set(e.get("filename", "") for e in errors[:50])
                    for f in list(files_with_errors)[:10]:
                        file_errors = [e for e in errors if e.get("filename") == f]
                        items.append(DiscoveredWork(
                            source="lint",
                            title=f"Fix {len(file_errors)} lint errors in {Path(f).name}",
                            description=f"Ruff found {len(file_errors)} issues in {f}",
                            task_type="backend",
                            priority=40,
                            file_path=f,
                        ))
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            pass

        return items[:10]

    def _scan_failed_tests(self) -> list[DiscoveredWork]:
        """Check for previously failed tests."""
        items = []

        # Look for pytest failure logs
        log_dir = self.forge_dir / "logs"
        if log_dir.exists():
            for log_file in log_dir.glob("*.log"):
                content = log_file.read_text(errors="ignore")
                if "FAILED" in content or "ERROR" in content:
                    # Extract failed test names
                    failed = re.findall(r'FAILED\s+([\w/:.]+)', content)
                    for test in failed[:5]:
                        items.append(DiscoveredWork(
                            source="failed_test",
                            title=f"Fix failing test: {test.split('::')[-1] if '::' in test else test}",
                            description=f"Test {test} is failing. Check the log at {log_file.name}",
                            task_type="backend",
                            priority=85,
                            file_path=test.split("::")[0] if "::" in test else "",
                        ))

        return items[:10]

    def _scan_missing_docs(self) -> list[DiscoveredWork]:
        """Check for missing README, docstrings, etc."""
        items = []

        # Missing README
        if not (self.project_dir / "README.md").exists():
            items.append(DiscoveredWork(
                source="missing_docs",
                title="Create README.md",
                description="Project has no README. Create one with setup instructions and description.",
                task_type="docs",
                priority=30,
            ))

        return items

    def _scan_security_issues(self) -> list[DiscoveredWork]:
        """Basic security scanning."""
        items = []

        # Check for hardcoded secrets patterns
        secret_patterns = [
            r'(?:api_key|apikey|secret|password|token)\s*=\s*["\'][^"\']{8,}["\']',
            r'sk-[a-zA-Z0-9]{20,}',
            r'AKIA[0-9A-Z]{16}',
        ]

        try:
            for pattern in secret_patterns:
                result = subprocess.run(
                    ["grep", "-rn", "-E", pattern,
                     "--include=*.py", "--include=*.js", "--include=*.ts",
                     "--include=*.env", "--exclude-dir=node_modules",
                     "--exclude-dir=.forge", "--exclude-dir=venv"],
                    cwd=self.project_dir, capture_output=True, text=True, timeout=15,
                )
                for line in result.stdout.strip().split("\n"):
                    if line and ".env.example" not in line and ".env.sample" not in line:
                        parts = line.split(":", 2)
                        if len(parts) >= 2:
                            items.append(DiscoveredWork(
                                source="security",
                                title=f"Potential hardcoded secret in {Path(parts[0]).name}",
                                description=f"Found what looks like a hardcoded secret at {parts[0]}:{parts[1]}",
                                task_type="review",
                                priority=95,
                                file_path=parts[0],
                                line_number=int(parts[1]) if parts[1].isdigit() else 0,
                            ))
        except (subprocess.TimeoutExpired, Exception):
            pass

        return items[:5]

    def _load_known_task_titles(self) -> set:
        """Load titles of tasks already on the board to avoid duplicates."""
        titles = set()
        tasks_dir = self.forge_dir / "tasks"
        if tasks_dir.exists():
            for f in tasks_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text())
                    titles.add(data.get("title", ""))
                except (json.JSONDecodeError, KeyError):
                    pass
        return titles

    def get_project_health(self) -> dict:
        """Calculate a project health score for ship-readiness."""
        score = 100  # Start at 100, deduct for issues
        details = {}

        # Test coverage proxy: ratio of test files to source files
        src_count = len([f for f in self.project_dir.rglob("*.py")
                         if "test" not in f.name.lower() and "__pycache__" not in str(f)
                         and ".forge" not in str(f) and "venv" not in str(f)
                         and f.name != "__init__.py"])
        test_count = len([f for f in self.project_dir.rglob("test_*.py")
                          if "__pycache__" not in str(f)])
        test_ratio = test_count / max(src_count, 1)
        details["test_coverage_proxy"] = f"{test_ratio:.0%}"
        if test_ratio < 0.5:
            score -= 20
        elif test_ratio < 0.8:
            score -= 10

        # TODO count
        todos = self._scan_todos()
        details["todo_count"] = len(todos)
        score -= min(len(todos) * 2, 20)

        # Lint errors
        lint = self._scan_lint_errors()
        details["lint_issues"] = len(lint)
        score -= min(len(lint) * 3, 15)

        # Security
        security = self._scan_security_issues()
        details["security_issues"] = len(security)
        score -= len(security) * 10

        # Has README
        has_readme = (self.project_dir / "README.md").exists()
        details["has_readme"] = has_readme
        if not has_readme:
            score -= 5

        # Has .env.example (if .env exists)
        if (self.project_dir / ".env").exists():
            has_example = (self.project_dir / ".env.example").exists()
            details["has_env_example"] = has_example
            if not has_example:
                score -= 5

        score = max(0, min(100, score))
        details["score"] = score

        # Ship readiness label
        if score >= 80:
            details["readiness"] = "🟢 Ready to ship"
        elif score >= 60:
            details["readiness"] = "🟡 Almost there"
        elif score >= 40:
            details["readiness"] = "🟠 Needs work"
        else:
            details["readiness"] = "🔴 Not ready"

        return details
