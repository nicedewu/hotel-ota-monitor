#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
NOTIFY_SUCCESS=false

for arg in "$@"; do
  case "$arg" in
    --notify-success)
      NOTIFY_SUCCESS=true
      ;;
    *)
      echo "未知参数: $arg" >&2
      exit 2
      ;;
  esac
done

cd "$PROJECT_DIR"
mkdir -p logs output/latest
LOG_FILE="logs/price_monitor.log"

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') price monitor start ====="

  SCRAPE_ARGS=(--channel chromium --headless --wait-ms "${WAIT_MS:-10000}")
  if [[ -n "${DATE:-}" ]]; then
    SCRAPE_ARGS+=(--date "$DATE")
  fi
  if [[ -n "${DAYS:-}" ]]; then
    SCRAPE_ARGS+=(--days "$DAYS")
  fi

  SCRAPE_STATUS=0
  .venv/bin/python main.py scrape-all "${SCRAPE_ARGS[@]}" || SCRAPE_STATUS=$?
  if [[ "$SCRAPE_STATUS" -ne 0 ]]; then
    echo "scrape-all 退出码: $SCRAPE_STATUS；继续执行 build-hermes 以生成状态文件。"
  fi

  BUILD_STATUS=0
  .venv/bin/python main.py build-hermes || BUILD_STATUS=$?
  if [[ "$BUILD_STATUS" -ne 0 ]]; then
    echo "build-hermes 退出码: $BUILD_STATUS"
    exit "$BUILD_STATUS"
  fi

  DB_WRITE_STATUS=failed
  if (source "$HOME/.hotel_agent/db.env" && .venv/bin/python main.py db-write); then
    DB_WRITE_STATUS=success
  else
    echo "db-write 失败；继续保留 latest 结果并发送飞书提示。"
  fi

  NEED_NOTIFY="$(
    .venv/bin/python -c "import json,pathlib; p=pathlib.Path('output/latest/hermes_payload.json'); s=pathlib.Path('output/latest/run_summary.json'); payload=json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}; summary=json.loads(s.read_text(encoding='utf-8')) if s.exists() else {}; statuses=[str(h.get('status','')) for g in ('own_hotels','competitor_hotels','unknown_hotels') for h in payload.get(g,[])]; abnormal=summary.get('need_manual_login') or any(x in {'login_required','session_expired','captcha_required'} for x in statuses); print('true' if abnormal else 'false')"
  )"

  if [[ "$NEED_NOTIFY" == "true" ]]; then
    .venv/bin/python scripts/notify_feishu.py --reason "manual_verification_required" --db-write-status "$DB_WRITE_STATUS"
  elif [[ "$NOTIFY_SUCCESS" == "true" ]]; then
    .venv/bin/python scripts/notify_feishu.py --reason "price_monitor_success" --db-write-status "$DB_WRITE_STATUS"
  else
    echo "价格监控完成，无需发送飞书。"
  fi

  echo "===== $(date '+%Y-%m-%d %H:%M:%S') price monitor end ====="
} 2>&1 | tee -a "$LOG_FILE"
