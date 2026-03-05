"""AgentForge Dashboard — Live monitoring for your agent swarm.

Run from your project directory:
    python ~/Desktop/agent-forge/dashboard.py

Opens http://localhost:8420 with live-updating task board, agent logs, and cost tracking.
"""

import json
import os
import time
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_DIR = Path.cwd()
FORGE_DIR = PROJECT_DIR / ".forge"


def get_tasks():
    tasks_dir = FORGE_DIR / "tasks"
    if not tasks_dir.exists():
        return []
    tasks = []
    for f in sorted(tasks_dir.glob("*.json")):
        try:
            tasks.append(json.loads(f.read_text()))
        except Exception:
            pass
    return tasks


def get_locks():
    locks_dir = FORGE_DIR / "locks"
    if not locks_dir.exists():
        return {}
    locks = {}
    for f in locks_dir.glob("*.lock"):
        try:
            locks[f.stem] = json.loads(f.read_text())
        except Exception:
            pass
    return locks


def get_logs(task_id=None, tail=80):
    logs_dir = FORGE_DIR / "logs"
    if not logs_dir.exists():
        return []
    logs = []
    for f in sorted(logs_dir.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True):
        if task_id and task_id not in f.name:
            continue
        try:
            content = f.read_text(errors="replace")
            lines = content.strip().split("\n")
            logs.append({
                "name": f.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                "tail": "\n".join(lines[-tail:]),
                "lines": len(lines),
            })
        except Exception:
            pass
    return logs[:20]


def get_budget():
    budget_file = FORGE_DIR / "budget" / "spending.json"
    if budget_file.exists():
        try:
            return json.loads(budget_file.read_text())
        except Exception:
            pass
    summary_file = FORGE_DIR / "budget" / "run_summary.json"
    if summary_file.exists():
        try:
            return json.loads(summary_file.read_text())
        except Exception:
            pass
    return {}


def get_health():
    health_file = FORGE_DIR / "memory" / "health_history.json"
    if health_file.exists():
        try:
            history = json.loads(health_file.read_text())
            return history[-1] if history else {}
        except Exception:
            pass
    return {}


def get_shared_context():
    ctx_file = FORGE_DIR / "context" / "SHARED.md"
    if ctx_file.exists():
        try:
            return ctx_file.read_text(errors="replace")[:5000]
        except Exception:
            pass
    return ""


def get_mail():
    mail_dir = FORGE_DIR / "mail"
    if not mail_dir.exists():
        return []
    messages = []
    for agent_dir in mail_dir.iterdir():
        if agent_dir.is_dir():
            for f in sorted(agent_dir.glob("*.md"), reverse=True)[:5]:
                try:
                    messages.append({
                        "to": agent_dir.name,
                        "file": f.name,
                        "content": f.read_text(errors="replace")[:500],
                    })
                except Exception:
                    pass
    return messages[:20]


def get_git_branches():
    try:
        import subprocess
        r = subprocess.run(
            ["git", "log", "--all", "--oneline", "-20"],
            capture_output=True, text=True, timeout=5, cwd=PROJECT_DIR,
        )
        return r.stdout.strip().split("\n") if r.returncode == 0 else []
    except Exception:
        return []


