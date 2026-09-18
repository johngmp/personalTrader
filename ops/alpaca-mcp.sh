#!/usr/bin/env bash
# Starts the Alpaca MCP server for Claude, using the PAPER keys from .env.
# Read-only toolsets: Claude can look at the account, prices and news, but cannot place orders.
# (All orders go through `trader`, which enforces the risk limits.)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
source .env
set +a
if [[ "${ALPACA_API_KEY:-}" == AK* ]]; then
  echo "Refusing to start: ALPACA_API_KEY looks like a LIVE key. Use paper keys (PK...)." >&2
  exit 1
fi
export ALPACA_PAPER_TRADE=true
export ALPACA_TOOLSETS="account,assets,stock-data,crypto-data,news"
exec uvx alpaca-mcp-server
