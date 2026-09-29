#!/usr/bin/env python3
"""portolan SubagentStop hook：仅在执行者（portolan:execution-loop）停止时运行，
对每个执行中任务做两件机械动作，不向任何上下文输出文字：
- round 锚点确定性哈希校验，检出信号落 pending_signal + journal 留痕
  （盲审由编排层下次派发前消费 pending_signal 时执行）；
- 从执行者 transcript 数压缩次数，非零则向 hook-events.jsonl 追加一条
  executor_compaction 事件（不带 decision 字段，不计入升频触发源）。"""
import importlib.util, json, os, re, signal, sys
from datetime import datetime, timezone

from portolan_paths import candidate_worksheets


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


def _count_compactions(transcript_path):
    """数 transcript 里 subtype 为 compact_boundary 的记录行；正文里恰好提到该字样的
    消息不算。文件缺失或不可读返回 0。"""
    if not transcript_path:
        return 0
    n = 0
    try:
        with open(os.path.expanduser(transcript_path), encoding="utf-8") as f:
            for line in f:
                if "compact_boundary" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("subtype") == "compact_boundary":
                    n += 1
    except OSError:
        return 0
    return n


def _append_compaction_event(task_dir, count, agent_id):
    """count 是该执行者 transcript 的累计压缩次数。同一执行者被唤醒后再次停止、
    累计值没变时不重复追加；统计时按 agent_id 取最大值。"""
    path = os.path.join(task_dir, "hook-events.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (ev.get("event") == "executor_compaction"
                        and ev.get("agent_id") == agent_id and ev.get("count") == count):
                    return
    except OSError:
        pass
    entry = {"ts": datetime.now(timezone.utc).isoformat(),
             "event": "executor_compaction", "count": count, "agent_id": agent_id}
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main(event):
    if os.environ.get("PORTOLAN_HOOK_DISABLE") == "1":
        sys.exit(0)
    sg = _load_state_guard()
    compactions = None  # 找到执行中任务后才读 transcript，且只读一次
    for ws in candidate_worksheets():
        try:
            with open(ws, "r", encoding="utf-8") as f:
                wcontent = f.read()
        except OSError:
            continue
        if not re.search(r"状态\s*[:：]\s*执行中", wcontent):
            continue
        task_dir = os.path.dirname(ws)
        if sg is not None:
            try:
                sg.stop_hook_round_check(task_dir)
            except Exception:
                pass
        if compactions is None:
            compactions = _count_compactions(event.get("agent_transcript_path"))
        if compactions:
            _append_compaction_event(task_dir, compactions, event.get("agent_id"))
    sys.exit(0)


if __name__ == "__main__":
    try:
        signal.signal(signal.SIGALRM, lambda *_: sys.exit(0))
        signal.alarm(8)
        main(json.load(sys.stdin))
    except Exception:
        sys.exit(0)
