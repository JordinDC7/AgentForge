"""DAG-based dependency graph with cycle detection and topological sort.

Replaces the linear dependency scan with a proper directed acyclic graph.
Catches circular dependencies at plan time instead of deadlocking at runtime.
"""

from collections import defaultdict, deque


class CycleError(Exception):
    """Raised when a circular dependency is detected."""
    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__(f"Circular dependency detected: {' -> '.join(cycle)}")


class DependencyGraph:
    """Directed acyclic graph for task dependencies.

    Usage:
        graph = DependencyGraph()
        graph.add_task("backend", depends_on=["architecture"])
        graph.add_task("testing", depends_on=["backend"])
        graph.validate()  # raises CycleError if circular
        order = graph.topological_sort()  # ['architecture', 'backend', 'testing']
        ready = graph.get_ready(done={"architecture"})  # ['backend']
    """

    def __init__(self):
        self._edges: dict[str, list[str]] = defaultdict(list)  # task -> dependencies
        self._reverse: dict[str, list[str]] = defaultdict(list)  # dependency -> dependents
        self._all_nodes: set[str] = set()

    def add_task(self, task_id: str, depends_on: list[str] | None = None):
        self._all_nodes.add(task_id)
        deps = depends_on or []
        # Clean up stale reverse edges before overwriting
        old_deps = self._edges.get(task_id, [])
        for old_dep in old_deps:
            rev = self._reverse.get(old_dep, [])
            if task_id in rev:
                rev.remove(task_id)
        self._edges[task_id] = deps
        for dep in deps:
            self._all_nodes.add(dep)
            self._reverse[dep].append(task_id)

    def remove_task(self, task_id: str):
        self._all_nodes.discard(task_id)
        # Clean up reverse edges for this task's dependencies
        for dep in self._edges.get(task_id, []):
            rev = self._reverse.get(dep, [])
            if task_id in rev:
                rev.remove(task_id)
        self._edges.pop(task_id, None)
        # Remove this task from other tasks' dependency lists
        for node, deps in self._edges.items():
            if task_id in deps:
                deps.remove(task_id)
        # Clean up reverse edges where this task is a dependency
        for dep, dependents in self._reverse.items():
            if task_id in dependents:
                dependents.remove(task_id)
        self._reverse.pop(task_id, None)

    def validate(self) -> bool:
        """Check for cycles using DFS. Raises CycleError if found."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in self._all_nodes}
        parent = {}

        def dfs(node: str) -> list[str] | None:
            color[node] = GRAY
            for dep in self._edges.get(node, []):
                if dep not in color:
                    continue
                if color[dep] == GRAY:
                    # Found cycle — reconstruct it
                    cycle = [dep, node]
                    cur = node
                    while cur in parent and parent[cur] != dep:
                        cur = parent[cur]
                        cycle.append(cur)
                    cycle.append(dep)
                    cycle.reverse()
                    raise CycleError(cycle)
                if color[dep] == WHITE:
                    parent[dep] = node
                    result = dfs(dep)
                    if result:
                        return result
            color[node] = BLACK
            return None

        for node in self._all_nodes:
            if color.get(node) == WHITE:
                dfs(node)
        return True

    def topological_sort(self) -> list[str]:
        """Return tasks in dependency order (Kahn's algorithm).

        Tasks with no dependencies come first.
        Raises CycleError if the graph has cycles.
        """
        in_degree = {n: 0 for n in self._all_nodes}
        for node, deps in self._edges.items():
            in_degree[node] = len([d for d in deps if d in self._all_nodes])

        queue = deque(n for n, d in in_degree.items() if d == 0)
        result = []

        while queue:
            node = queue.popleft()
            result.append(node)
            for dependent in self._reverse.get(node, []):
                if dependent in in_degree:
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        queue.append(dependent)

        if len(result) != len(self._all_nodes):
            # Find the cycle for a useful error message
            remaining = self._all_nodes - set(result)
            raise CycleError(list(remaining)[:5])

        return result

    def get_ready(self, done: set[str], in_progress: set[str] | None = None) -> list[str]:
        """Get tasks whose dependencies are all satisfied.

        Args:
            done: Set of completed task IDs
            in_progress: Set of currently running task IDs (excluded from ready)
        """
        in_progress = in_progress or set()
        ready = []
        for node in self._all_nodes:
            if node in done or node in in_progress:
                continue
            deps = self._edges.get(node, [])
            if all(d in done for d in deps if d in self._all_nodes):
                ready.append(node)
        return ready

    def get_dependents(self, task_id: str) -> list[str]:
        """Get all tasks that depend on the given task (direct dependents)."""
        return list(self._reverse.get(task_id, []))

    def get_all_downstream(self, task_id: str) -> set[str]:
        """Get all tasks transitively downstream of the given task."""
        visited = set()
        queue = deque([task_id])
        while queue:
            node = queue.popleft()
            for dependent in self._reverse.get(node, []):
                if dependent not in visited:
                    visited.add(dependent)
                    queue.append(dependent)
        return visited

    def get_critical_path(self, estimates: dict[str, float] | None = None) -> list[str]:
        """Get the critical path (longest path through the DAG).

        Args:
            estimates: Dict of task_id -> estimated duration. Defaults to 1.0 each.
        """
        estimates = estimates or {}
        order = self.topological_sort()

        # Longest path using DP
        dist: dict[str, float] = {n: 0.0 for n in self._all_nodes}
        pred: dict[str, str | None] = {n: None for n in self._all_nodes}

        for node in order:
            node_time = estimates.get(node, 1.0)
            for dependent in self._reverse.get(node, []):
                new_dist = dist[node] + node_time
                if new_dist > dist[dependent]:
                    dist[dependent] = new_dist
                    pred[dependent] = node

        # Find the end of the critical path (include each node's own duration)
        if not dist:
            return []
        total_dist = {n: dist[n] + estimates.get(n, 1.0) for n in self._all_nodes}
        end_node = max(total_dist, key=total_dist.get)
        path = []
        cur: str | None = end_node
        while cur is not None:
            path.append(cur)
            cur = pred.get(cur)
        path.reverse()
        return path
