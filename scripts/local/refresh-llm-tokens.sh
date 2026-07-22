#!/bin/zsh
# Pushes the local ccusage token total (Claude Code + Codex) to the profile
# repo's PLATFORM_LLM_TOKENS Actions variable, which the render workflow
# injects into the LLM-tokens ledger row. Only the raw token COUNT leaves this
# machine — cost figures are never published.
#
# Installed as a LaunchAgent (see com.anoop-kondepudi.profile-llm-tokens.plist).
set -euo pipefail
export PATH="/opt/homebrew/opt/node@24/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

TOKENS=$(npx -y ccusage@latest --json 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['totals']['totalTokens'])")

[[ "$TOKENS" =~ ^[0-9]+$ ]] || { echo "ccusage returned no total"; exit 1; }

gh variable set PLATFORM_LLM_TOKENS \
  --repo Anoop-Kondepudi/Anoop-Kondepudi \
  --body "$TOKENS"
echo "PLATFORM_LLM_TOKENS -> $TOKENS"
