#!/usr/bin/env python3
"""守卫式 PreToolUse hook：写拦截（冻结文件保护，C3/T3）+ 无证据完成宣称拦截。
由 hooks.json 随插件自动注册（非 portolan 场景 <5ms 放行）。
写拦截判定先于完成宣称拦截执行——两者是独立职责，各自可单测。
"""
import glob
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone


WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

# 核心保底清单（layer 2 用，纯字符串比对，不依赖 sidecar）
_CORE_PROTECTED_BASENAMES = ("任务协议单.md", "rubric.md", "execution.md")

AMEND_CMD = "state-guard amend-freeze --file <path> --reason <理由> --approver human"


@dataclass
class Decision:
    """写拦截判定结果。log_event 为 None 表示不必记 hook-events.jsonl。"""
    block: bool
    reason: str | None = None
    log_event: str | None = None  # "blocked" | "passthrough_nonexec"


# ── C3：pre-tool-use 写拦截 ──────────────────────────────────────


def _target_path_for_tool(tool_name: str, tool_input: dict) -> str | None:
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path")
    return tool_input.get("file_path")


def _block_message(tool_name: str, display_path: str, kind: str) -> str:
    """三段式文案：拦了什么 / 执行者走提案通道 / 修订流程先解锁。"""
    if kind == "snapshot":
        what = (f"hook 拦下了这次 {tool_name} 写入——目标是 `.frozen/` 冻结快照"
                 f"（{display_path}）。任何阶段都不能直接改，快照只能靠 "
                 "amend-freeze 内部逻辑更新。")
    else:
        what = (f"hook 拦下了这次 {tool_name} 写入——目标是执行期冻结的核心文件"
                 f"（{display_path}）。phase=exec 期间不能直接改。")
    return (
        what + "\n"
        "发现目标或方案不对，就在 execution.md 写结构化变更提案（pending_proposal "
        "字段），挂「需批准」终态，等人工审批。\n"
        "确实要改这份冻结文件，那是停点才做的事：先让任务离开执行期（编排层 "
        "orch-set 把 phase 切出 exec），改完再用 "
        f"`{AMEND_CMD}` 把新版入账。执行期内别绕开直接写。"
    )


def _bash_write_targets(command: str) -> list[str]:
    """从 Bash 命令里挑"写指示符相邻"的候选目标。
    模式清单小而笨（重定向 / sed -i / tee / mv|cp 尾参 / chmod 尾参），
    漏网的交哈希层兜底，不追求密封。"""
    targets = []
    # 重定向：> file / >> file；排除 2>&1 这类 fd 重定向（目标以 & 开头）
    for m in re.finditer(r"(?<![<>])\d*>{1,2}(?!&)\s*([^\s;|&<>]+)", command):
        targets.append(m.group(1))
    tokens = command.split()
    for i, tok in enumerate(tokens):
        base = tok.rsplit("/", 1)[-1]
        rest = [t for t in tokens[i + 1:] if not t.startswith("-")]
        if base == "sed" and rest:
            targets.append(rest[-1])
        elif base == "tee":
            targets.extend(rest)
        elif base in ("mv", "cp") and rest:
            targets.append(rest[-1])
        elif base == "chmod":
            targets.extend(rest)
    return targets


def _protected_hit(candidates: list, frozen_basenames: set) -> str | None:
    """候选目标里第一个命中冻结清单 basename 或落在 .frozen/ 路径段下的，返回原始写法。"""
    for c in candidates:
        base = os.path.basename(c.rstrip("/"))
        if base in frozen_basenames:
            return c
        if ".frozen" in c.split("/"):
            return c
    return None


def _decide_for_path(display_path: str, real_path: str, frozen_paths, phase,
                      task_root, tool_name: str) -> Decision:
    """判定链步骤②③④：给定已解析的目标路径，判定放行/记日志/拦截。"""
    if task_root is None:
        return Decision(False)
    frozen_dir = os.path.realpath(os.path.join(task_root, ".frozen"))
    in_snapshot = real_path == frozen_dir or real_path.startswith(frozen_dir + os.sep)
    frozen_set = {os.path.realpath(p) for p in (frozen_paths or [])}
    in_frozen_list = real_path in frozen_set

    if in_snapshot:
        # .frozen/ 全阶段拦，不受 phase 影响
        return Decision(True, _block_message(tool_name, display_path, "snapshot"), "blocked")
    if not in_frozen_list:
        return Decision(False)
    if phase == "exec":
        return Decision(True, _block_message(tool_name, display_path, "core"), "blocked")
    # phase != exec：放行但记事件（非 exec 窗口的冻结文件写入）
    return Decision(False, log_event="passthrough_nonexec")


