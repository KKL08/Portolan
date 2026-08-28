#!/usr/bin/env python3
"""portolan PreCompact hook：上下文压缩前注入编排状态摘要，防止压缩后失忆。
永不阻断；无活跃任务时 <5ms 静默退出。"""
import glob
import json
import os
import re
import signal
import sys


def main():
    if os.environ.get("PORTOLAN_HOOK_DISABLE") == "1":
        sys.exit(0)
    cwd = os.getcwd()
    candidates = glob.glob(os.path.join(cwd, ".portolan", "*", "工作底稿.md")) \
        + glob.glob(os.path.join(cwd, "*", ".portolan", "*", "工作底稿.md"))
    if not candidates:
        sys.exit(0)

    try:
        json.load(sys.stdin)
    except Exception:
        pass

    lines = []
    for ws in candidates:
        try:
            with open(ws, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        m = re.search(r"状态\s*[:：]\s*(\S+)", content)
        if not m or m.group(1) not in ("执行中", "准备中"):
            continue
        task_dir = os.path.dirname(ws)
        slug = os.path.basename(task_dir)
        status = m.group(1)

        attempt = "?"
        terminal = "无"
        tier = "?"
        tk = None
        satisfied = None
        sidecar = os.path.join(task_dir, "state.json")
        if os.path.exists(sidecar):
            try:
                with open(sidecar, "r", encoding="utf-8") as f:
                    state = json.load(f)
                orch = state.get("orch", {})
                attempt = orch.get("attempt", "?")
                tier = orch.get("tolerance_tier", "?")
                satisfied = orch.get("checklist_baseline")
                tk = state.get("latest_terminal_kind")
                tr = state.get("latest_terminal_reason")
                if tk:
                    terminal = f"{tk}" + (f"（{tr}）" if tr else "")
            except (OSError, json.JSONDecodeError):
                pass

        # 原始目标：从任务协议单成功画像取首行，防压缩后目标缩水
        goal = ""
        try:
            with open(os.path.join(task_dir, "任务协议单.md"), "r", encoding="utf-8") as f:
                gm = re.search(r"##\s*成功画像\s*\n+([^\n#]+)", f.read())
            if gm:
                g = gm.group(1).strip()
                # 截断加省略号，避免半个 token 被硬切读成"目标就是它"
                g_short = (g[:100] + "…") if len(g) > 100 else g
                goal = f" 目标={g_short}" if g else ""
        except OSError:
            pass

        progress = f" 上次边界已过硬验收 {satisfied} 项。" if satisfied and satisfied != "0" else ""

        # 按终态给针对性下一步（无 LLM 判断，纯字面量映射）
        nexts = {
            "需批准": "读批注区待批清单，人拍板后 continue。",
            "被阻塞": "按 journal 终态复核方式重跑，或 verify-blocked 核实依赖。",
            "无进展": "evidence-delta 核实增量，按档位换思路或找人。",
            "完成": "派 finish subagent 独立重跑验收。",
        }
        nxt = nexts.get(tk, "读 references/orchestrate.md 按编排状态续跑。")

        lines.append(
            f"[portolan] 活跃任务「{slug}」：状态={status}, attempt={attempt}, "
            f"上次终态={terminal}, 档位={tier}, 目录={task_dir}。{goal}{progress}"
            f"续跑操作：{nxt}")

    if lines:
        print("\n".join(lines))
    sys.exit(0)


if __name__ == "__main__":
    try:
        signal.signal(signal.SIGALRM, lambda *_: sys.exit(0))
        signal.alarm(8)
        main()
    except Exception:
        sys.exit(0)
