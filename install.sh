#!/usr/bin/env bash
# AgentForge installer — run from anywhere
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

FORGE_HOME="${FORGE_HOME:-$HOME/.agent-forge}"

echo -e "${CYAN}${BOLD}"
echo "  ⚡ AgentForge Installer"
echo "  Provider-agnostic multi-agent orchestration"
echo -e "${NC}"

# ─── Step 1: Clone or update ───────────────────────────────────
if [ -d "$FORGE_HOME" ]; then
    echo -e "${BLUE}Updating existing installation...${NC}"
    cd "$FORGE_HOME" && git pull --quiet 2>/dev/null || true
else
    echo -e "${BLUE}Installing to ${FORGE_HOME}...${NC}"
    git clone https://github.com/JordinDC7/AgentForge.git "$FORGE_HOME" 2>/dev/null || {
        # If no remote repo yet, just copy current directory
        echo -e "${YELLOW}No remote repo found. Copying local files...${NC}"
        mkdir -p "$FORGE_HOME"
        cp -r "$(dirname "$0")"/* "$FORGE_HOME"/ 2>/dev/null || true
    }
fi

# ─── Step 2: Create the `forge` command ────────────────────────
FORGE_BIN="$HOME/.local/bin/forge"
mkdir -p "$HOME/.local/bin"

cat > "$FORGE_BIN" << LAUNCHER
#!/usr/bin/env bash
# AgentForge launcher — routes to the Python CLI
export FORGE_HOME="${FORGE_HOME}"
export PYTHONPATH="\${FORGE_HOME}:\${PYTHONPATH:-}"
exec python3 "\${FORGE_HOME}/forge.py" "\$@"
LAUNCHER
chmod +x "$FORGE_BIN"

# ─── Step 3: Check PATH ───────────────────────────────────────
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo ""
    echo -e "${YELLOW}⚠ Add this to your shell profile (.bashrc / .zshrc):${NC}"
    echo ""
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
    # Try to add it automatically
    for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
        if [ -f "$rc" ]; then
            if ! grep -q '.local/bin' "$rc" 2>/dev/null; then
                echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc"
                echo -e "  ${GREEN}Added to $(basename $rc) automatically${NC}"
            fi
            break
        fi
    done
fi

# ─── Step 4: Check Python ─────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}❌ Python 3.11+ required. Install it first.${NC}"
    exit 1
fi

PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo -e "  Python:  ${GREEN}${PYVER}${NC}"

# ─── Step 5: Install Python dependencies (minimal) ────────────
pip3 install pyyaml rich gitpython --quiet --break-system-packages 2>/dev/null || \
pip3 install pyyaml rich gitpython --quiet 2>/dev/null || true

# ─── Step 6: Detect available AI providers ─────────────────────
echo ""
echo -e "${BOLD}Detected AI Providers:${NC}"

check_provider() {
    local name="$1" cmd="$2" install="$3" cost="$4"
    if command -v "$cmd" &>/dev/null; then
        echo -e "  ${GREEN}✅ ${name}${NC} (${cost})"
        return 0
    else
        echo -e "  ${RED}❌ ${name}${NC} — install: ${install}"
        return 1
    fi
}

PROVIDERS_FOUND=0
check_provider "Gemini CLI"  "gemini"   "npx @google/gemini-cli (FREE!)"     "FREE" && ((PROVIDERS_FOUND++)) || true
check_provider "Codex CLI"   "codex"    "npm i -g @openai/codex"             "\$20/mo" && ((PROVIDERS_FOUND++)) || true
check_provider "Claude Code" "claude"   "npm i -g @anthropic-ai/claude-code" "\$20-200/mo" && ((PROVIDERS_FOUND++)) || true
check_provider "Aider"       "aider"    "pip install aider-chat"             "API costs" && ((PROVIDERS_FOUND++)) || true
check_provider "OpenCode"    "opencode" "go install github.com/opencode-ai/opencode@latest" "free" && ((PROVIDERS_FOUND++)) || true
check_provider "Ollama"      "ollama"   "https://ollama.com/download"        "local/free" && ((PROVIDERS_FOUND++)) || true

echo ""
if [ "$PROVIDERS_FOUND" -eq 0 ]; then
    echo -e "${YELLOW}⚠ No AI providers found. Install at least one:${NC}"
    echo -e "  ${BOLD}Recommended start: Gemini CLI (completely free)${NC}"
    echo "  npx @google/gemini-cli"
    echo ""
    echo "  Then add Claude Code or Codex for harder tasks."
fi

# ─── Done ──────────────────────────────────────────────────────
echo -e "${GREEN}${BOLD}✅ AgentForge installed!${NC}"
echo ""
echo -e "  ${CYAN}Quick start:${NC}"
echo ""
echo "  # New project from scratch:"
echo "  forge new my-awesome-app"
echo "  cd my-awesome-app"
echo "  forge plan 'Build a REST API with user auth'"
echo "  forge run --budget 10"
echo ""
echo "  # Add to existing project:"
echo "  cd your-project"
echo "  forge init"
echo "  forge plan 'Add search functionality'"
echo "  forge run --budget 5"
echo ""
echo -e "  ${CYAN}Docs: forge help${NC}"
