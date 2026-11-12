#!/usr/bin/env bash
set -euo pipefail

# 清理残留的人工登录进程；不动常驻 TigerVNC 会话，也不动定时抓价的 headless Chrome。
pkill -f "main.py login" 2>/dev/null || true
sleep 1

# 清理仍占用 profile 的「有头」Chrome；headless 属于定时抓价，不能杀。
for pid in $(pgrep -f "ctrip_manual_profile" || true); do
    if ! tr "\0" " " < "/proc/$pid/cmdline" 2>/dev/null | grep -q -- "--headless"; then
        kill "$pid" 2>/dev/null || true
    fi
done

# 历史残留（旧 noVNC 链路使用过 Xvfb :99）
pkill -f "Xvfb :99" 2>/dev/null || true

echo "验证会话残留已清理；TigerVNC 常驻会话保留运行。"
