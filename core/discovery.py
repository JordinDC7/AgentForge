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
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)


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
        # Clear codebase cache so we pick up new files from previous agents
        if hasattr(self, '_codebase_cache'):
            del self._codebase_cache
        work: list[DiscoveredWork] = []

        work.extend(self._scan_vision_gaps())
        work.extend(self._scan_todos())
        work.extend(self._scan_missing_tests())
        work.extend(self._scan_lint_errors())
        work.extend(self._scan_failed_tests())
        work.extend(self._scan_missing_docs())
        work.extend(self._scan_security_issues())

        # Deduplicate against already-known tasks using fuzzy matching
        known_titles, known_prefixes, failed_titles = self._load_known_task_titles()
        filtered = []
        for w in work:
            title_lower = w.title.strip().lower()
            # Exact match
            if title_lower in known_titles:
                continue
            # Prefix match (first 60 chars)
            if title_lower[:60] in known_prefixes:
                continue
            # Normalized match (strip common prefixes like [BUG], [TODO], Fix:, etc.)
            normalized = self._normalize_title(title_lower)
            if normalized in known_titles or normalized[:60] in known_prefixes:
                continue
            # Skip tasks that already failed — don't rediscover the same bug
            if normalized in failed_titles or normalized[:60] in failed_titles:
                continue
            # Skip if any known title contains the core of this title (substring match)
            core = normalized[:40]
            if len(core) >= 10 and any(core in kt for kt in known_titles if len(kt) > 10):
                continue
            filtered.append(w)

        # Sort by priority
        filtered.sort(key=lambda w: w.priority, reverse=True)

        # Similarity dedup: within the batch itself, skip items too similar to each other
        # This catches cases like 10 "log.debug" TODOs that are technically unique
        deduplicated = []
        for w in filtered:
            w_norm = self._normalize_title(w.title.lower())
            # Check similarity against items already in this batch
            too_similar = False
            for existing in deduplicated:
                e_norm = self._normalize_title(existing.title.lower())
                # If the first 30 chars match, or they share >60% words, skip
                if w_norm[:30] == e_norm[:30]:
                    too_similar = True
                    break
                w_words = set(w_norm.split())
                e_words = set(e_norm.split())
                if w_words and e_words:
                    overlap = len(w_words & e_words) / max(len(w_words), len(e_words))
                    if overlap > 0.6:
                        too_similar = True
                        break
            if not too_similar:
                deduplicated.append(w)

        return deduplicated[:8]  # Hard cap: max 8 tasks per discovery cycle

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Strip common prefixes/brackets to get the core title for dedup."""
        # Remove [BUG], [TODO], [FIXME] etc.
        title = re.sub(r'^\[[\w]+\]\s*', '', title)
        # Remove "Fix:", "Build:", "Write tests for:", "Implement:" etc.
        title = re.sub(r'^(fix|build|implement|design|write tests for|review|future)[:\s]+', '', title, flags=re.IGNORECASE)
        # Remove "Fix failing test:" prefix
        title = re.sub(r'^fix failing test[:\s]+', '', title, flags=re.IGNORECASE)
        return title.strip()

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

        vision_text = vision_file.read_text(encoding="utf-8", errors="replace")

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
                design_title = f"Design: {feature_name[:70]}"
                items.append(DiscoveredWork(
                    source="vision",
                    title=design_title,
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

                # Implementation task — Codex builds it (depends on design)
                items.append(DiscoveredWork(
                    source="vision",
                    title=f"Implement: {feature_name[:70]}",
                    description=(
                        description +
                        f"\n\n⚠️ DEPENDS ON: \"{design_title}\" — "
                        "wait for design task to complete before implementing."
                    ),
                    task_type=self._infer_task_type(feature_name + " " + feature_body),
                    priority=68,  # Lower than design (72) so design dispatches first
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

        return items[:8]  # Cap vision items per cycle — avoid queuing too many ambitious features

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

    def _keywords_in_codebase(self, keywords: list[str], threshold: float = 0.6) -> bool:
        """Check if enough keywords appear in the codebase to suggest implementation exists.

        Uses pure Python instead of grep for Windows compatibility.
        """
        if not keywords:
            return False

        skip_dirs = {".forge", "venv", ".venv", "node_modules", "__pycache__", ".git", "dist", "build"}
        # Build a cache of file contents on first call (scan up to 100 files)
        if not hasattr(self, '_codebase_cache'):
            self._codebase_cache = []
            # Collect all candidate files first, then cap — avoids bias toward first extension
            all_files = []
            for ext in ["*.py", "*.js", "*.ts", "*.tsx", "*.jsx"]:
                for f in self.project_dir.rglob(ext):
                    if any(part in skip_dirs for part in f.parts):
                        continue
                    all_files.append(f)
                    if len(all_files) >= 500:  # Safety cap on scanning
                        break
                if len(all_files) >= 500:
                    break
            # Evenly sample if over limit
            if len(all_files) > 100:
                import random
                all_files = random.sample(all_files, 100)
            for f in all_files:
                try:
                    self._codebase_cache.append(f.read_text(encoding="utf-8", errors="replace").lower())
                except (OSError, PermissionError):
                    pass

        checked = keywords[:8]
        if not checked:
            return False

        found = 0
        for kw in checked:
            kw_lower = kw.lower()
            if any(kw_lower in content for content in self._codebase_cache):
                found += 1

        return (found / len(checked)) >= threshold

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

    def _infer_task_type_from_file(self, file_path: str, comment: str = "") -> str:
        """Infer task type from file extension and path, falling back to comment keywords."""
        path_lower = file_path.lower()
        if any(path_lower.endswith(ext) for ext in (".jsx", ".tsx", ".css", ".scss", ".html")):
            return "frontend"
        if "test" in path_lower or "spec" in path_lower:
            return "testing"
        if any(seg in path_lower for seg in ("docs/", "readme", "guide")):
            return "docs"
        # Fall back to keyword-based inference from comment text
        if comment:
            return self._infer_task_type(comment)
        return "backend"

    def _scan_todos(self) -> list[DiscoveredWork]:
        """Find TODO, FIXME, HACK, XXX comments in code.

        Deduplicates by file:line to avoid creating multiple tasks for
        the same comment discovered across cycles. Uses pure Python for
        Windows compatibility.
        """
        items = []
        seen_locations = set()  # "filepath:linenum" dedup
        seen_comments = set()   # comment text dedup
        patterns = {
            "TODO": 60,
            "FIXME": 80,
            "HACK": 70,
            "XXX": 75,
            "BUG": 90,
        }
        # Pre-compile word-boundary regexes to avoid false positives
        # (e.g. "debug" matching "BUG", "toDoSomething" matching "TODO")
        pattern_regexes = {p: re.compile(rf'\b{p}\b', re.IGNORECASE) for p in patterns}
        skip_dirs = {"node_modules", ".forge", "venv", ".venv", "__pycache__", ".git", "dist", "build"}
        extensions = {".py", ".js", ".ts", ".jsx", ".tsx"}

        try:
            file_count = 0
            for f in self.project_dir.rglob("*"):
                if f.suffix not in extensions:
                    continue
                if any(part in skip_dirs for part in f.parts):
                    continue
                file_count += 1
                if file_count > 200:
                    break
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                except (OSError, PermissionError):
                    continue
                rel_path = str(f.relative_to(self.project_dir))
                for line_num, line in enumerate(content.split("\n"), 1):
                    for pattern, priority in patterns.items():
                        if not pattern_regexes[pattern].search(line):
                            continue
                        # Deduplicate by location
                        loc_key = f"{rel_path}:{line_num}"
                        if loc_key in seen_locations:
                            continue
                        seen_locations.add(loc_key)

                        # Extract the actual comment
                        comment = line.strip()
                        for p in patterns:
                            comment = re.sub(rf'{p}[:\s]*', '', comment, flags=re.IGNORECASE)
                        comment = comment.strip().strip('#').strip('//').strip('/*').strip('*/').strip()

                        if len(comment) < 10:
                            continue

                        # Deduplicate by comment text
                        comment_key = comment.lower()[:60]
                        if comment_key in seen_comments:
                            continue
                        seen_comments.add(comment_key)

                        items.append(DiscoveredWork(
                            source="todo",
                            title=f"[{pattern}] {comment[:80]}",
                            description=f"Found {pattern} at {rel_path}:{line_num}\n\n{line.strip()}",
                            task_type=self._infer_task_type_from_file(rel_path, comment),
                            priority=priority,
                            file_path=rel_path,
                            line_number=line_num,
                        ))
                        break  # One pattern match per line
        except Exception:
            _log.debug("TODO scan error", exc_info=True)

        return items[:5]  # Cap at 5 to avoid flooding the task queue

    def _scan_missing_tests(self) -> list[DiscoveredWork]:
        """Find source files that don't have corresponding test files."""
        items = []

        # Skip temp files, migrations, configs, and generated files
        skip_names = {"__init__", "setup", "conftest", "manage", "wsgi", "asgi",
                      "settings", "config", "migrations", "alembic"}
        skip_prefixes = ("temp_", "tmp_", "old_", "backup_", "copy_")
        skip_dirs = {"__pycache__", ".forge", "venv", ".venv", "node_modules",
                     "migrations", ".git", "dist", "build"}

        src_files = []
        for f in self.project_dir.rglob("*.py"):
            if any(part in skip_dirs for part in f.parts):
                continue
            if ("test" not in f.name.lower()
                     and f.name != "__init__.py"
                     and f.stem not in skip_names
                     and not f.stem.startswith(skip_prefixes)):
                src_files.append(f)
            if len(src_files) >= 100:  # Safety cap for large repos
                break

        # Collect existing test coverage from multiple patterns:
        #   tests/test_*.py, test/*.py, **/test_*.py, **/*_test.py
        existing_tests = set()
        for test_pattern in ["tests/test_*.py", "test/test_*.py", "**/test_*.py", "**/*_test.py"]:
            for tf in self.project_dir.glob(test_pattern):
                stem = tf.stem
                # Normalize: test_foo -> foo, foo_test -> foo
                if stem.startswith("test_"):
                    stem = stem[5:]
                elif stem.endswith("_test"):
                    stem = stem[:-5]
                existing_tests.add(stem)

        for src in src_files:
            module_name = src.stem
            if module_name not in existing_tests and module_name != "main":
                # Also check for a co-located test file (test_foo.py next to foo.py)
                colocated = src.parent / f"test_{module_name}.py"
                if colocated.exists():
                    continue
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
            js_files = []
            for f in self.project_dir.rglob(ext):
                if any(part in skip_dirs for part in f.parts):
                    continue
                if "test" not in f.name.lower() and f.name not in ("index.js", "index.ts"):
                    js_files.append(f)
                if len(js_files) >= 50:
                    break

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

        return items[:5]

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

        return items[:5]

    def _scan_failed_tests(self) -> list[DiscoveredWork]:
        """Check for previously failed tests.

        Only scans recent logs (last 2 hours) and deduplicates by test name.
        """
        items = []
        seen_tests = set()
        cutoff = datetime.now().timestamp() - 7200  # 2 hours ago

        # Look for pytest failure logs
        log_dir = self.forge_dir / "logs"
        if log_dir.exists():
            for log_file in log_dir.glob("*.log"):
                try:
                    if log_file.stat().st_mtime < cutoff:
                        continue  # Skip old logs
                except OSError:
                    continue
                content = log_file.read_text(encoding="utf-8", errors="replace")
                if "FAILED" in content:
                    failed = re.findall(r'FAILED\s+([\w/:.]+)', content)
                    for test in failed[:3]:
                        test_name = test.split('::')[-1] if '::' in test else test
                        if test_name in seen_tests:
                            continue
                        seen_tests.add(test_name)
                        items.append(DiscoveredWork(
                            source="failed_test",
                            title=f"Fix failing test: {test_name}",
                            description=f"Test {test} is failing. Check the log at {log_file.name}",
                            task_type="backend",
                            priority=85,
                            file_path=test.split("::")[0] if "::" in test else "",
                        ))

        return items[:5]

    def _scan_missing_docs(self) -> list[DiscoveredWork]:
        """Check for missing or outdated documentation."""
        items = []

        # Missing README
        if not (self.project_dir / "README.md").exists():
            items.append(DiscoveredWork(
                source="missing_docs",
                title="Create README.md",
                description="Project has no README. Create one with: project description, setup/install instructions, usage examples, and configuration options.",
                task_type="docs",
                priority=40,
            ))
        else:
            # README exists but may be stale — check if it's very short
            readme = (self.project_dir / "README.md").read_text(encoding="utf-8", errors="replace")
            if len(readme) < 200:
                items.append(DiscoveredWork(
                    source="missing_docs",
                    title="Expand README.md — currently too short",
                    description="README.md exists but has very little content. Add: project overview, setup instructions, usage examples, API docs, and configuration.",
                    task_type="docs",
                    priority=35,
                ))

        # Check for public Python files without module docstrings
        _skip_dirs = {".forge", "venv", ".venv", "__pycache__", "node_modules", ".git", "dist", "build"}
        py_files = []
        for f in self.project_dir.rglob("*.py"):
            if any(part in _skip_dirs for part in f.parts):
                continue
            py_files.append(f)
            if len(py_files) >= 50:
                break
        undocumented = []
        for f in py_files:
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                # Check if file has classes/functions but no module docstring
                if ("def " in content or "class " in content) and not content.strip().startswith('"""') and not content.strip().startswith("'''"):
                    undocumented.append(str(f.relative_to(self.project_dir)))
            except Exception:
                pass

        if len(undocumented) >= 3:
            items.append(DiscoveredWork(
                source="missing_docs",
                title=f"Add docstrings to {len(undocumented)} undocumented modules",
                description=f"These files have no module-level docstrings:\n" + "\n".join(f"- {f}" for f in undocumented[:10]),
                task_type="docs",
                priority=25,
            ))

        # Check for API files without inline docs
        api_patterns = ["api", "routes", "views", "endpoints", "handlers"]
        for f in py_files[:50]:
            fname = f.name.lower()
            if any(p in fname for p in api_patterns):
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    func_count = content.count("def ")
                    doc_count = content.count('"""')
                    if func_count > 3 and doc_count // 2 < func_count:
                        items.append(DiscoveredWork(
                            source="missing_docs",
                            title=f"Document API functions in {f.name}",
                            description=f"{f.relative_to(self.project_dir)} has {func_count} functions but only {doc_count//2} docstrings. Add docstrings with parameter descriptions and return types.",
                            task_type="docs",
                            priority=30,
                            file_path=str(f.relative_to(self.project_dir)),
                        ))
                        break  # One docs task per cycle is enough
                except Exception:
                    pass

        return items

    def _scan_security_issues(self) -> list[DiscoveredWork]:
        """Basic security scanning. Uses pure Python for Windows compatibility."""
        items = []

        secret_patterns = [
            re.compile(r'(?:api_key|apikey|secret|password|token)\s*=\s*["\'][^"\']{8,}["\']', re.IGNORECASE),
            re.compile(r'sk-[a-zA-Z0-9]{20,}'),
            re.compile(r'AKIA[0-9A-Z]{16}'),
        ]
        skip_dirs = {"node_modules", ".forge", "venv", ".venv", "__pycache__", ".git", "dist", "build"}
        extensions = {".py", ".js", ".ts", ".env"}

        try:
            file_count = 0
            for f in self.project_dir.rglob("*"):
                if f.suffix not in extensions:
                    continue
                if any(part in skip_dirs for part in f.parts):
                    continue
                if ".env.example" in f.name or ".env.sample" in f.name:
                    continue
                # Skip test files — they often contain mock secrets/tokens
                if "test" in f.name.lower() or "spec" in f.name.lower() or "fixture" in f.name.lower():
                    continue
                file_count += 1
                if file_count > 100:
                    break
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                except (OSError, PermissionError):
                    continue
                rel_path = str(f.relative_to(self.project_dir))
                for line_num, line in enumerate(content.split("\n"), 1):
                    for pattern in secret_patterns:
                        if pattern.search(line):
                            items.append(DiscoveredWork(
                                source="security",
                                title=f"Potential hardcoded secret in {f.name}",
                                description=f"Found what looks like a hardcoded secret at {rel_path}:{line_num}",
                                task_type="review",
                                priority=95,
                                file_path=rel_path,
                                line_number=line_num,
                            ))
                            break
        except Exception:
            _log.debug("Security scan error", exc_info=True)

        return items[:5]

    def _load_known_task_titles(self) -> tuple[set, set, set]:
        """Load titles of tasks already on the board to avoid duplicates.

        Returns:
            (exact_titles, prefix_titles, failed_titles) — all lowercase.
            failed_titles contains normalized titles of tasks that already failed,
            so we don't rediscover the same broken thing.
        """
        titles = set()
        prefixes = set()
        failed = set()
        tasks_dir = self.forge_dir / "tasks"
        if tasks_dir.exists():
            for f in tasks_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
                    title = data.get("title", "").strip().lower()
                    if not title:
                        continue  # Skip empty titles — they poison prefix matching
                    normalized = self._normalize_title(title)
                    titles.add(title)
                    if normalized:
                        titles.add(normalized)
                    prefixes.add(title[:60])
                    if normalized:
                        prefixes.add(normalized[:60])
                    # Track failed tasks so we don't rediscover them
                    if data.get("status") == "failed":
                        failed.add(normalized)
                        failed.add(normalized[:60])
                except (json.JSONDecodeError, KeyError):
                    pass
        return titles, prefixes, failed

    def get_project_health(self) -> dict:
        """Calculate a project health score for ship-readiness.

        Scoring breakdown (100 total):
          Tests (30 pts): file coverage ratio + whether tests actually pass
          Code quality (25 pts): TODOs by severity + lint errors
          Security (20 pts): hardcoded secrets, missing .env.example
          Documentation (10 pts): README, inline docs
          Task completion (15 pts): ratio of done vs total forge tasks
        """
        breakdown = {}
        score = 0

        # ── Tests (30 pts) ──
        _skip = {"__pycache__", ".forge", "venv", ".venv", "node_modules", ".git", "dist", "build"}
        src_files = []
        test_files = []
        for f in self.project_dir.rglob("*.py"):
            if any(part in _skip for part in f.parts):
                continue
            if f.name.startswith("test_") or "test" in f.name.lower():
                test_files.append(f)
            elif f.name != "__init__.py":
                src_files.append(f)
            if len(src_files) + len(test_files) > 200:
                break
        # Also count JS/TS files
        for ext in ["*.js", "*.ts", "*.jsx", "*.tsx"]:
            for f in self.project_dir.rglob(ext):
                if any(part in _skip for part in f.parts):
                    continue
                name_lower = f.name.lower()
                if "test" in name_lower or "spec" in name_lower:
                    test_files.append(f)
                else:
                    src_files.append(f)
                if len(src_files) + len(test_files) > 300:
                    break

        src_count = len(src_files)
        test_count = len(test_files)
        test_ratio = test_count / max(src_count, 1)
        breakdown["test_file_ratio"] = f"{test_ratio:.0%}"

        # File coverage: 0-20 pts
        if test_ratio >= 0.8:
            test_pts = 20
        elif test_ratio >= 0.5:
            test_pts = 15
        elif test_ratio >= 0.3:
            test_pts = 10
        elif test_ratio > 0:
            test_pts = 5
        else:
            test_pts = 0

        # Test pass rate: 0-10 pts (check if tests actually run and pass)
        test_pass_pts = 0
        try:
            result = subprocess.run(
                ["python", "-m", "pytest", "--tb=no", "-q", "--no-header"],
                cwd=self.project_dir, capture_output=True, text=True, timeout=60,
            )
            output = result.stdout + result.stderr
            # Parse "X passed, Y failed" or "X passed"
            passed_match = re.search(r'(\d+)\s+passed', output)
            failed_match = re.search(r'(\d+)\s+failed', output)
            passed = int(passed_match.group(1)) if passed_match else 0
            failed = int(failed_match.group(1)) if failed_match else 0
            total_tests = passed + failed
            breakdown["tests_passed"] = passed
            breakdown["tests_failed"] = failed
            if total_tests > 0:
                pass_rate = passed / total_tests
                test_pass_pts = round(pass_rate * 10)
            elif result.returncode == 0:
                test_pass_pts = 5  # Tests exist and didn't crash
        except (subprocess.TimeoutExpired, FileNotFoundError):
            breakdown["tests_passed"] = "?"
            breakdown["tests_failed"] = "?"
            test_pass_pts = 0

        test_total = test_pts + test_pass_pts
        breakdown["test_score"] = f"{test_total}/30"
        score += test_total

        # ── Code quality (25 pts) ──
        # Count TODOs directly (not via _scan_todos which caps at 5)
        todo_count = 0
        fixme_count = 0
        hack_count = 0
        _todo_re = re.compile(r'\b(TODO|FIXME|HACK|XXX|BUG)\b', re.IGNORECASE)
        _skip_dirs_q = {"node_modules", ".forge", "venv", ".venv", "__pycache__", ".git", "dist", "build"}
        _extensions_q = {".py", ".js", ".ts", ".jsx", ".tsx"}
        _file_count_q = 0
        for f in self.project_dir.rglob("*"):
            if f.suffix not in _extensions_q:
                continue
            if any(part in _skip_dirs_q for part in f.parts):
                continue
            _file_count_q += 1
            if _file_count_q > 200:
                break
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except (OSError, PermissionError):
                continue
            for line in content.split("\n"):
                for m in _todo_re.finditer(line):
                    tag = m.group(1).upper()
                    if tag in ("FIXME", "BUG"):
                        fixme_count += 1
                    elif tag in ("HACK", "XXX"):
                        hack_count += 1
                    else:
                        todo_count += 1
                    break  # One match per line
        breakdown["todos"] = todo_count
        breakdown["fixmes"] = fixme_count
        breakdown["hacks"] = hack_count
        # FIXME/BUG: -3 pts each, HACK/XXX: -2 pts each, TODO: -1 pt each
        todo_penalty = min(fixme_count * 3 + hack_count * 2 + todo_count * 1, 15)

        # Lint: deduct based on file count with errors, not raw error count
        lint = self._scan_lint_errors()
        lint_penalty = min(len(lint) * 2, 10)
        breakdown["lint_files_with_errors"] = len(lint)

        quality_score = max(0, 25 - todo_penalty - lint_penalty)
        breakdown["quality_score"] = f"{quality_score}/25"
        score += quality_score

        # ── Security (20 pts) ──
        security = self._scan_security_issues()
        sec_penalty = len(security) * 10  # Each secret is a critical issue
        breakdown["security_issues"] = len(security)

        env_penalty = 0
        if (self.project_dir / ".env").exists():
            has_example = (self.project_dir / ".env.example").exists()
            breakdown["has_env_example"] = has_example
            if not has_example:
                env_penalty = 5

        security_score = max(0, 20 - sec_penalty - env_penalty)
        breakdown["security_score"] = f"{security_score}/20"
        score += security_score

        # ── Documentation (10 pts) ──
        doc_score = 0
        has_readme = (self.project_dir / "README.md").exists()
        breakdown["has_readme"] = has_readme
        if has_readme:
            doc_score += 5
            # Bonus: README has meaningful content (>500 chars)
            try:
                readme_len = len((self.project_dir / "README.md").read_text(encoding="utf-8", errors="replace"))
                if readme_len > 500:
                    doc_score += 3
            except Exception:
                pass

        # Has CLAUDE.md or similar project config
        has_project_docs = any((self.project_dir / f).exists()
                              for f in ["CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md"])
        if has_project_docs:
            doc_score += 2
        breakdown["doc_score"] = f"{min(doc_score, 10)}/10"
        score += min(doc_score, 10)

        # ── Task completion (15 pts) ──
        tasks_dir = self.forge_dir / "tasks"
        task_total = 0
        task_done = 0
        task_failed = 0
        if tasks_dir.exists():
            for f in tasks_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
                    task_total += 1
                    if data.get("status") == "done":
                        task_done += 1
                    elif data.get("status") == "failed":
                        task_failed += 1
                except Exception:
                    pass

        if task_total > 0:
            completion_rate = task_done / task_total
            task_pts = round(completion_rate * 15)
            # Penalize failures
            if task_failed > 0:
                task_pts = max(0, task_pts - min(task_failed * 2, 5))
        else:
            task_pts = 8  # No tasks = neutral (not penalized for not using forge)
        breakdown["tasks_done"] = task_done
        breakdown["tasks_total"] = task_total
        breakdown["tasks_failed"] = task_failed
        breakdown["task_score"] = f"{task_pts}/15"
        score += task_pts

        # ── Final score ──
        score = max(0, min(100, score))
        breakdown["score"] = score

        if score >= 80:
            breakdown["readiness"] = "🟢 Ready to ship"
        elif score >= 60:
            breakdown["readiness"] = "🟡 Almost there"
        elif score >= 40:
            breakdown["readiness"] = "🟠 Needs work"
        else:
            breakdown["readiness"] = "🔴 Not ready"

        return breakdown
