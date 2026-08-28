#!/usr/bin/env python3
"""守卫式 PreToolUse hook：agent 声称完成前必须过机械断言。
由 hooks.json 随插件自动注册（非 portolan 场景 <5ms 放行）。
"""
import glob
import json
import os
import re
import sys


def _detect_completion_claim(tool_input: dict) -> bool:
    """启发式检测：这次工具调用是不是"完成声明"？
    tool_input 命中任一完成措辞即算（中英双语）。
    """
    text = json.dumps(tool_input, ensure_ascii=False).lower()
    return any(kw in text for kw in [
        "task complete", "task finished", "goal satisfied", "all done",
        "任务完成", "任务已完成", "已全部完成", "目标达成", "大功告成",
    ])


def _active_task_dir(cwd: str) -> str | None:
    """定位这次完成声明指向的任务目录。
    一个执行会话只推一个任务、一直在写它的 journal——所以目标 = 状态"执行中"
    且 journal 最近被写过的那个（唯一执行中即精确命中；多个并存取最新推进的）。
    无执行中任务返回 None（守卫只管执行期的过早完成，其余放行）。"""
    dirs = [d for d in
            glob.glob(os.path.join(cwd, ".portolan", "*"))
            + glob.glob(os.path.join(cwd, "*", ".portolan", "*"))
            if os.path.isdir(d)]
    active = []
    for d in dirs:
        try:
            with open(os.path.join(d, "工作底稿.md"), encoding="utf-8") as f:
                if re.search(r"状态\s*[:：]\s*执行中", f.read()):
                    active.append(d)
        except OSError:
            continue
    if not active:
        return None

    def _journal_mtime(d: str) -> float:
        j = os.path.join(d, "journal.md")
        return os.path.getmtime(j) if os.path.exists(j) else 0.0

    return max(active, key=_journal_mtime)


def _has_fresh_evidence(task_dir: str) -> bool:
    """检查 journal.md 里是否有 evidence_id。
    只检查 evidence 是否存在；freshness 检查留给 finish。
    """
    journal = os.path.join(task_dir, "journal.md")
    if not os.path.exists(journal):
        return False
    with open(journal, encoding="utf-8") as f:
        content = f.read()
    return "evidence_id" in content


def main():
    # 读 stdin 拿 hook event
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # 无法解析 → 放行避免误伤
        print(json.dumps({"continue": True}))
        sys.exit(0)

    tool_input = event.get("tool_input", {})
    cwd = event.get("cwd", os.getcwd())

    # 非完成声明 → 放行（含非 portolan 场景）
    if not _detect_completion_claim(tool_input):
        print(json.dumps({"continue": True}))
        sys.exit(0)

    # 完成声明 → 定位正被推进的执行中任务，查 evidence
    task_dir = _active_task_dir(cwd)
    if task_dir and not _has_fresh_evidence(task_dir):
        # fail-close 真阻断
        print(json.dumps({
            "continue": False,
            "decision": "block",
            "reason": "portolan guard: 声明完成前必须至少有一条 evidence 落到 journal.md 结构化小节。",
        }))
        sys.exit(0)

    # 默认放行
    print(json.dumps({"continue": True}))
    sys.exit(0)


if __name__ == "__main__":
    main()