def build_api_response():
    tasks = get_tasks()
    locks = get_locks()

    # Cross-reference: if a lock exists for a task, it's active regardless of saved status
    locked_ids = set(locks.keys())
    for t in tasks:
        if t.get("id") in locked_ids and t.get("status") not in ("done", "failed"):
            t["status"] = "in_progress"
            lock_info = locks.get(t["id"], {})
            if not t.get("assigned_provider") and lock_info.get("agent"):
                t["assigned_provider"] = lock_info["agent"]

    active = [t for t in tasks if t.get("status") == "in_progress"]
    done = [t for t in tasks if t.get("status") == "done"]
    failed = [t for t in tasks if t.get("status") == "failed"]
    ready = [t for t in tasks if t.get("status") in ("ready", "backlog")]

    return {
        "project": PROJECT_DIR.name,
        "timestamp": datetime.now().isoformat(),
        "tasks": {
            "active": active,
            "done": done,
            "failed": failed,
            "ready": ready,
            "total": len(tasks),
        },
        "locks": locks,
        "budget": get_budget(),
        "health": get_health(),
        "logs": get_logs(),
        "context": get_shared_context(),
        "mail": get_mail(),
        "commits": get_git_branches(),
    }


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentForge Dashboard</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600;700&display=swap');
  * { margin: 0; padding: 0; box-sizing: border-box; }
  :root {
    --bg: #0a0a0f; --surface: #12121a; --surface2: #1a1a25;
    --border: #2a2a3a; --text: #e0e0e8; --text2: #888898;
    --amber: #f59e0b; --green: #10b981; --red: #ef4444;
    --blue: #3b82f6; --purple: #8b5cf6;
  }
  body { font-family: 'JetBrains Mono', monospace; background: var(--bg); color: var(--text); }
  .header {
    padding: 16px 24px; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center;
  }
  .header h1 { font-size: 16px; color: var(--amber); font-weight: 700; }
  .header .meta { font-size: 11px; color: var(--text2); }
  .live-dot { display: inline-block; width: 8px; height: 8px; background: var(--green);
    border-radius: 50%; margin-right: 6px; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
  .grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1px; background: var(--border); }
  .grid-full { grid-column: 1 / -1; }
  .panel { background: var(--surface); padding: 16px; min-height: 120px; }
  .panel-title { font-size: 10px; text-transform: uppercase; letter-spacing: 2px;
    color: var(--text2); margin-bottom: 12px; font-weight: 600; }
  .stat-row { display: flex; gap: 24px; flex-wrap: wrap; }
  .stat { text-align: center; }
  .stat-val { font-size: 28px; font-weight: 700; }
  .stat-label { font-size: 10px; color: var(--text2); margin-top: 2px; }
  .task-item {
    padding: 8px 10px; margin: 4px 0; border-radius: 4px;
    background: var(--surface2); font-size: 12px; display: flex;
    justify-content: space-between; align-items: center;
  }
  .task-item .title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .badge {
    font-size: 9px; padding: 2px 6px; border-radius: 3px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 1px;
  }
  .badge-active { background: var(--blue); color: white; }
  .badge-done { background: var(--green); color: white; }
  .badge-failed { background: var(--red); color: white; }
  .badge-ready { background: var(--surface2); color: var(--text2); border: 1px solid var(--border); }
  .badge-provider { background: var(--purple); color: white; margin-left: 4px; }
  .log-viewer {
    background: #000; border-radius: 4px; padding: 10px; font-size: 11px;
    max-height: 300px; overflow-y: auto; white-space: pre-wrap;
    word-break: break-all; color: #8f8; line-height: 1.5;
  }
  .log-tab {
    display: inline-block; padding: 4px 10px; font-size: 10px; cursor: pointer;
    border: 1px solid var(--border); border-bottom: none; border-radius: 4px 4px 0 0;
    color: var(--text2); background: var(--surface2); margin-right: 2px;
  }
  .log-tab.active { color: var(--amber); background: #000; }
  .context-box {
    background: var(--surface2); border-radius: 4px; padding: 10px; font-size: 11px;
    max-height: 250px; overflow-y: auto; white-space: pre-wrap; color: var(--text2);
    line-height: 1.5;
  }
  .commit-list { font-size: 11px; color: var(--text2); }
  .commit-list div { padding: 3px 0; border-bottom: 1px solid var(--border); }
  .commit-list .hash { color: var(--amber); }
  .mail-item { padding: 6px 8px; margin: 3px 0; background: var(--surface2);
    border-radius: 4px; font-size: 11px; }
  .mail-item .to { color: var(--blue); font-weight: 600; }
  .health-bar { height: 8px; border-radius: 4px; background: var(--surface2); margin-top: 8px; overflow: hidden; }
  .health-fill { height: 100%; border-radius: 4px; transition: width 0.5s; }
  .cost-item { display: flex; justify-content: space-between; padding: 4px 0;
    font-size: 12px; border-bottom: 1px solid var(--border); }
  .refresh-note { font-size: 10px; color: var(--text2); text-align: center; padding: 8px; }
</style>
</head>
<body>
<div class="header">
  <h1>⚡ AgentForge Dashboard</h1>
  <div class="meta"><span class="live-dot"></span><span id="project">—</span> · <span id="timestamp">—</span></div>
</div>
<div class="grid" id="grid">
  <!-- Stats -->
  <div class="panel grid-full">
    <div class="stat-row" id="stats"></div>
  </div>
  <!-- Active Tasks -->
  <div class="panel">
    <div class="panel-title">Active Agents</div>
    <div id="active-tasks">—</div>
  </div>
  <!-- Completed -->
  <div class="panel">
    <div class="panel-title">Completed</div>
    <div id="done-tasks">—</div>
  </div>
  <!-- Queue / Failed -->
  <div class="panel">
    <div class="panel-title">Queue / Failed</div>
    <div id="queue-tasks">—</div>
  </div>
  <!-- Logs -->
  <div class="panel grid-full">
    <div class="panel-title">Agent Logs</div>
    <div id="log-tabs"></div>
    <div class="log-viewer" id="log-viewer">Waiting for data...</div>
  </div>
  <!-- Shared Context -->
  <div class="panel">
    <div class="panel-title">Shared Context</div>
    <div class="context-box" id="context">—</div>
  </div>
  <!-- Mail -->
  <div class="panel">
    <div class="panel-title">Agent Mail</div>
    <div id="mail">—</div>
  </div>
  <!-- Git -->
  <div class="panel">
    <div class="panel-title">Recent Commits</div>
    <div class="commit-list" id="commits">—</div>
  </div>
</div>
<div class="refresh-note">Auto-refreshes every 5 seconds</div>

<script>
let currentLog = 0;
let allLogs = [];

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function renderStats(data) {
  const t = data.tasks;
  const h = data.health;
  const score = h.score || 0;
  const scoreColor = score >= 80 ? 'var(--green)' : score >= 60 ? 'var(--amber)' : 'var(--red)';
  const b = data.budget;
  const spent = b.total_spent || 0;
  const budget = b.budget_total || 0;
  const totalTokens = b.total_tokens || 0;

  function fmtTokens(n) {
    if (n >= 1000000) return (n/1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n/1000).toFixed(1) + 'K';
    return n.toString();
  }

  // Provider breakdown with tokens
  const bp = b.by_provider || {};
  const providerHtml = Object.entries(bp).map(([name, info]) =>
    `<div style="font-size:10px;color:var(--text2);padding:2px 0">
      <span style="color:var(--amber)">${name}</span>:
      ${info.tasks}t · ${fmtTokens(info.total_tokens)} tok ·
      <span style="color:var(--green)">$${info.cost.toFixed(4)}</span>
      <span style="color:var(--text2);font-size:9px">(in:${fmtTokens(info.input_tokens)} out:${fmtTokens(info.output_tokens)})</span>
    </div>`
  ).join('');

  document.getElementById('stats').innerHTML = `
    <div class="stat"><div class="stat-val" style="color:var(--blue)">${t.active.length}</div><div class="stat-label">Active</div></div>
    <div class="stat"><div class="stat-val" style="color:var(--green)">${t.done.length}</div><div class="stat-label">Done</div></div>
    <div class="stat"><div class="stat-val" style="color:var(--red)">${t.failed.length}</div><div class="stat-label">Failed</div></div>
    <div class="stat"><div class="stat-val">${t.ready.length}</div><div class="stat-label">Queued</div></div>
    <div class="stat"><div class="stat-val">${t.total}</div><div class="stat-label">Total</div></div>
    <div class="stat">
      <div class="stat-val" style="color:${scoreColor}">${score}</div>
      <div class="stat-label">Health</div>
      <div class="health-bar" style="width:120px"><div class="health-fill" style="width:${score}%;background:${scoreColor}"></div></div>
    </div>
    <div class="stat">
      <div class="stat-val" style="color:var(--amber)">${fmtTokens(totalTokens)}</div>
      <div class="stat-label">Tokens</div>
    </div>
    <div class="stat">
      <div class="stat-val" style="color:var(--green)">$${spent.toFixed(4)}</div>
      <div class="stat-label">Est. Cost${budget ? ' / $' + budget.toFixed(0) : ''}</div>
    </div>
  `;
  document.getElementById('stats').innerHTML += `<div style="width:100%;margin-top:8px">${providerHtml || '<div style="font-size:10px;color:var(--text2)">No token data yet</div>'}</div>`;
}

function renderTask(t, badge) {
  const provider = t.assigned_provider || '?';
  const cost = t.actual_cost_usd || 0;
  const costStr = cost > 0 ? `$${cost.toFixed(4)}` : '';
  const typeColors = {
    architecture: '#8b5cf6', review: '#f59e0b', backend: '#3b82f6',
    frontend: '#10b981', testing: '#06b6d4', docs: '#6b7280'
  };
  const typeColor = typeColors[t.type] || 'var(--text2)';
  return `<div class="task-item">
    <span class="badge" style="background:${typeColor};color:white;margin-right:6px;min-width:32px;text-align:center">${esc(t.type ? t.type.slice(0,4) : '?')}</span>
    <span class="title">${esc(t.title || t.id)}</span>
    ${costStr ? `<span style="color:var(--amber);font-size:10px;margin:0 4px">${costStr}</span>` : ''}
    <span class="badge badge-${badge}">${badge}</span>
    ${provider !== '?' ? `<span class="badge badge-provider">${esc(provider)}</span>` : ''}
  </div>`;
}

function renderTasks(data) {
  const t = data.tasks;
  document.getElementById('active-tasks').innerHTML =
    t.active.length ? t.active.map(x => renderTask(x, 'active')).join('') : '<div style="color:var(--text2);font-size:12px">No active agents</div>';
  document.getElementById('done-tasks').innerHTML =
    t.done.length ? t.done.slice(-10).reverse().map(x => renderTask(x, 'done')).join('') : '<div style="color:var(--text2);font-size:12px">None yet</div>';

  let queueHtml = t.failed.map(x => renderTask(x, 'failed')).join('');
  queueHtml += t.ready.slice(0, 8).map(x => renderTask(x, 'ready')).join('');
  document.getElementById('queue-tasks').innerHTML = queueHtml || '<div style="color:var(--text2);font-size:12px">Empty</div>';
}

function renderLogs(data) {
  allLogs = data.logs;
  const tabs = document.getElementById('log-tabs');
  tabs.innerHTML = allLogs.map((l, i) =>
    `<span class="log-tab ${i === currentLog ? 'active' : ''}" onclick="currentLog=${i};refresh()">${esc(l.name.replace('.log','').slice(-20))}</span>`
  ).join('');

  const viewer = document.getElementById('log-viewer');
  if (allLogs.length > 0 && allLogs[currentLog]) {
    viewer.textContent = allLogs[currentLog].tail;
    viewer.scrollTop = viewer.scrollHeight;
  } else {
    viewer.textContent = 'No logs yet...';
  }
}

function renderContext(data) {
  document.getElementById('context').textContent = data.context || 'Empty';
}

function renderMail(data) {
  document.getElementById('mail').innerHTML = data.mail.length
    ? data.mail.map(m => `<div class="mail-item"><span class="to">→ ${esc(m.to)}</span> ${esc(m.content.slice(0, 120))}</div>`).join('')
    : '<div style="color:var(--text2);font-size:12px">No messages</div>';
}

function renderCommits(data) {
  document.getElementById('commits').innerHTML = data.commits.length
    ? data.commits.map(c => {
        const parts = c.split(' ');
        const hash = parts[0];
        const msg = parts.slice(1).join(' ');
        return `<div><span class="hash">${esc(hash)}</span> ${esc(msg)}</div>`;
      }).join('')
    : '<div style="color:var(--text2)">No commits</div>';
}

async function refresh() {
  try {
    const r = await fetch('/api/state');
    const data = await r.json();
    document.getElementById('project').textContent = data.project;
    document.getElementById('timestamp').textContent = new Date(data.timestamp).toLocaleTimeString();
    renderStats(data);
    renderTasks(data);
    renderLogs(data);
    renderContext(data);
    renderMail(data);
    renderCommits(data);
  } catch(e) {
    document.getElementById('log-viewer').textContent = 'Connection lost. Retrying...';
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            data = build_api_response()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        elif parsed.path == "/" or parsed.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress access logs


def main():
    port = 8420
    if not FORGE_DIR.exists():
        print(f"⚠ No .forge/ directory found in {PROJECT_DIR}")
        print(f"  Run 'python forge.py init' first, or cd into your project directory.")
        return

    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"⚡ AgentForge Dashboard")
    print(f"   Project: {PROJECT_DIR.name}")
    print(f"   URL:     http://localhost:{port}")
    print(f"   Ctrl+C to stop\n")

    try:
        import webbrowser
        webbrowser.open(f"http://localhost:{port}")
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⚡ Dashboard stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
