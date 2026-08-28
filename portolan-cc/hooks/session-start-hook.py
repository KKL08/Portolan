#!/usr/bin/env python3
"""portolan SessionStart hook：未完成任务恢复注入。永不阻断。"""
import glob, json, os, re, signal, sys


def main():
    if os.environ.get("PORTOLAN_HOOK_DISABLE") == "1":
        sys.exit(0)
    cwd = os.getcwd()
    lines = []
    for ws in glob.glob(os.path.join(cwd, ".portolan", "*", "工作底稿.md")) \
            + glob.glob(os.path.join(cwd, "*", ".portolan", "*", "工作底稿.md")):
        try:
            with open(ws, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        if not re.search(r"状态\s*[:：]\s*执行中", content):
            continue
        slug = os.path.basename(os.path.dirname(ws))
        am = re.search(r"-\s*attempt\s*[:：]\s*(\d+)", content)
        lines.append(
            f"[portolan] 发现未完成任务「{slug}」（attempt {am.group(1) if am else '?'}，"
            f"目录 {os.path.dirname(ws)}）。恢复：读 references/orchestrate.md 按编排状态续跑；"
            f"放弃：把工作底稿状态改为已归档。")
    if lines:
        print("\n".join(lines))
    sys.exit(0)


if __name__ == "__main__":
    try:
        signal.signal(signal.SIGALRM, lambda *_: sys.exit(0))
        signal.alarm(8)
        json.load(sys.stdin)  # 消费输入（协议要求），内容不用
        main()
    except Exception:
        sys.exit(0)
