#!/usr/bin/env python3
"""portolan Stop hook：未收尾任务时阻止父 session 退出（G2/G9）。
安全边界：8s SIGALRM fail-open；异常一律放行；非 portolan 场景 <5ms 静默过。"""
import glob
import json
import os
import re
import signal
import sys
import time

STALE_SECONDS = 2 * 3600


def _allow():
    print("{}")
    sys.exit(0)


def main():
    if os.environ.get("PORTOLAN_HOOK_DISABLE") == "1":
        _allow()
    # 最快路径：无 .portolan 目录直接放行（不读 stdin 之外的任何东西前先查）
    cwd = os.getcwd()
    candidates = glob.glob(os.path.join(cwd, ".portolan", "*", "工作底稿.md")) \
        + glob.glob(os.path.join(cwd, "*", ".portolan", "*", "工作底稿.md"))
    if not candidates:
        _allow()

    data = json.load(sys.stdin)
    if data.get("stop_hook_active"):
        _allow()  # 重入放行
    stop_reason = data.get("stop_reason", "")
    if "ScheduleWakeup" in stop_reason:
        _allow()  # /loop 的定时唤醒，不阻拦

    now = time.time()
    for ws in candidates:
        try:
            with open(ws, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        m = re.search(r"状态\s*[:：]\s*(\S+)", content)
        if not m or m.group(1) != "执行中":
            continue
        if now - os.path.getmtime(ws) > STALE_SECONDS:
            continue  # stale 放行
        # 等人拍板的任务（终态=需批准）：机械确定在等用户，扣住父 session 无意义 → 放行
        sidecar = os.path.join(os.path.dirname(ws), "state.json")
        try:
            with open(sidecar, "r", encoding="utf-8") as f:
                if json.load(f).get("latest_terminal_kind") == "需批准":
                    continue
        except (OSError, json.JSONDecodeError):
            pass
        slug = os.path.basename(os.path.dirname(ws))
        print(json.dumps({
            "decision": "block",
            "reason": (f"portolan 任务「{slug}」仍在执行中（工作底稿状态=执行中）。"
                       f"请继续 orchestrate.md 编排；确认放弃请把 {os.path.dirname(ws)}/"
                       f"工作底稿.md 的\"状态\"字段改为\"已归档\"后重试退出。"),
        }, ensure_ascii=False))
        sys.exit(0)
    _allow()


if __name__ == "__main__":
    try:
        signal.signal(signal.SIGALRM, lambda *_: _allow())
        signal.alarm(8)
        main()
    except Exception:
        _allow()
