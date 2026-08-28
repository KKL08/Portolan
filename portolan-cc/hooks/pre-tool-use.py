#!/usr/bin/env python3
"""守卫式 PreToolUse hook：agent 声称完成前必须过机械断言。
由 hooks.json 随插件自动注册（非 portolan 场景 <5ms 放行）。
"""
import json
import os
import sys


def _detect_completion_claim(tool_input: dict) -> bool:
    """启发式检测：这次工具调用是不是"完成声明"？
    检查 tool_input 里含 "task complete" / "task finished" / "goal satisfied" / "all done" 关键字。
    """
    text = json.dumps(tool_input, ensure_ascii=False).lower()
    return any(kw in text for kw in [
        "task complete", "task finished", "goal satisfied", "all done",
    ])


def _find_task_dir(cwd: str) -> str | None:
    """在 cwd 下找 .portolan/<slug>/ 任务目录（返回第一个，或 None）"""
    portolan_root = os.path.join(cwd, ".portolan")
    if not os.path.isdir(portolan_root):
        return None
    for name in sorted(os.listdir(portolan_root)):
        p = os.path.join(portolan_root, name)
        if os.path.isdir(p):
            return p
    return None


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

    # 非 portolan 场景 → 放行
    task_dir = _find_task_dir(cwd)
    if not task_dir:
        print(json.dumps({"continue": True}))
        sys.exit(0)

    # 疑似完成声明 → 检查 evidence
    if _detect_completion_claim(tool_input):
        if not _has_fresh_evidence(task_dir):
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
