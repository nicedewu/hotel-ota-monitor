#!/usr/bin/env bash
set -euo pipefail

exec 9>/tmp/hotel-multi-business-snapshot.lock
if ! flock -w 60 9; then
  echo "business snapshot skipped: lock busy" >&2
  exit 75
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
cd "$PROJECT_DIR"

set -a
source "$HOME/.hotel_agent/db.env"
if [ -f "$HOME/.hotel_agent/webhook.env" ]; then
  source "$HOME/.hotel_agent/webhook.env"
fi
set +a

# 按运行时间分组，使示例排程延迟启动时仍能归入对应时段。
hour=$((10#$(date +%H)))
if [ "$hour" -ge 17 ]; then
  slot=evening
elif [ "$hour" -ge 14 ]; then
  slot=afternoon
else
  slot=morning
fi
echo "business snapshot slot=$slot (hour=$hour)"

exec .venv/bin/python main.py scheduled-snapshot --all-active --days 1 --push --slot "$slot"
