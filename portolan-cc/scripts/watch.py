#!/usr/bin/env python3
"""watch: portolan 会话自动拉起 daemon（opt-in）。
观察工作底稿状态字段，subprocess 拉起下一 continue/finish 会话。
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

# 引入 state-guard 用于 SINGLE_FLIGHT lock 与终态解析
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATE_GUARD = os.path.join(_PLUGIN_ROOT, "bin", "state-guard.py")
import importlib.util
_spec = importlib.util.spec_from_file_location("state_guard", _STATE_GUARD)
state_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(state_guard)


_child_proc = None  # Popen handle for in-flight CC subprocess


class WatchAlreadyRunningError(RuntimeError):
    pass


def _pidfile_path(task_dir: str) -> str:
    return os.path.join(task_dir, ".watch.pid")


def _write_pidfile(task_dir: str) -> None:
    pf = _pidfile_path(task_dir)
    try:
        fd = os.open(pf, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        # File exists — check if the owning process is still alive
        _check_pidfile(task_dir)  # raises WatchAlreadyRunningError if alive
        # Dead process — clean up and retry
        os.remove(pf)
        fd = os.open(pf, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)


def _check_pidfile(task_dir: str) -> None:
    """已有 watch 在跑（pid 活着）→ raise。"""
    pf = _pidfile_path(task_dir)
    if not os.path.exists(pf):
        return
    try:
        with open(pf) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, 0)
            raise WatchAlreadyRunningError(f"watch already running (pid={pid})")
        except OSError:
            os.remove(pf)
    except (ValueError, OSError):
        os.remove(pf)


def poll_status(task_dir: str) -> str:
    """读工作底稿"状态"字段。返回 '准备中'/'执行中'/'已收尾'/'已归档' 之一。"""
    ws = os.path.join(task_dir, "工作底稿.md")
    if not os.path.exists(ws):
        return "未知"
    with open(ws, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(r"状态\s*[:：]\s*(\S+)", content)
    return m.group(1) if m else "未知"


def poll_terminal_state(task_dir: str) -> str | None:
    """最新终态。委托 state_guard.parse_latest_terminal（sidecar 优先 + journal
    fallback），与 state-guard、hook 共用同一解析。"""
    journal = os.path.join(task_dir, "journal.md")
    if not os.path.exists(journal):
        return None
    with open(journal, "r", encoding="utf-8") as f:
        content = f.read()
    term = state_guard.parse_latest_terminal(content, task_dir=task_dir)
    return term["state"] if term else None


def launch_next_session(task_dir: str, mode: str) -> subprocess.CompletedProcess:
    """subprocess 拉起 CC 会话跑 /portolan:<mode>。
    mode ∈ {'continue','finish'}。
    """
    global _child_proc
    prompt = f"/portolan:{mode} --task-dir {task_dir}"
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _child_proc = proc
    try:
        stdout, stderr = proc.communicate(timeout=3600)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    finally:
        _child_proc = None
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def classify_result(result: subprocess.CompletedProcess) -> str:
    """精细分类拉起结果。
    返回：'success' / 'error' / 'quota' / 'blocked_on_user' / 'user_stop'
    """
    if result.returncode < 0:  # killed by signal
        if result.returncode == -signal.SIGTERM:
            return "user_stop"
        return "error"
    if result.returncode != 0:
        stderr = (result.stderr or "").lower()
        if "quota" in stderr or "rate limit" in stderr or "429" in stderr:
            return "quota"
        return "error"
    # CC --output-format json 顶层: {"type":"result","subtype":"success","is_error":false,"result":"..."}
    try:
        data = json.loads(result.stdout or "{}")
        is_error = data.get("is_error", False)
        subtype = data.get("subtype", "unknown")
        if is_error:
            return "error"
        if subtype == "success":
            return "success"
        if subtype in ("blocked_on_user", "waiting_user"):
            return "blocked_on_user"
        return "error"
    except json.JSONDecodeError:
        return "error"


def watch(task_dir: str, max_blocks: int = 2, poll_interval: int = 3) -> None:
    """常驻 daemon 主循环。"""
    _check_pidfile(task_dir)
    _write_pidfile(task_dir)

    def _sigterm_handler(signum, frame):
        global _child_proc
        if _child_proc:
            try:
                _child_proc.terminate()
            except OSError:
                pass
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    block_count = 0
    last_processed_terminal = None  # 水位标记：避免对同一终态重复拉起
    try:
        while True:
            status = poll_status(task_dir)
            if status in ("已收尾", "已归档"):
                print(f"[watch] task 已终态 ({status})，退出", file=sys.stderr)
                break

            terminal = poll_terminal_state(task_dir)
            if terminal is None:
                time.sleep(poll_interval)
                continue

            # 同一终态已处理过 → 等 journal 更新
            journal_path = os.path.join(task_dir, "journal.md")
            journal_mtime = os.path.getmtime(journal_path) if os.path.exists(journal_path) else 0
            terminal_key = f"{terminal}@{journal_mtime}"
            if terminal_key == last_processed_terminal:
                time.sleep(poll_interval)
                continue

            # journal 已声明终态 → 抢 SINGLE_FLIGHT lock 拉起下一模式
            lockfile = os.path.join(task_dir, ".watch.lock")
            try:
                fd = state_guard.acquire_lock(lockfile, "SINGLE_FLIGHT")
            except BlockingIOError:
                # 别人在拉起，跳过
                time.sleep(poll_interval)
                continue

            try:
                mode = "finish" if terminal == "完成" else "continue"
                print(f"[watch] 拉起 /{mode}（terminal={terminal}）", file=sys.stderr)
                result = launch_next_session(task_dir, mode)
                kind = classify_result(result)
                print(f"[watch] 拉起结果：{kind}", file=sys.stderr)

                last_processed_terminal = terminal_key
                if kind == "success" or kind == "blocked_on_user":
                    block_count = 0
                elif kind == "user_stop":
                    print("[watch] 用户主动 stop，退出", file=sys.stderr)
                    break
                elif kind == "quota":
                    print("[watch] quota/网络 → 退避 60s", file=sys.stderr)
                    time.sleep(60)  # 无限退避（不计入 max_blocks）
                elif kind == "error":
                    block_count += 1
                    print(f"[watch] error → block_count={block_count}/{max_blocks}", file=sys.stderr)
                    if block_count >= max_blocks:
                        print(f"[watch] 达到 MAX_BLOCKS={max_blocks}，fail-open 退出", file=sys.stderr)
                        break
            finally:
                state_guard.release_lock(fd)

            time.sleep(poll_interval)
    finally:
        # 清理 pidfile
        pf = _pidfile_path(task_dir)
        if os.path.exists(pf):
            try:
                os.remove(pf)
            except OSError:
                pass


def stop(task_dir: str) -> None:
    """向已运行的 watch 发 SIGTERM。"""
    pf = _pidfile_path(task_dir)
    if not os.path.exists(pf):
        print("no watch running", file=sys.stderr)
        sys.exit(1)
    with open(pf) as f:
        pid = int(f.read().strip())
    os.kill(pid, signal.SIGTERM)
    print(f"stopped watch (pid={pid})", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(prog="watch")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--max-blocks", type=int, default=2)
    p.add_argument("--poll-interval", type=int, default=3)
    p.add_argument("--stop", action="store_true", help="stop already-running watch")
    args = p.parse_args()
    if args.stop:
        stop(args.task_dir)
    else:
        watch(args.task_dir, args.max_blocks, args.poll_interval)


if __name__ == "__main__":
    main()
