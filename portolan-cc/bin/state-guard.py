#!/usr/bin/env python3
"""state-guard: portolan 写入前不变式断言组件。
承担：fcntl 三策略锁、冻结哈希、schema 校验、编排状态 sidecar、判停链。
"""
import datetime
import fcntl
import hashlib
import json
import os
import re
import sys
import subprocess
import time
import uuid

import yaml  # PyYAML


class LockReentryError(RuntimeError):
    """同进程试图重入 acquire 同一 lockfile（防自锁死锁）"""


class UnsupportedFileSystemError(RuntimeError):
    """state-guard 只支持本地文件系统（拒绝 NFS）"""


# in-memory registry 防同进程重入自锁
# 格式：{lockfile_path: (fd, mode)}
_held_locks: dict[str, tuple[int, str]] = {}

STALE_LOCK_THRESHOLD_SECONDS = 30 * 60  # 30 min


def _pid_alive(pid: int) -> bool:
    """检查 pid 是否还活着（发 signal 0 探测）"""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cleanup_stale_locks(lockfile: str, stale_threshold_seconds: int = STALE_LOCK_THRESHOLD_SECONDS) -> bool:
    """检查 lockfile 记录的 pid 和时间戳。
    若 pid 已死或时间戳超过阈值，清理 lockfile 内容。
    返回是否执行了清理。
    """
    if not os.path.exists(lockfile):
        return False
    try:
        with open(lockfile, "r") as f:
            content = f.read().strip()
        if not content:
            return False
        record = json.loads(content)
        pid = record.get("pid", 0)
        ts = record.get("timestamp", 0)
        now = time.time()
        if not _pid_alive(pid) or (now - ts) > stale_threshold_seconds:
            # 持锁后再清理元数据，防止与并发 acquire_lock 的 TOCTOU race
            try:
                cleanup_fd = os.open(lockfile, os.O_RDWR)
                fcntl.flock(cleanup_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.ftruncate(cleanup_fd, 0)
                fcntl.flock(cleanup_fd, fcntl.LOCK_UN)
                os.close(cleanup_fd)
            except (BlockingIOError, OSError):
                pass  # 别人已持锁，跳过清理
            return True
    except (json.JSONDecodeError, OSError):
        # 无法解析或读取 → 尝试持锁清理
        try:
            cleanup_fd = os.open(lockfile, os.O_RDWR)
            fcntl.flock(cleanup_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(cleanup_fd, 0)
            fcntl.flock(cleanup_fd, fcntl.LOCK_UN)
            os.close(cleanup_fd)
        except (BlockingIOError, OSError):
            pass
        return True
    return False


def detect_fs_type(path: str) -> str:
    """返回文件系统类型（'apfs'/'ext4'/'nfs'/etc.）。
    Linux 用 `stat -f -c %T`（GNU stat 的 -f 表示"查询文件系统状态"）。
    macOS 用 `mount` 挂载表解析、匹配最长挂载点前缀——BSD stat(1) 的
    -f 只是"使用此格式串"标志、必须带参数，并不是"查询文件系统"开关，
    其 %T 输出的其实是 ls -F 风格的文件类型字符（如 '/'、'@'），
    不是文件系统类型，不能照搬 Linux 的写法。"""
    target = path if os.path.exists(path) else (os.path.dirname(path) or ".")
    target = os.path.realpath(target)
    try:
        if sys.platform == "darwin":
            result = subprocess.run(
                ["mount"], capture_output=True, text=True, timeout=2,
            )
            best_mountpoint, best_fstype = "", "unknown"
            for line in result.stdout.splitlines():
                # 格式："<device> on <mountpoint> (<fstype>, ...)"
                if " on " not in line or " (" not in line:
                    continue
                mountpoint, rest = line.split(" on ", 1)[1].split(" (", 1)
                if (target == mountpoint or target.startswith(mountpoint.rstrip("/") + "/")) \
                        and len(mountpoint) > len(best_mountpoint):
                    best_mountpoint = mountpoint
                    best_fstype = rest.split(",", 1)[0].strip()
            return best_fstype.lower()
        else:  # Linux
            result = subprocess.run(
                ["stat", "-f", "-c", "%T", target],
                capture_output=True, text=True, timeout=2,
            )
            return result.stdout.strip().lower()
    except Exception:
        return "unknown"


def acquire_lock(lockfile: str, mode: str) -> int:
    """获取 fcntl 锁。mode ∈ {'MUTATION','MONITOR','SINGLE_FLIGHT'}。
    RAII 语义：进程退出/kill -9 内核自动释放。
    抢锁前先清理陈旧 lock；拿到锁后写入 pid+timestamp+mode 供陈旧检测。
    """
    if mode not in ("MUTATION", "MONITOR", "SINGLE_FLIGHT"):
        raise ValueError(f"invalid mode: {mode}")

    # NFS 检测
    fs = detect_fs_type(lockfile)
    if fs in ("nfs", "nfs4"):
        raise UnsupportedFileSystemError(
            f"state-guard 不支持 NFS 场景（lockfile={lockfile}, fs={fs}）"
        )

    # 防同进程重入
    if lockfile in _held_locks:
        raise LockReentryError(
            f"同进程已持有 lockfile={lockfile} mode={_held_locks[lockfile][1]}"
        )

    # 先清理陈旧 lock 再抢
    cleanup_stale_locks(lockfile)

    # 创建 lockfile 并抢锁
    os.makedirs(os.path.dirname(lockfile) or ".", exist_ok=True)
    fd = os.open(lockfile, os.O_CREAT | os.O_RDWR, 0o644)

    if mode == "MONITOR":
        # MONITOR 用阻塞式共享锁：多读者并发，写者持锁时等待而非报错
        flock_op = fcntl.LOCK_SH
    else:  # MUTATION / SINGLE_FLIGHT
        flock_op = fcntl.LOCK_EX | fcntl.LOCK_NB  # 独占

    try:
        fcntl.flock(fd, flock_op)
    except BlockingIOError:
        os.close(fd)
        raise

    _held_locks[lockfile] = (fd, mode)

    # 写入 pid 和 timestamp 到 lockfile 用于陈旧检测
    try:
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "timestamp": time.time(), "mode": mode}).encode())
        os.lseek(fd, 0, 0)  # 归零 offset 供后续读
    except OSError:
        pass

    return fd


