#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

cd "$PROJECT_DIR"
mkdir -p logs

VNC_DISPLAY="${VNC_DISPLAY:-:1}"
VNC_PORT="${VNC_PORT:-5901}"

# 单实例锁：已有登录会话时不得干扰对方，直接退出。
LOCK_FILE="/tmp/ctrip-verify-session.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "已有登录会话进行中（锁：$LOCK_FILE），本次不启动。" >&2
    echo "如确认无人使用，请先执行 scripts/stop_verify_session.sh 清理残留后重试。" >&2
    exit 75
fi

_vnc_listening() {
    ss -tln | grep -q "127.0.0.1:${VNC_PORT} "
}

# 复用常驻 TigerVNC 会话（仅监听 localhost）；不存在时按原参数拉起。
# 本脚本不停止 TigerVNC：它是常驻会话，登录结束后保留。
if ! _vnc_listening; then
    echo "TigerVNC ${VNC_DISPLAY} 未运行，正在启动……"
    vncserver "$VNC_DISPLAY" -localhost yes -geometry 1280x800 -depth 24 \
        -xstartup "$HOME/.vnc/xstartup" >> logs/tigervnc_start.log 2>&1 9>&-
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        _vnc_listening && break
        sleep 1
    done
    if ! _vnc_listening; then
        echo "TigerVNC 启动失败，见 logs/tigervnc_start.log 与 ~/.vnc/*.log。" >&2
        exit 1
    fi
fi

echo "VNC 后端就绪：TigerVNC ${VNC_DISPLAY}（127.0.0.1:${VNC_PORT}，VncAuth）。"
echo "本机建立 SSH 隧道后用 VNC Viewer 连接 127.0.0.1::${VNC_PORT}。"
echo "（日常运维请直接使用授权运维机上的「携程重登」快捷方式。）"
echo "现在启动携程人工登录流程；登录确认成功后自动结束，TigerVNC 会话保留。"

DISPLAY="$VNC_DISPLAY" PYTHONUNBUFFERED=1 .venv/bin/python main.py login --channel chrome
