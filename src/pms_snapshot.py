from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from src.utils import today_str

# 单店 PMS 阶段共享预算:两次子进程调用分食同一 deadline,防止 PMS 挂起时
# 每个门店独立计时，超时由外层快照任务处理。
PMS_STAGE_BUDGET_SECONDS = 90


class PmsSnapshotError(RuntimeError):
    """A user-facing PMS snapshot failure."""


def write_pms_snapshot(*, stay_date: str | None, days: int, own_hotel_key: str, refresh_monitor_baseline: bool = False) -> None:
    if days != 1:
        raise PmsSnapshotError("pms-snapshot 定时快照只支持 --days 1")
    date_text = stay_date or today_str()
    project_dir = _pms_agent_dir()
    main_py = project_dir / "main.py"
    if not main_py.is_file():
        raise PmsSnapshotError("未找到 hotel_pms_agent，请设置 PMS_AGENT_DIR")

    python_exe = os.getenv("PMS_PYTHON", sys.executable)
    deadline = time.monotonic() + PMS_STAGE_BUDGET_SECONDS
    command = [
        python_exe,
        str(main_py),
        "db-write-own-status",
        "--date",
        date_text,
        "--days",
        "1",
        "--hotel-key",
        own_hotel_key,
    ]
    result = _run_within_budget(command, project_dir, deadline, "PMS 快照写入")
    if result.returncode:
        message = (result.stderr or result.stdout or "PMS 快照写入失败").strip().splitlines()[-1]
        raise PmsSnapshotError(message)
    if refresh_monitor_baseline:
        baseline_command = [
            python_exe,
            str(main_py),
            "occupancy-watch",
            "--hotel-key",
            own_hotel_key,
            "--date",
            date_text,
            "--baseline-only",
        ]
        baseline_result = _run_within_budget(baseline_command, project_dir, deadline, "库存监控基线写入")
        if baseline_result.returncode:
            message = (baseline_result.stderr or baseline_result.stdout or "库存监控基线写入失败").strip().splitlines()[-1]
            raise PmsSnapshotError(message)


def _run_within_budget(command: list[str], cwd: Path, deadline: float, label: str) -> subprocess.CompletedProcess[str]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PmsSnapshotError(f"{label}超时：单店 PMS 阶段预算 {PMS_STAGE_BUDGET_SECONDS}s 已用尽")
    try:
        return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False, timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        raise PmsSnapshotError(f"{label}超时：单店 PMS 阶段预算 {PMS_STAGE_BUDGET_SECONDS}s 已用尽") from exc


def _pms_agent_dir() -> Path:
    configured = os.getenv("PMS_AGENT_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "hotel_pms_agent"
