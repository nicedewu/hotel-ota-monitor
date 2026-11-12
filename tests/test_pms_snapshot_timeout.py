import subprocess
import time

import pytest

from src.pms_snapshot import PMS_STAGE_BUDGET_SECONDS, PmsSnapshotError, write_pms_snapshot


def _fake_agent_dir(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("PMS_AGENT_DIR", str(tmp_path))


def _fake_clock(monkeypatch, values):
    real = time.monotonic
    pending = list(values)
    monkeypatch.setattr(time, "monotonic", lambda: pending.pop(0) if pending else real())


def test_timeout_expired_converts_to_pms_error(tmp_path, monkeypatch):
    _fake_agent_dir(tmp_path, monkeypatch)

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(PmsSnapshotError, match="已用尽"):
        write_pms_snapshot(stay_date="2026-07-26", days=1, own_hotel_key="own_demo_a")


def test_budget_shared_between_two_calls(tmp_path, monkeypatch):
    _fake_agent_dir(tmp_path, monkeypatch)
    # 时序:deadline 起点 1000 → 第一次调用剩 90s → 第二次调用时已耗 80s,只剩 10s
    _fake_clock(monkeypatch, [1000.0, 1000.0, 1080.0])
    seen_timeouts = []

    def fake_run(command, **kwargs):
        seen_timeouts.append(round(kwargs["timeout"]))
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    write_pms_snapshot(stay_date="2026-07-26", days=1, own_hotel_key="own_demo_a", refresh_monitor_baseline=True)
    assert seen_timeouts == [PMS_STAGE_BUDGET_SECONDS, 10]


def test_budget_exhausted_skips_second_call(tmp_path, monkeypatch):
    _fake_agent_dir(tmp_path, monkeypatch)
    # 第一次调用即耗尽预算(1095 > 1000+90),第二次不再发起子进程
    _fake_clock(monkeypatch, [1000.0, 1000.0, 1095.0])
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(PmsSnapshotError, match="已用尽"):
        write_pms_snapshot(stay_date="2026-07-26", days=1, own_hotel_key="own_demo_a", refresh_monitor_baseline=True)
    assert len(calls) == 1
