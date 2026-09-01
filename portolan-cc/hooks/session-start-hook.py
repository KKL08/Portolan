#!/usr/bin/env python3
"""portolan SessionStart hook：未完成任务恢复注入；compact 源输出再入卡。
再入卡三段式（注入给意识，重读给内容——不搬运规程原文）。永不阻断。"""
import glob, json, os, re, signal, sys


def _scan_running(cwd):
    tasks = []
    for ws in glob.glob(os.path.join(cwd, ".portolan", "*", "工作底稿.md")) \
            + glob.glob(os.path.join(cwd, "*", ".portolan", "*", "工作底稿.md")):
        try:
            with open(ws, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        if not re.search(r"状态\s*[:：]\s*执行中", content):
            continue
        task_dir = os.path.dirname(ws)
        info = {"slug": os.path.basename(task_dir), "dir": task_dir,
                "attempt": "?", "tier": "?", "terminal": "无"}
        am = re.search(r"-\s*attempt\s*[:：]\s*(\d+)", content)
        if am:
            info["attempt"] = am.group(1)
        try:
            with open(os.path.join(task_dir, "state.json"),
                      encoding="utf-8") as f:
                sc = json.load(f)
            orch = sc.get("orch", {})
            info["attempt"] = str(orch.get("attempt", info["attempt"]))
            info["tier"] = str(orch.get("tolerance_tier", "?"))
            info["terminal"] = str(sc.get("latest_terminal_kind") or "无")
        except (OSError, json.JSONDecodeError):
            pass
        tasks.append(info)
    return tasks


def _reentry_card(t):
    return (
        f"[portolan·压缩再入] 任务「{t['slug']}」执行中：attempt={t['attempt']}, "
        f"上次终态={t['terminal']}, 档位={t['tier']}, 目录={t['dir']}。\n"
        "先完整重读 references/orchestrate.md（循环骨架+动作表+铁律），"
        f"然后编排动作一律跑 `state-guard orch-step --task-dir {t['dir']} "
        "--context resume` 并照返回的 action 执行——不凭记忆分支。\n"
        "文件地图：references/orchestrate.md=编排规程；references/continue.md="
        "停点分流；任务目录内 任务协议单.md=冻结契约（只读）、journal.md=执行"
        "记录（执行环写）、批注区.md=用户指令入口、工作底稿.md=状态投影（只读）。")


def main(source):
    if os.environ.get("PORTOLAN_HOOK_DISABLE") == "1":
        sys.exit(0)
    tasks = _scan_running(os.getcwd())
    if not tasks:
        sys.exit(0)
    if source == "compact":
        print("\n".join(_reentry_card(t) for t in tasks))
    else:
        print("\n".join(
            f"[portolan] 发现未完成任务「{t['slug']}」（attempt {t['attempt']}，"
            f"目录 {t['dir']}）。恢复：读 references/orchestrate.md 按编排状态续跑；"
            f"放弃：把工作底稿状态改为已归档。" for t in tasks))
    sys.exit(0)


if __name__ == "__main__":
    try:
        signal.signal(signal.SIGALRM, lambda *_: sys.exit(0))
        signal.alarm(8)
        try:
            payload = json.load(sys.stdin)
        except Exception:
            payload = {}
        main(str(payload.get("source", "")))
    except Exception:
        sys.exit(0)
