#!/usr/bin/env python3
"""portolan SubagentStop hook：subagent 返回但无新终态时告警（只告警不阻断）；
并在 round 锚点做确定性哈希校验，检出信号则落 pending_signal + journal 留痕
（纯代码，不起 LLM——盲审由编排层下次派发前消费 pending_signal 时执行）。"""
import glob, importlib.util, json, os, re, signal, sys


def _read_sidecar(task_dir):
    """读 state.json（sidecar 是终态与水位的权威，与 stop-hook 同源）。"""
    try:
        with open(os.path.join(task_dir, "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _load_state_guard():
    """加载 bin/state-guard.py（确定性判定集中处）。加载失败返回 None（容错放行）。"""
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bin", "state-guard.py")
        spec = importlib.util.spec_from_file_location("state_guard", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def main():
    if os.environ.get("PORTOLAN_HOOK_DISABLE") == "1":
        sys.exit(0)
    cwd = os.getcwd()
    sg = _load_state_guard()
    for ws in glob.glob(os.path.join(cwd, ".portolan", "*", "工作底稿.md")) \
            + glob.glob(os.path.join(cwd, "*", ".portolan", "*", "工作底稿.md")):
        try:
            with open(ws, "r", encoding="utf-8") as f:
                wcontent = f.read()
        except OSError:
            continue
        if not re.search(r"状态\s*[:：]\s*执行中", wcontent):
            continue
        task_dir = os.path.dirname(ws)
        # round 锚点确定性哈希校验：检出信号写 pending_signal + journal 留痕
        if sg is not None:
            try:
                sig = sg.stop_hook_round_check(task_dir)
                if sig:
                    slug = os.path.basename(task_dir)
                    print(f"[portolan] 信号：任务「{slug}」round 校验检出哈希不匹配"
                          f"（{sig.get('count')} 项），已写 pending_signal，"
                          f"编排层下次派发前须先走 triage 分诊。")
            except Exception:
                pass
        # 终态与水位都取自 sidecar（权威）
        sidecar = _read_sidecar(task_dir)
        latest = sidecar.get("latest_terminal_kind") if sidecar else None
        watermark = (sidecar.get("orch", {}).get("terminal_watermark", "")
                     if sidecar else "")
        watermark_state = watermark.split("@")[0] if watermark else ""
        if latest is None or (watermark_state and latest == watermark_state):
            slug = os.path.basename(task_dir)
            print(f"[portolan] 警告：任务「{slug}」的 subagent 已返回但 journal "
                  f"未声明命名终态（或与水位相同）。编排层应先 SendMessage 恢复，"
                  f"失败再冷启动重派（orchestrate.md「无终态返回」节）。")
    sys.exit(0)


if __name__ == "__main__":
    try:
        signal.signal(signal.SIGALRM, lambda *_: sys.exit(0))
        signal.alarm(8)
        json.load(sys.stdin)
        main()
    except Exception:
        sys.exit(0)
