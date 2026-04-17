#!/usr/bin/env zsh
# install.sh — set up agent-tools
set -e

SCRIPT_DIR="${0:A:h}"
LOCAL_BIN="${LOCAL_BIN:-$HOME/.local/bin}"
DATA_DIR="${AGENT_DIR:-$HOME/.agents}"
SHELL_RC="${SHELL_RC:-$HOME/.zshrc}"

green()  { print -P "%F{green}$*%f"; }
yellow() { print -P "%F{yellow}$*%f"; }
red()    { print -P "%F{red}$*%f"; }
info()   { print "  $*"; }

echo ""
green "==> agent-tools install"
echo ""

# 1. Dependencies
echo "Checking dependencies..."
missing=()
command -v python3 &>/dev/null || missing+=(python3)
command -v tmux    &>/dev/null || missing+=(tmux)
command -v git     &>/dev/null || missing+=(git)
if (( ${#missing} )); then
    red "Missing: ${missing[*]}"
    info "Install with: brew install ${missing[*]}"
    exit 1
fi
info "python3 $(python3 --version 2>&1 | awk '{print $2}')  tmux $(tmux -V | awk '{print $2}')  git $(git --version | awk '{print $3}')"

# 2. Wrappers in $LOCAL_BIN
echo ""
echo "Installing wrappers → $LOCAL_BIN..."
mkdir -p "$LOCAL_BIN"

cat > "$LOCAL_BIN/agent-dashboard" <<WRAPPER
#!/usr/bin/env zsh
source ~/.env 2>/dev/null
exec python3 "$SCRIPT_DIR/agent-dashboard.py" "\$@"
WRAPPER
chmod +x "$LOCAL_BIN/agent-dashboard"
info "installed agent-dashboard"

ln -sf "$SCRIPT_DIR/agent-update" "$LOCAL_BIN/agent-update"
chmod +x "$SCRIPT_DIR/agent-update"
info "linked agent-update"

# 3. PATH
echo ""
echo "Checking PATH..."
if ! grep -q 'HOME/.local/bin' "$SHELL_RC" 2>/dev/null; then
    echo "\nexport PATH=\"\$HOME/.local/bin:\$PATH\"" >> "$SHELL_RC"
    info "added ~/.local/bin to $SHELL_RC"
else
    info "~/.local/bin already in $SHELL_RC"
fi

# 4. Source agent-tools.sh
echo ""
echo "Checking shell integration..."
SOURCE_LINE="source $SCRIPT_DIR/agent-tools.sh"
if ! grep -qF "agent-tools.sh" "$SHELL_RC" 2>/dev/null; then
    echo "\n# agent-tools\n$SOURCE_LINE" >> "$SHELL_RC"
    info "added source line to $SHELL_RC"
else
    info "agent-tools.sh already sourced in $SHELL_RC"
fi

# 5. Data directory
echo ""
echo "Setting up data dir..."
mkdir -p "$DATA_DIR"
AGENTS_CSV="$DATA_DIR/agents.csv"
if [[ ! -f "$AGENTS_CSV" ]]; then
    echo "Status,Task,TaskID,Session,Notes,Started,Type,Focus" > "$AGENTS_CSV"
    info "created $AGENTS_CSV"
else
    info "$AGENTS_CSV exists"
fi
AGENT_LOG_FILE="$DATA_DIR/agent-log.md"
if [[ ! -f "$AGENT_LOG_FILE" ]]; then
    echo "# Agent Log\n" > "$AGENT_LOG_FILE"
    info "created $AGENT_LOG_FILE"
else
    info "$AGENT_LOG_FILE exists"
fi

# 6. Extension scaffold
echo ""
echo "Checking extension..."
EXT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/agent-tools"
EXT_FILE="$EXT_DIR/ext.sh"
if [[ ! -f "$EXT_FILE" ]]; then
    mkdir -p "$EXT_DIR"
    cp "$SCRIPT_DIR/ext.example.sh" "$EXT_FILE"
    info "seeded $EXT_FILE from ext.example.sh"
else
    info "$EXT_FILE exists — leaving alone"
fi

echo ""
green "==> Done"
echo ""
echo "  Reload shell:  source $SHELL_RC"
echo "  Try:           agent-start   (or: agents)"
echo ""