def release_lock(fd: int) -> None:
    """释放锁并从 registry 移除。"""
    # 从 registry 找到对应的 lockfile
    to_remove = [lf for lf, (f, _) in _held_locks.items() if f == fd]
    for lf in to_remove:
        del _held_locks[lf]
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def compute_freeze_hash(file_path: str) -> str:
    """计算文件内容的 sha256（用于冻结哈希）"""
    with open(file_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _find_section(content: str, heading: str,
                  stop_at_subsection: bool = False) -> tuple[int, int, int] | None:
    """定位 markdown '## <heading>' 节。返回 (heading_start, body_start, body_end) 或 None。

    - heading_start：'##' 所在下标
    - body_start：标题行换行之后（节正文起点）
    - body_end：节正文终点——下一个 H2 之前，或文件尾（EOF）。
      stop_at_subsection=True 时也止于下一个 H3（供"验收清单"这类含子节的场景）。

    边界语义（H2 = 行首 '##' 紧跟非 '#' 字符；空节 body_start == body_end）
    只此一处定义，全部 call site 共用。
    """
    head_re = re.compile(r"(?:^|\n)(##[ \t]*" + re.escape(heading) + r"[^\n]*\n)")
    hm = head_re.search(content)
    if not hm:
        return None
    heading_start = hm.start(1)
    body_start = hm.end(1)
    boundary = re.compile(r"\n###|\n##[^#]") if stop_at_subsection \
        else re.compile(r"\n##[^#]")
    bm = boundary.search(content, body_start)
    body_end = bm.start() if bm else len(content)
    return heading_start, body_start, body_end


def _parse_worksheet_hashes(worksheet_content: str) -> dict[str, str]:
    """从工作底稿"冻结哈希"节解析 {文件名: hash} dict。
    格式约定：`- <文件名>: `<hash>`` 每行一条。
    """
    hashes = {}
    sec = _find_section(worksheet_content, "冻结哈希")
    if not sec:
        return hashes
    section = worksheet_content[sec[1]:sec[2]]
    for line in section.splitlines():
        line = line.strip()
        # 匹配 `- <文件名>: `<hash>`` 或 `- <文件名>: <hash>`
        m2 = re.match(r"^\-\s*([^\s:]+):\s*`?([a-f0-9]{64})`?", line)
        if m2:
            hashes[m2.group(1)] = m2.group(2)
    return hashes


def verify_freeze_hashes(task_dir: str) -> list[dict]:
    """核对任务目录下的冻结哈希。返回不匹配列表。
    读取 工作底稿.md 里"冻结哈希"节的记录，与实际文件哈希对比。
    """
    worksheet_path = os.path.join(task_dir, "工作底稿.md")
    if not os.path.exists(worksheet_path):
        return [{"file": worksheet_path, "expected": None, "actual": None, "reason": "worksheet missing"}]
    with open(worksheet_path, "r", encoding="utf-8") as f:
        content = f.read()
    recorded = _parse_worksheet_hashes(content)

    mismatches = []
    # 必核文件白名单：冻结清单本身住在执行期可写的工作底稿，删掉一行哈希
    # 记录即可让 verify-freeze 对该文件视而不见。凡任务目录里实际存在的核心文件
    # （任务协议单.md / rubric.md / execution.md）都必须登记哈希——缺记录即 mismatch，
    # 堵死"文件仍在、只删哈希行"的绕过（文件不存在则无从算哈希，不强求）。
    required = [f for f in ("任务协议单.md", "rubric.md", "execution.md")
                if os.path.exists(os.path.join(task_dir, f))]
    # 评分标准基线：任务协议单声明的不可变评分标准文件（判分逻辑 + 期望答案）也必须
    # 登记冻结——被审计者不得改审计准绳。声明本身住在已冻的任务协议单里，改声明会被
    # 契约哈希抓到，形成闭链。相对路径可指向任务目录外（如 ../../tests/xxx.py）。
    contract_path = os.path.join(task_dir, "任务协议单.md")
    if os.path.exists(contract_path):
        with open(contract_path, "r", encoding="utf-8") as f:
            for std in parse_eval_standard(f.read()):
                if os.path.exists(os.path.join(task_dir, std)) and std not in required:
                    required.append(std)
    for req in required:
        if req not in recorded:
            mismatches.append({
                "file": os.path.join(task_dir, req),
                "expected": None,
                "actual": None,
                "reason": "freeze record missing (必核文件未登记冻结哈希)",
            })

    for filename, expected_hash in recorded.items():
        file_path = os.path.join(task_dir, filename)
        if not os.path.exists(file_path):
            mismatches.append({
                "file": file_path,
                "expected": expected_hash,
                "actual": None,
                "reason": "file missing",
            })
            continue
        actual = compute_freeze_hash(file_path)
        if actual != expected_hash:
            mismatches.append({
                "file": file_path,
                "expected": expected_hash,
                "actual": actual,
                "reason": "hash mismatch",
            })
    return mismatches


def update_freeze_hash(worksheet_path: str, file_path: str) -> None:
    """更新工作底稿"冻结哈希"节内 file_path 的哈希。
    若该文件哈希不存在则追加，存在则替换。

    键用"相对任务目录的路径"（任务目录 = 工作底稿所在目录）：任务目录内文件
    退化为基名，任务目录外的评分标准文件（如 ../../tests/xxx.py）
    保留相对路径，verify-freeze 才能 os.path.join 回原位核对。"""
    task_dir = os.path.dirname(worksheet_path)
    filename = os.path.relpath(file_path, task_dir)
    new_hash = compute_freeze_hash(file_path)

    with open(worksheet_path, "r", encoding="utf-8") as f:
        content = f.read()

    sec = _find_section(content, "冻结哈希")
    if not sec:
        # 节不存在则追加
        content += f"\n## 冻结哈希\n\n- {filename}: `{new_hash}`\n"
    else:
        _, body_start, body_end = sec
        section = content[body_start:body_end]
        # 替换或追加
        pattern = re.compile(rf"^\-\s*{re.escape(filename)}:\s*`?[a-f0-9]{{64}}`?\s*$", re.M)
        if pattern.search(section):
            new_section = pattern.sub(f"- {filename}: `{new_hash}`", section)
        else:
            new_section = section.rstrip() + f"\n- {filename}: `{new_hash}`\n"
        content = content[:body_start] + new_section + content[body_end:]

    with open(worksheet_path, "w", encoding="utf-8") as f:
        f.write(content)


def freeze_journal(task_dir: str) -> str:
    """冻结 journal.md：更新工作底稿哈希，并置 phase=verify（自此执行侧写入被拒）。
    返回新 hash。finish 第 0 步用。
    """
    journal_path = os.path.join(task_dir, "journal.md")
    worksheet_path = os.path.join(task_dir, "工作底稿.md")
    update_freeze_hash(worksheet_path, journal_path)
    orch_set(task_dir, "phase", "verify")
    return compute_freeze_hash(journal_path)


# evidence contract 10 字段
EVIDENCE_REQUIRED_FIELDS = [
    "evidence_id", "producer_attempt_id", "kind", "locator",
    "content_hash", "observed_at", "fresh_until",
    "command_or_method", "exit_status_or_verdict", "verifier_id",
]
EVIDENCE_ALLOWED_KINDS = ["shell", "grep"]

# 当前进展 8 项
PROGRESS8_REQUIRED_FIELDS = [
    "goal_revision", "work_items", "last_decision",
    "active_approvals", "verified_facts", "unknown_outcomes",
    "next_action", "needs_reverify",
]


def validate_evidence_schema(yaml_content: str) -> list[str]:
    """校验 evidence 是否符合 10 字段 schema。返回错误消息列表。"""
    errors = []
    try:
        data = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        return [f"YAML parse error: {e}"]

    if not isinstance(data, dict):
        return ["evidence must be a YAML mapping"]

    for field in EVIDENCE_REQUIRED_FIELDS:
        if field not in data:
            errors.append(f"missing required field: {field}")

    if "kind" in data and data["kind"] not in EVIDENCE_ALLOWED_KINDS:
        errors.append(
            f"kind must be one of {EVIDENCE_ALLOWED_KINDS}, got: {data['kind']}"
        )

    # verifier_id 格式校验（finish-session-{uuid} / subagent-{uuid} / user:{git_username}）
    if "verifier_id" in data:
        vid = str(data["verifier_id"])
        if not (vid.startswith("finish-session-") or vid.startswith("subagent-") or vid.startswith("user:")):
            errors.append(
                f"verifier_id must start with 'finish-session-' / 'subagent-' / 'user:', got: {vid}"
            )

    return errors


def validate_progress8_schema(yaml_content: str) -> list[str]:
    """校验当前进展 8 项 schema。"""
    errors = []
    try:
        data = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        return [f"YAML parse error: {e}"]

    if not isinstance(data, dict):
        return ["current progress must be a YAML mapping"]

    for field in PROGRESS8_REQUIRED_FIELDS:
        if field not in data:
            errors.append(f"missing required field: {field}")

    return errors


# ── 判停解析核心 ──────────────────────────────────────

TERMINAL_STATES = ("完成", "无事可做", "被阻塞", "需批准", "无进展")
_TERMINAL_RE = re.compile(r"终态\s*[:：]\s*\**(" + "|".join(TERMINAL_STATES) + r")\**")


def parse_latest_terminal(journal_content: str,
                          task_dir: str | None = None) -> dict | None:
    """终态声明节的最新一条完整记录。返回 {"state","entry"} 或 None。

    若 task_dir 给且 sidecar 有 latest_terminal_kind → 优先返回 sidecar 合成记录
    （sidecar 是权威）；否则按 - 日期/轮次 锚点切分 journal 记录（fallback，
    兼容手写终态）。"""
    # 优先路径：sidecar
    if task_dir is not None:
        sidecar = _read_state_json(task_dir)
        if sidecar and sidecar.get("latest_terminal_kind"):
            state = sidecar["latest_terminal_kind"]
            ts = sidecar.get("latest_terminal_ts", "")
            attempt = sidecar.get("orch", {}).get("attempt", "0")
            reason = sidecar.get("latest_terminal_reason", "")
            recheck_cmd = sidecar.get("latest_recheck_cmd") or ""
            recheck_line = (f"复核方式：`{recheck_cmd}`" if recheck_cmd
                            else "复核方式：无")
            entry = (f"- 日期/轮次：{ts} / 第 {attempt} 轮\n"
                     f"- 终态：{state}\n"
                     f"- 依据：{reason}\n"
                     f"- {recheck_line}\n"
                     f"- 下一步建议：\n")
            return {"state": state, "entry": entry}

    # Fallback：markdown 解析（兼容手写终态）
    sec = _find_section(journal_content, "终态声明")
    if not sec:
        return None
    section = journal_content[sec[0]:sec[2]]
    records = re.split(r"\n(?=-\s*日期/轮次\s*[:：])", section)
    last = None
    for rec in records:
        tm = _TERMINAL_RE.search(rec)
        if tm:
            last = {"state": tm.group(1), "entry": rec}
    return last


def extract_recheck_command(entry: str) -> str | None:
    """终态条目里"复核方式"之后的第一条反引号命令。"""
    m = re.search(r"复核方式[^`]*`([^`]+)`", entry, re.DOTALL)
    return m.group(1).strip() if m else None


def parse_evidence_entries(journal_content: str) -> list[dict]:
    """全部 ```yaml 围栏块里的 evidence 条目（dict 列表）。"""
    entries = []
    for block in re.findall(r"```yaml\n(.*?)```", journal_content, re.DOTALL):
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and isinstance(data.get("evidence"), list):
            entries.extend(e for e in data["evidence"] if isinstance(e, dict))
        elif isinstance(data, list):
            entries.extend(e for e in data if isinstance(e, dict) and "evidence_id" in e)
    return entries


def evidence_snapshot(journal_content: str) -> tuple[int, str]:
    """(evidence 条数, 排序 (id,hash) 对的 sha256 指纹)。"""
    pairs = sorted(
        (str(e.get("evidence_id", "")), str(e.get("content_hash", "")))
        for e in parse_evidence_entries(journal_content)
    )
    fp = hashlib.sha256(json.dumps(pairs, ensure_ascii=False).encode()).hexdigest()
    return len(pairs), fp


def parse_checklist_commands(contract_content: str) -> list[str]:
    """验收清单节（截止到反向断言子节或下一 H2）内所有"- 验收命令：`X`"行的命令。
    只提取结构化字段"- 验收命令：`X`"，避免把行内代码（如 `Args`）误当命令。"""
    sec = _find_section(contract_content, "验收清单", stop_at_subsection=True)
    if not sec:
        return []
    body = re.sub(r"\{\{.*?\}\}", "", contract_content[sec[1]:sec[2]], flags=re.DOTALL)
    return [c.strip() for c in re.findall(
        r"-\s*验收命令\s*[:：]\s*`([^`]+)`", body)]


def parse_eval_standard(contract_content: str) -> list[str]:
    """任务协议单"评分标准基线"节声明的不可变评分标准文件（判分逻辑 + 期望答案）。
    路径相对任务目录。执行者不得改；dispatch 冻结、verify-freeze/finish 核对。
    冻"评分标准"而非"整个工作区"——补代码类任务工作文件本就该变，只标准不可动。"""
    sec = _find_section(contract_content, "评分标准基线")
    if not sec:
        return []
    body = re.sub(r"\{\{.*?\}\}", "", contract_content[sec[1]:sec[2]], flags=re.DOTALL)
    return [p.strip() for p in re.findall(
        r"-\s*标准文件\s*[:：]\s*`([^`]+)`", body)]


# ── 编排状态（单一状态实体：工作底稿 ## 编排状态 节）──────

# 协议标识符单一来源：编排状态字段名权威清单
ORCH_FIELDS = ("attempt", "terminal_watermark", "evidence_count",
               "evidence_fingerprint", "zero_flip_streak", "rethink_count",
               "trigger_count", "checklist_baseline", "tolerance_tier",
               "phase")
_ORCH_DEFAULTS = {f: "0" for f in ORCH_FIELDS}
_ORCH_DEFAULTS.update({"terminal_watermark": "", "evidence_fingerprint": "",
                       "tolerance_tier": "", "phase": "exec"})
# phase ∈ {exec, verify}：freeze-journal 进 verify 后执行侧写入（run-check/
# declare-terminal）一律被拒；continue 重派前用 orch-set 置回 exec。


# ── 编排状态 sidecar（.portolan/<slug>/state.json）─────────────
# Sidecar 是编排状态的权威。工作底稿 ## 编排状态 节由后续步骤（write_projection）
# 顺手投影，仅供人可读；执行者/用户编辑投影不改变状态（下次 orch-set 覆盖）。

STATE_JSON_NAME = "state.json"
STATE_JSON_SCHEMA_VERSION = "1"


def _state_json_path(task_dir: str) -> str:
    return os.path.join(task_dir, STATE_JSON_NAME)


def _read_state_json(task_dir: str) -> dict | None:
    """读 sidecar。不存在或损坏返回 None（触发迁移/重建路径，不抛）。"""
    try:
        with open(_state_json_path(task_dir), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_state_json_atomic(task_dir: str, state: dict) -> None:
    """原子写：同目录临时文件 + fsync + os.replace。防半写、跨 fs 安全。"""
    path = _state_json_path(task_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _ws_path(task_dir: str) -> str:
    return os.path.join(task_dir, "工作底稿.md")


def write_projection(ws_path: str, state: dict) -> None:
    """把当前编排状态投影到工作底稿 ## 编排状态 节（人可读镜像）。
    投影只读，编辑不改变状态。sidecar 才是权威。
    工作底稿不存在则静默跳过（新任务尚未落盘场景）。"""
    try:
        with open(ws_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return
    section_body = "".join(f"- {k}: {state.get(k, '')}\n" for k in ORCH_FIELDS)
    section = ("## 编排状态\n"
               "> 本节由 state-guard 投影，只读；编辑不改变状态。"
               "sidecar `state.json` 才是权威。\n\n"
               + section_body)
    sec = _find_section(content, "编排状态")
    if sec:
        content = content[:sec[0]] + section.rstrip() + content[sec[2]:]
    else:
        content += "\n" + section
    with open(ws_path, "w", encoding="utf-8") as f:
        f.write(content)


def orch_get(task_dir: str) -> dict:
    """读编排状态。sidecar 为权威；不存在时从工作底稿自动迁移一次。"""
    state = dict(_ORCH_DEFAULTS)
    sidecar = _read_state_json(task_dir)
    if sidecar is not None and isinstance(sidecar.get("orch"), dict):
        for k in ORCH_FIELDS:
            if k in sidecar["orch"]:
                state[k] = str(sidecar["orch"][k])
        return state
    # Sidecar 不存在或缺 orch 键 → 从工作底稿 ## 编排状态 节抽字段
    try:
        with open(_ws_path(task_dir), "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return state  # 工作底稿也没有 → 全默认
    sec = _find_section(content, "编排状态")
    if sec:
        for line in content[sec[1]:sec[2]].splitlines():
            lm = re.match(r"^-\s*(\w+)\s*[:：]\s*(.*)$", line.strip())
            if lm and lm.group(1) in ORCH_FIELDS:
                state[lm.group(1)] = lm.group(2).strip()
    # 顺手写 sidecar 完成迁移（下次直接读 sidecar，不再回工作底稿）
    _write_state_json_atomic(task_dir, {
        "schema_version": STATE_JSON_SCHEMA_VERSION,
        "orch": dict(state),
    })
    return state


def orch_set(task_dir: str, field: str, value: str) -> None:
    """持 MUTATION 锁改写编排状态单字段。sidecar 是权威，并同步投影到工作底稿。"""
    if field not in ORCH_FIELDS:
        raise ValueError(f"未知编排状态字段: {field}（权威清单见 ORCH_FIELDS）")
    lockfile = os.path.join(task_dir, ".state.lock")
    fd = acquire_lock(lockfile, "MUTATION")
    try:
        state = orch_get(task_dir)  # 会触发自动迁移（若 sidecar 缺）
        state[field] = value
        sidecar = _read_state_json(task_dir) or {}
        sidecar["schema_version"] = STATE_JSON_SCHEMA_VERSION
        sidecar["orch"] = dict(state)
        _write_state_json_atomic(task_dir, sidecar)
        # 顺手投影到工作底稿（人可读镜像；不影响权威）
        write_projection(_ws_path(task_dir), state)
    finally:
        release_lock(fd)


def declare_terminal(task_dir: str, state: str, reason: str,
                     recheck_cmd: str | None = None) -> None:
    """工具代笔终态：追加多行 bullet 到 journal ## 终态声明 节 + 更新 sidecar。
    执行者用 CLI 声明终态，保证 journal 格式与 sidecar 一致（跟 run-check 同源）。"""
    if state not in TERMINAL_STATES:
        raise ValueError(f"未知终态: {state}（合法：{TERMINAL_STATES}）")
    lockfile = os.path.join(task_dir, ".state.lock")
    fd = acquire_lock(lockfile, "MUTATION")
    try:
        current_state = orch_get(task_dir)
        if current_state.get("phase") == "verify":
            raise ValueError("任务在验证期（phase=verify），拒绝执行侧声明终态")
        # 组装多行 bullet 记录
        now = datetime.datetime.now(datetime.timezone.utc)
        ts_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        attempt = current_state.get("attempt", "0")
        recheck_line = f"复核方式：`{recheck_cmd}`" if recheck_cmd else "复核方式：无"
        record = (
            f"\n- 日期/轮次：{ts_iso} / 第 {attempt} 轮\n"
            f"- 终态：{state}\n"
            f"- 依据：{reason}\n"
            f"- {recheck_line}\n"
            f"- 下一步建议：\n"
        )

        # 追加到 journal ## 终态声明 节末尾（下一个 ## 前 or EOF）
        journal_path = os.path.join(task_dir, "journal.md")
        try:
            with open(journal_path, "r", encoding="utf-8") as f:
                journal = f.read()
        except OSError:
            journal = "# journal\n\n## 终态声明\n"

        sec = _find_section(journal, "终态声明")
        if sec:
            insert_pos = sec[2]  # body_end：下一个 H2 前 or EOF，追加到本节末尾
            journal = journal[:insert_pos] + record + journal[insert_pos:]
        else:
            journal = journal.rstrip() + "\n\n## 终态声明\n" + record

        with open(journal_path, "w", encoding="utf-8") as f:
            f.write(journal)

        # 更新 sidecar 的终态字段（sidecar 是权威，parse_latest_terminal 会读）
        sidecar = _read_state_json(task_dir) or {}
        sidecar["schema_version"] = STATE_JSON_SCHEMA_VERSION
        sidecar["latest_terminal_kind"] = state
        sidecar["latest_terminal_ts"] = ts_iso
        sidecar["latest_terminal_reason"] = reason
        sidecar["latest_recheck_cmd"] = recheck_cmd
        _write_state_json_atomic(task_dir, sidecar)
    finally:
        release_lock(fd)


def clear_terminal(task_dir: str) -> None:
    """清除 sidecar 最新终态字段（latest_terminal_*）。编排接受终态、派下一
    attempt 前调用——防旧终态滞留：stop-hook 依 latest_terminal_kind=='需批准'
    放行父 session，若不清，重派后新执行期仍按上一轮陈旧终态被误放行；watch
    的终态轮询亦不再重复命中旧终态。sidecar 缺失则无操作（幂等）。"""
    lockfile = os.path.join(task_dir, ".state.lock")
    fd = acquire_lock(lockfile, "MUTATION")
    try:
        sidecar = _read_state_json(task_dir)
        if sidecar is None:
            return
        for k in ("latest_terminal_kind", "latest_terminal_ts",
                  "latest_terminal_reason", "latest_recheck_cmd"):
            sidecar.pop(k, None)
        _write_state_json_atomic(task_dir, sidecar)
    finally:
        release_lock(fd)


def verify_blocked(task_dir: str, timeout: int = 30) -> dict:
    """停止型终态核验（仅覆盖"被阻塞"）。
    复核命令 exit 0 = 依赖是通的 = 主张被驳斥（refuted）。"""
    try:
        with open(os.path.join(task_dir, "journal.md"), "r", encoding="utf-8") as f:
            journal = f.read()
    except OSError:
        return {"verdict": "unverifiable", "reason": "journal 不存在"}
    term = parse_latest_terminal(journal, task_dir=task_dir)
    if term is None or term["state"] != "被阻塞":
        return {"verdict": "not_applicable",
                "reason": f"最新终态={term['state'] if term else '无'}"}
    cmd = extract_recheck_command(term["entry"])
    if not cmd:
        return {"verdict": "unverifiable", "reason": "终态声明缺复核命令"}
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"verdict": "unverifiable", "reason": f"复核命令超时（{timeout}s）"}
    except Exception as e:
        return {"verdict": "unverifiable", "reason": f"复核命令执行失败: {e}"}
    if r.returncode == 0:
        return {"verdict": "refuted",
                "reason": f"复核命令 `{cmd}` exit 0——依赖是通的，阻塞主张不成立"}
    return {"verdict": "confirmed",
            "reason": f"复核命令 `{cmd}` exit {r.returncode}——阻塞属实"}


_PASS_VERDICTS = {0, "0", "pass"}


def evidence_delta(task_dir: str, update: bool = False) -> dict:
    """attempt 边界 evidence 增量对比（机械进展判定的输入）。"""
    with open(os.path.join(task_dir, "journal.md"), "r", encoding="utf-8") as f:
        journal = f.read()
    count, fp = evidence_snapshot(journal)
    st = orch_get(task_dir)
    prev_count = int(st["evidence_count"] or 0)
    changed = fp != st["evidence_fingerprint"]
    result = {"new_count": max(0, count - prev_count), "total": count,
              "changed": changed}
    if update:
        orch_set(task_dir, "evidence_count", str(count))
        orch_set(task_dir, "evidence_fingerprint", fp)
    return result


def checklist_flips(task_dir: str, update: bool = False) -> dict:
    """验收项翻转计数（触发检测的输入）。"""
    with open(os.path.join(task_dir, "任务协议单.md"), "r", encoding="utf-8") as f:
        contract = f.read()
    with open(os.path.join(task_dir, "journal.md"), "r", encoding="utf-8") as f:
        journal = f.read()
    cmds = parse_checklist_commands(contract)
    entries = parse_evidence_entries(journal)
    satisfied = sum(
        1 for c in cmds
        if any(str(e.get("command_or_method", "")).strip() == c
               and e.get("exit_status_or_verdict") in _PASS_VERDICTS
               for e in entries))
    baseline = int(orch_get(task_dir)["checklist_baseline"] or 0)
    result = {"satisfied": satisfied, "total": len(cmds),
              "flips": satisfied - baseline}
    if update:
        orch_set(task_dir, "checklist_baseline", str(satisfied))
    return result


def anchor_check(task_dir: str) -> dict:
    """量尺锚定：command_or_method 必须对上任务协议单某条验收命令。"""
    with open(os.path.join(task_dir, "任务协议单.md"), "r", encoding="utf-8") as f:
        cmds = set(parse_checklist_commands(f.read()))
    with open(os.path.join(task_dir, "journal.md"), "r", encoding="utf-8") as f:
        entries = parse_evidence_entries(f.read())
    unanchored = [str(e.get("evidence_id", "?")) for e in entries
                  if str(e.get("command_or_method", "")).strip() not in cmds]
    return {"unanchored": unanchored}


def run_check(task_dir: str, item: int, turn: int | None = None, timeout: int = 600) -> int:
    """跑协议验收命令并把 evidence 亲手记进 journal（执行者插不进的 10 字段由工具自算）。
    返回值 = 验收命令的真实退出码；工具自身故障（越界/无命令/状态冻结）返回 2。"""
    contract_path = os.path.join(task_dir, "任务协议单.md")
    try:
        with open(contract_path, "r", encoding="utf-8") as f:
            contract = f.read()
    except OSError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    cmds = parse_checklist_commands(contract)
    if item < 1 or item > len(cmds):
        print(json.dumps(
            {"error": f"验收清单项 {item} 越界（共 {len(cmds)} 项）"},
            ensure_ascii=False), file=sys.stderr)
        return 2
    cmd = cmds[item - 1]

    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           timeout=timeout)
        exit_code = r.returncode
        output = (r.stdout or b"") + (r.stderr or b"")
    except subprocess.TimeoutExpired as e:
        exit_code = 124
        output = (e.stdout or b"") + (e.stderr or b"")

    now = datetime.datetime.now(datetime.timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    checks_dir = os.path.join(task_dir, "checks")
    os.makedirs(checks_dir, exist_ok=True)
    log_name = f"check-{item}-{ts}.log"
    log_path = os.path.join(checks_dir, log_name)
    with open(log_path, "wb") as f:
        f.write(output)

    content_hash = hashlib.sha256(output).hexdigest()
    observed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_until = (now + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    if turn is not None:
        producer_attempt_id = f"turn-{turn}"
    else:
        try:
            attempt = orch_get(task_dir)["attempt"]
            producer_attempt_id = f"attempt-{attempt}"
        except Exception:
            producer_attempt_id = "attempt-0"

    journal_path = os.path.join(task_dir, "journal.md")
    lockfile = os.path.join(task_dir, ".state.lock")
    fd = acquire_lock(lockfile, "MUTATION")
    try:
        ws_path = os.path.join(task_dir, "工作底稿.md")
        if os.path.exists(ws_path):
            with open(ws_path, "r", encoding="utf-8") as f:
                ws = f.read()
            sm = re.search(r"状态\s*[:：]\s*(\S+)", ws)
            if sm and sm.group(1) != "执行中":
                release_lock(fd)
                print(json.dumps(
                    {"error": f"任务状态非'执行中'（当前：{sm.group(1)}），拒绝追加 evidence"},
                    ensure_ascii=False), file=sys.stderr)
                return 2
        if orch_get(task_dir).get("phase") == "verify":
            release_lock(fd)
            print(json.dumps(
                {"error": "任务在验证期（phase=verify），拒绝执行侧写入 evidence"},
                ensure_ascii=False), file=sys.stderr)
            return 2

        with open(journal_path, "r", encoding="utf-8") as f:
            journal = f.read()
        evidence_id = f"ev-{len(parse_evidence_entries(journal)) + 1:03d}"
        verifier_id = f"run-check-{uuid.uuid4().hex[:8]}"

        yaml_block = (
            "\n```yaml\n"
            "evidence:\n"
            f"  - evidence_id: {evidence_id}\n"
            f"    producer_attempt_id: {producer_attempt_id}\n"
            f"    kind: shell\n"
            f"    locator: {os.path.join('checks', log_name)}\n"
            f"    content_hash: {content_hash}\n"
            f"    observed_at: \"{observed_at}\"\n"
            f"    fresh_until: \"{fresh_until}\"\n"
            f"    command_or_method: {json.dumps(cmd, ensure_ascii=False)}\n"
            f"    exit_status_or_verdict: {exit_code}\n"
            f"    verifier_id: {verifier_id}\n"
            "```\n"
        )

        sec = _find_section(journal, "结构化 evidence")
        if sec:
            insert_at = sec[2]
            journal = journal[:insert_at] + yaml_block + journal[insert_at:]
        else:
            journal = journal.rstrip("\n") + "\n\n## 结构化 evidence\n" + yaml_block

        with open(journal_path, "w", encoding="utf-8") as f:
            f.write(journal)
    finally:
        release_lock(fd)

    print(json.dumps({
        "evidence_id": evidence_id,
        "exit_code": exit_code,
        "content_hash": content_hash,
        "log": log_path,
    }, ensure_ascii=False))
    return exit_code


def evidence_list(task_dir: str) -> int:
    journal_path = os.path.join(task_dir, "journal.md")
    try:
        with open(journal_path, "r", encoding="utf-8") as f:
            journal = f.read()
    except OSError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    entries = parse_evidence_entries(journal)
    print(json.dumps(entries, ensure_ascii=False, indent=2))
    return 0


def main():
    """CLI 入口。用法：state-guard lock --mode M --target T"""
    import argparse
    parser = argparse.ArgumentParser(prog="state-guard")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_verify = sub.add_parser("verify-freeze", help="核对任务目录的冻结哈希")
    p_verify.add_argument("--task-dir", required=True)

    p_update = sub.add_parser("update-freeze", help="更新工作底稿的冻结哈希")
    p_update.add_argument("--task-dir", required=True)
    p_update.add_argument("--files", required=True, nargs="+",
                          help="要更新哈希的文件名（相对 task-dir）")

    p_val = sub.add_parser("validate-schema", help="校验 YAML schema")
    p_val.add_argument("--type", required=True, choices=["evidence", "progress8"])
    p_val.add_argument("--input", required=True, help="YAML 文件路径")

    p_freeze = sub.add_parser("freeze-journal", help="冻结 journal 并更新工作底稿哈希（finish 第 0 步）")
    p_freeze.add_argument("--task-dir", required=True)

    p_og = sub.add_parser("orch-get", help="读编排状态（JSON）")
    p_og.add_argument("--task-dir", required=True)
    p_og.add_argument("--field", choices=ORCH_FIELDS)

    p_os = sub.add_parser("orch-set", help="写编排状态单字段（MUTATION 锁）")
    p_os.add_argument("--task-dir", required=True)
    p_os.add_argument("--field", required=True, choices=ORCH_FIELDS)
    p_os.add_argument("--value", required=True)

    p_vb = sub.add_parser("verify-blocked", help="停止型终态核验（被阻塞）")
    p_vb.add_argument("--task-dir", required=True)
    p_vb.add_argument("--timeout", type=int, default=30)

    p_ed = sub.add_parser("evidence-delta", help="attempt 边界 evidence 增量对比")
    p_ed.add_argument("--task-dir", required=True)
    p_ed.add_argument("--update", action="store_true")

    p_cf = sub.add_parser("checklist-flips", help="验收项翻转计数")
    p_cf.add_argument("--task-dir", required=True)
    p_cf.add_argument("--update", action="store_true")

    p_ac = sub.add_parser("anchor-check", help="量尺锚定校验（finish 拒收依据）")
    p_ac.add_argument("--task-dir", required=True)

    p_rc = sub.add_parser("run-check", help="跑协议验收命令并把 evidence 亲手记进 journal")
    p_rc.add_argument("--task-dir", required=True)
    p_rc.add_argument("--item", type=int, required=True, help="验收清单项编号（1-based）")
    p_rc.add_argument("--turn", type=int, help="turn 号（可选，缺则用 orch_get.attempt）")
    p_rc.add_argument("--timeout", type=int, default=600)

    p_el = sub.add_parser("evidence-list", help="列出 journal 全部 evidence（JSON）")
    p_el.add_argument("--task-dir", required=True)

    p_dt = sub.add_parser("declare-terminal",
                          help="工具代笔终态：追加 journal + 更新 sidecar")
    p_dt.add_argument("--task-dir", required=True)
    p_dt.add_argument("--state", required=True, choices=list(TERMINAL_STATES),
                      help="命名终态之一")
    p_dt.add_argument("--reason", required=True, help="终态依据一句话")
    p_dt.add_argument("--recheck-cmd", default=None,
                      help="被阻塞时的复核命令（可跑）；其他终态可空")

    p_clt = sub.add_parser("clear-terminal",
                           help="清除 sidecar 最新终态（编排派下一 attempt 前调用）")
    p_clt.add_argument("--task-dir", required=True)

    args = parser.parse_args()
    if args.cmd == "verify-freeze":
        mismatches = verify_freeze_hashes(args.task_dir)
        if mismatches:
            print("FREEZE HASH MISMATCH:", file=sys.stderr)
            for m in mismatches:
                print(f"  {m['file']}: {m['reason']}", file=sys.stderr)
            sys.exit(1)
        # 成功也回执：区分"已核对 N 个文件且匹配"与"无冻结基线（0 个）被跳过"
        ws = os.path.join(args.task_dir, "工作底稿.md")
        recorded = {}
        if os.path.exists(ws):
            with open(ws, "r", encoding="utf-8") as f:
                recorded = _parse_worksheet_hashes(f.read())
        print(json.dumps({"verified": sorted(recorded.keys()),
                          "count": len(recorded)}, ensure_ascii=False))
        sys.exit(0)
    elif args.cmd == "update-freeze":
        worksheet = os.path.join(args.task_dir, "工作底稿.md")
        for filename in args.files:
            file_path = os.path.join(args.task_dir, filename)
            update_freeze_hash(worksheet, file_path)
        sys.exit(0)
    elif args.cmd == "validate-schema":
        with open(args.input, "r", encoding="utf-8") as f:
            content = f.read()
        if args.type == "evidence":
            errors = validate_evidence_schema(content)
        else:
            errors = validate_progress8_schema(content)
        if errors:
            for e in errors:
                print(f"SCHEMA ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    elif args.cmd == "freeze-journal":
        new_hash = freeze_journal(args.task_dir)
        print(f"journal frozen: {new_hash}")
        sys.exit(0)
    elif args.cmd == "orch-get":
        st = orch_get(args.task_dir)
        print(json.dumps({args.field: st[args.field]} if args.field else st,
                         ensure_ascii=False))
        sys.exit(0)
    elif args.cmd == "orch-set":
        orch_set(args.task_dir, args.field, args.value)
        sys.exit(0)
    elif args.cmd == "verify-blocked":
        result = verify_blocked(args.task_dir, args.timeout)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit({"confirmed": 0, "not_applicable": 0,
                  "refuted": 1, "unverifiable": 2}[result["verdict"]])
    elif args.cmd == "evidence-delta":
        result = evidence_delta(args.task_dir, update=args.update)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if result["new_count"] > 0 else 1)
    elif args.cmd == "checklist-flips":
        result = checklist_flips(args.task_dir, update=args.update)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)
    elif args.cmd == "anchor-check":
        result = anchor_check(args.task_dir)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if not result["unanchored"] else 1)
    elif args.cmd == "run-check":
        sys.exit(run_check(args.task_dir, args.item, args.turn, args.timeout))
    elif args.cmd == "evidence-list":
        sys.exit(evidence_list(args.task_dir))
    elif args.cmd == "declare-terminal":
        try:
            declare_terminal(args.task_dir, args.state, args.reason, args.recheck_cmd)
        except ValueError as e:
            print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
            sys.exit(2)
        sc = _read_state_json(args.task_dir) or {}
        print(json.dumps({
            "terminal": args.state,
            "ts": sc.get("latest_terminal_ts"),
            "sidecar": _state_json_path(args.task_dir),
        }, ensure_ascii=False))
        sys.exit(0)
    elif args.cmd == "clear-terminal":
        clear_terminal(args.task_dir)
        sys.exit(0)


if __name__ == "__main__":
    main()