def decide(tool_name: str, tool_input: dict, frozen_paths, phase: str,
           task_root: str | None) -> Decision:
    """C3 判定链纯函数：
    ① 非 Write/Edit/MultiEdit/NotebookEdit/Bash → 放行
    ② 无活跃任务 → 放行（.frozen/ 全阶段拦是③④里对该目录的特判，不在此步豁免）
    ③ 目标不在 frozen_paths ∪ {.frozen/ 下所有路径} → 放行
    ④ block（phase==exec 命中 frozen_paths，或命中 .frozen/ 不论 phase）
    """
    if tool_name in WRITE_TOOLS:
        path = _target_path_for_tool(tool_name, tool_input)
        if not path:
            return Decision(False)
        return _decide_for_path(path, os.path.realpath(path), frozen_paths,
                                 phase, task_root, tool_name)
    if tool_name == "Bash":
        if task_root is None:
            return Decision(False)
        command = tool_input.get("command", "")
        frozen_basenames = {os.path.basename(p) for p in (frozen_paths or [])}
        hit = _protected_hit(_bash_write_targets(command), frozen_basenames)
        if hit is None:
            return Decision(False)
        candidate = hit if os.path.isabs(hit) else os.path.join(task_root, hit)
        return _decide_for_path(hit, os.path.realpath(candidate), frozen_paths,
                                 phase, task_root, tool_name)
    return Decision(False)


def _core_fallback_decide(tool_name: str, tool_input: dict,
                           task_root: str | None) -> Decision:
    """layer 2 核心保底：纯字符串操作，不碰 sidecar/realpath。
    只覆盖 Write/Edit/MultiEdit/NotebookEdit（Bash 高置信通道属 layer 3，
    保底层不做，漏拦交哈希层）。"""
    if task_root is None or tool_name not in WRITE_TOOLS:
        return Decision(False)
    path = _target_path_for_tool(tool_name, tool_input)
    if not path:
        return Decision(False)
    basename = path.rsplit("/", 1)[-1]
    is_core = basename in _CORE_PROTECTED_BASENAMES
    is_snapshot = "/.frozen/" in path or path.rstrip("/").endswith("/.frozen")
    if not (is_core or is_snapshot):
        return Decision(False)
    task_root_prefix = task_root.rstrip("/") + "/"
    if not path.startswith(task_root_prefix):
        return Decision(False)
    return Decision(True, _block_message(tool_name, path,
                                          "snapshot" if is_snapshot else "core"),
                    "blocked")


def _read_sidecar_strict(task_dir: str):
    """读 state.json：不存在返回 None（正常情况，例如旧 schema/未 freeze）；
    JSON 损坏则原样抛出，让上层降级到核心保底。"""
    path = os.path.join(task_dir, "state.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _display_path_for_log(tool_name: str, tool_input: dict) -> str:
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path", "")
    if tool_name == "Bash":
        return tool_input.get("command", "")
    return tool_input.get("file_path", "")


def _append_hook_event(task_root: str, tool_name: str, tool_input: dict,
                        decision: Decision, error: str = None) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "path": _display_path_for_log(tool_name, tool_input),
        "decision": "block" if decision.block else "pass",
        "event": decision.log_event,
    }
    if error:
        entry["error"] = error
    try:
        with open(os.path.join(task_root, "hook-events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _write_guard_decide(tool_name: str, tool_input: dict,
                         task_root: str | None) -> Decision:
    """三层降级的编排：layer2 核心保底先算好垫底，layer3 完整检查异常就回落它。"""
    fallback = _core_fallback_decide(tool_name, tool_input, task_root)
    error = None
    try:
        sidecar = _read_sidecar_strict(task_root) if task_root else None
        frozen_paths = (sidecar or {}).get("frozen_paths") or []
        phase = (sidecar or {}).get("orch", {}).get("phase", "exec")
        decision = decide(tool_name, tool_input, frozen_paths, phase, task_root)
    except Exception as exc:
        decision = fallback
        error = repr(exc)
    if task_root and (decision.log_event or error):
        _append_hook_event(task_root, tool_name, tool_input, decision, error)
    return decision


# ── 无证据完成宣称拦截（独立职责，写拦截之后执行）──────────────


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
    """定位这次工具调用指向的活跃任务目录。
    一个执行会话只推一个任务、一直在写它的 journal——所以目标 = 状态"执行中"
    且 journal 最近被写过的那个（唯一执行中即精确命中；多个并存取最新推进的）。
    无执行中任务返回 None（写拦截与完成宣称拦截均只管这类任务，其余放行）。"""
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
        # 无法解析 → 放行避免误伤（三层降级 layer 1）
        print(json.dumps({"continue": True}))
        sys.exit(0)

    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})
    cwd = event.get("cwd", os.getcwd())

    is_write_tool = tool_name in WRITE_TOOLS or tool_name == "Bash"
    is_completion_claim = _detect_completion_claim(tool_input)

    # 定位活跃任务：只在写拦截或完成宣称场景才需要，非 portolan 场景保持快路径
    task_root = _active_task_dir(cwd) if (is_write_tool or is_completion_claim) else None

    # 写拦截先于完成宣称拦截执行
    if is_write_tool:
        decision = _write_guard_decide(tool_name, tool_input, task_root)
        if decision.block:
            print(json.dumps({
                "continue": False,
                "decision": "block",
                "reason": decision.reason,
            }))
            sys.exit(0)

    # 非完成声明 → 放行（含非 portolan 场景）
    if not is_completion_claim:
        print(json.dumps({"continue": True}))
        sys.exit(0)

    # 完成声明 → 定位正被推进的执行中任务，查 evidence
    if task_root and not _has_fresh_evidence(task_root):
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
