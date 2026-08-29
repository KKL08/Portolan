#!/usr/bin/env python3
"""state-guard: portolan 写入前不变式断言组件。
承担：fcntl 三策略锁、冻结哈希、schema 校验、编排状态 sidecar、判停链。
"""
import datetime
import difflib
import fcntl
import hashlib
import json
import os
import re
import shutil
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
    """计算文件内容的 sha256（原始字节；遗留无前缀记录的比对基准）"""
    with open(file_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# 规范化 v1 前缀：记录带此前缀 = 走 normalize_content 规范化比对；无前缀 = 遗留原始字节
FREEZE_HASH_PREFIX = "sha256-n1:"


def normalize_content(raw: bytes) -> bytes:
    """内容规范化 v1：UTF-8 解码 → 去 BOM → CRLF/CR 统一 LF → 末尾归一单个 \\n。
    不删行内尾随空格、不动缩进——Markdown 行尾双空格是硬换行语义，肉眼可见的
    变化就该触发信号。返回规范化后的 UTF-8 字节。"""
    text = raw.decode("utf-8")
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.rstrip("\n") + "\n"
    return text.encode("utf-8")


def freeze_hash(file_path: str) -> str:
    """规范化冻结哈希：normalize_content 后取 sha256，带 sha256-n1: 前缀。
    freeze / update-freeze / amend-freeze 一律写此前缀。"""
    with open(file_path, "rb") as f:
        raw = f.read()
    return FREEZE_HASH_PREFIX + hashlib.sha256(normalize_content(raw)).hexdigest()


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
        # 匹配 `- <文件名>: `<hash>`` 或 `- <文件名>: <hash>`；hash 可带 sha256-n1: 前缀
        m2 = re.match(r"^\-\s*([^\s:]+):\s*`?((?:sha256-n1:)?[a-f0-9]{64})`?", line)
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
        # 带前缀记录走规范化比对，无前缀遗留记录按原始字节比对
        if expected_hash.startswith(FREEZE_HASH_PREFIX):
            actual = freeze_hash(file_path)
        else:
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
    new_hash = freeze_hash(file_path)

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
        pattern = re.compile(rf"^\-\s*{re.escape(filename)}:\s*`?(?:sha256-n1:)?[a-f0-9]{{64}}`?\s*$", re.M)
        if pattern.search(section):
            new_section = pattern.sub(f"- {filename}: `{new_hash}`", section)
        else:
            new_section = section.rstrip() + f"\n- {filename}: `{new_hash}`\n"
        content = content[:body_start] + new_section + content[body_end:]

    with open(worksheet_path, "w", encoding="utf-8") as f:
        f.write(content)


def _remove_freeze_hash(worksheet_path: str, record_key: str) -> None:
    """删除工作底稿"冻结哈希"节内 record_key 对应的记录行（不存在则无操作）。
    旧键快照记录迁移到新键后调此清理遗留行。"""
    try:
        with open(worksheet_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return
    sec = _find_section(content, "冻结哈希")
    if not sec:
        return
    _, body_start, body_end = sec
    section = content[body_start:body_end]
    pattern = re.compile(
        rf"^\-\s*{re.escape(record_key)}:\s*`?(?:sha256-n1:)?[a-f0-9]{{64}}`?\s*\n?",
        re.M)
    new_section = pattern.sub("", section)
    if new_section == section:
        return
    content = content[:body_start] + new_section + content[body_end:]
    with open(worksheet_path, "w", encoding="utf-8") as f:
        f.write(content)


# ── 冻结快照（.frozen/ 只读副本 + sidecar frozen_paths 缓存）───────
# 唯一真相源仍是工作底稿冻结哈希节：live 哈希（键=文件相对路径）与快照哈希
# （键=.frozen/<snap_key>）双记录，快照实体与记录互为交叉验证。frozen_paths
# 是 hook 写保护的加速缓存，从工作底稿真相源重建，被毒最多漏拦一次。
# snap_key = <sha256(norm)[:8]>-<basename>：norm 为工作底稿记录用相对路径。
# 键只依赖单文件路径、不依赖冻结文件集合——加文件不翻旧键；同 basename 不同路径
# （x.py 与 sub/x.py）落不同槽，杜绝静默别名。旧键（纯 basename）读回退 + 写迁移见下。

FROZEN_DIR = ".frozen"
V1_SUFFIX = ".v1"  # 初版基线拷贝后缀（.frozen/<snap_key>.v1）：audit-chain v1→vN diff 基线


def _snap_key(task_dir: str, file_rel: str) -> str:
    """快照槽键：<sha256(norm)[:8]>-<basename>，norm=工作底稿记录用相对路径（POSIX 斜杠）。
    与 update_freeze_hash 的 live 记录键同源（os.path.relpath），保证冻结期与读回期一致。"""
    norm = os.path.relpath(os.path.join(task_dir, file_rel), task_dir)
    norm = norm.replace(os.sep, "/")
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:8]
    return f"{digest}-{os.path.basename(norm)}"


def _snap_path_for(task_dir: str, file_rel: str) -> str:
    """快照实体路径：新键（.frozen/<snap_key>）优先，旧键（.frozen/<basename>）回退。
    两者都不存在返回新键路径。契约快照读取（triage_mode/成功画像/校验档位）共用。"""
    new_p = os.path.join(task_dir, FROZEN_DIR, _snap_key(task_dir, file_rel))
    if os.path.exists(new_p):
        return new_p
    old_p = os.path.join(task_dir, FROZEN_DIR, os.path.basename(file_rel))
    if os.path.exists(old_p):
        return old_p
    return new_p


def _preserve_v1_snapshot(snap_path: str) -> None:
    """初版基线拷贝 .frozen/<snap_key>.v1：仅首冻建立，后续 freeze/amend 不覆盖。
    audit-chain 的 v1→vN 全量 diff 基线——amend 滚动覆盖快照实体，唯此保初版。"""
    v1 = snap_path + V1_SUFFIX
    if not os.path.exists(v1):
        shutil.copyfile(snap_path, v1)
        os.chmod(v1, 0o444)


def snapshot_frozen_file(task_dir: str, file_path: str) -> str:
    """把冻结文件复制到 .frozen/<snap_key> 并置 0444 只读。返回快照路径。
    覆盖旧快照（0444）前先解除只读位——续跑重冻会重复走这里。"""
    frozen_dir = os.path.join(task_dir, FROZEN_DIR)
    os.makedirs(frozen_dir, exist_ok=True)
    key = _snap_key(task_dir, os.path.relpath(file_path, task_dir))
    dst = os.path.join(frozen_dir, key)
    if os.path.exists(dst):
        os.chmod(dst, 0o644)
        os.remove(dst)
    shutil.copyfile(file_path, dst)
    os.chmod(dst, 0o444)
    return dst


def _migrate_legacy_v1(task_dir: str, file_rel: str, new_snap_path: str) -> None:
    """迁移旧键初版基线 .frozen/<basename>.v1 → 新键 .frozen/<snap_key>.v1（仅当新键 v1
    未建且旧键 v1 存在）。保住原始 v1 基线，避免 update-freeze 重跑把审计基线错设成当前态。"""
    new_v1 = new_snap_path + V1_SUFFIX
    if os.path.exists(new_v1):
        return
    basename = os.path.basename(os.path.relpath(
        os.path.join(task_dir, file_rel), task_dir))
    old_v1 = os.path.join(task_dir, FROZEN_DIR, basename + V1_SUFFIX)
    if os.path.exists(old_v1):
        shutil.copyfile(old_v1, new_v1)
        os.chmod(new_v1, 0o444)


def _cleanup_legacy_snapshot(task_dir: str, file_rel: str) -> None:
    """update-freeze 重跑迁移入口：清理同文件的旧键（.frozen/<basename>）记录、
    实体与 .v1。旧键仅在纯 basename 命名的历史快照出现；新键恒带 8 位 hash 前缀，
    故删 .frozen/<basename> 绝不误伤新键槽。"""
    basename = os.path.basename(os.path.relpath(
        os.path.join(task_dir, file_rel), task_dir))
    old_snap_path = os.path.join(task_dir, FROZEN_DIR, basename)
    for p in (old_snap_path, old_snap_path + V1_SUFFIX):
        if os.path.exists(p):
            os.chmod(p, 0o644)
            os.remove(p)
    _remove_freeze_hash(_ws_path(task_dir), f"{FROZEN_DIR}/{basename}")


def freeze_files(task_dir: str, files: list[str]) -> dict:
    """冻结一组文件（dispatch/continue 调 update-freeze 走这里）：
    ① 每文件写 live 规范化哈希进工作底稿冻结哈希节
    ② 复制到 .frozen/<snap_key>（0444），把快照哈希记进同节（键 .frozen/<snap_key>）
    ③ 清理该文件的旧键（.frozen/<basename>）记录与实体——update-freeze 即一次性迁移入口
    ④ 从工作底稿真相源重建 sidecar frozen_paths 缓存，并迁到 schema v2
    files 相对 task_dir（可指向任务目录外的评分标准文件）。"""
    worksheet_path = _ws_path(task_dir)
    for f in files:
        file_path = os.path.join(task_dir, f)
        update_freeze_hash(worksheet_path, file_path)          # live 哈希
        dst = snapshot_frozen_file(task_dir, file_path)        # 只读快照（新键）
        _migrate_legacy_v1(task_dir, f, dst)                   # 旧键 v1 → 新键（若有）
        _preserve_v1_snapshot(dst)                             # 初版基线（仅首冻）
        update_freeze_hash(worksheet_path, dst)                # 快照哈希（键 .frozen/<snap_key>）
        _cleanup_legacy_snapshot(task_dir, f)                  # 清旧键（迁移）
    frozen_paths = _rebuild_frozen_paths(task_dir)
    _write_frozen_paths_sidecar(task_dir, frozen_paths)
    return {"frozen_paths": frozen_paths}


def _rebuild_frozen_paths(task_dir: str) -> list[str]:
    """从工作底稿冻结哈希记录重建 frozen_paths（realpath 绝对路径，排除快照副本）。"""
    try:
        with open(_ws_path(task_dir), "r", encoding="utf-8") as f:
            recorded = _parse_worksheet_hashes(f.read())
    except OSError:
        return []
    paths = []
    for key in recorded:
        if key.startswith(FROZEN_DIR + "/"):
            continue  # 快照副本本身不进 live 写保护清单
        rp = os.path.realpath(os.path.join(task_dir, key))
        if rp not in paths:
            paths.append(rp)
    return sorted(paths)


def freeze_journal(task_dir: str) -> str:
    """冻结 journal.md：更新工作底稿哈希，并置 phase=verify（自此执行侧写入被拒）。
    返回新 hash。finish 第 0 步用。
    """
    journal_path = os.path.join(task_dir, "journal.md")
    worksheet_path = os.path.join(task_dir, "工作底稿.md")
    update_freeze_hash(worksheet_path, journal_path)
    orch_set(task_dir, "phase", "verify")
    return freeze_hash(journal_path)


# ── 受控变更：amend-freeze（T4）─────────────────────────────────
# 停点窗口内的受控重冻结。原子序列：验快照完整性 → journal 追加 amend 条目 →
# 更新工作底稿 live+快照双记录 → 覆盖 .frozen/ 快照 → 刷新 sidecar 缓存。
# 任何一步失败整体回滚，无半态。exec 阶段拒绝（合法 amend 只发生在停点窗口）。

FREEZE_AMEND_HEADING = "冻结变更"


class AmendPhaseError(RuntimeError):
    """phase==exec 时调 amend-freeze（合法 amend 只在停点窗口）"""


class AmendIntegrityError(RuntimeError):
    """快照完整性异常（缺失/投毒/记录被动），须先走 verify-freeze --explain 分诊"""


def _norm_lines(raw: bytes) -> list[str]:
    return normalize_content(raw).decode("utf-8").splitlines()


def unified_freeze_diff(old_raw: bytes, new_raw: bytes,
                        old_label: str = "v1", new_label: str = "live") -> str:
    """规范化后的 unified diff（快照 vs 现状）。"""
    return "\n".join(difflib.unified_diff(
        _norm_lines(old_raw), _norm_lines(new_raw),
        fromfile=old_label, tofile=new_label, lineterm=""))


def _diff_summary(old_raw: bytes, new_raw: bytes) -> str:
    """diff 摘要：+新增行 -删除行（规范化后计）。"""
    diff = list(difflib.unified_diff(_norm_lines(old_raw), _norm_lines(new_raw),
                                     lineterm=""))
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    return f"+{added} -{removed}"


def _freeze_integrity(task_dir: str, file_rel: str) -> dict:
    """单个冻结文件的快照完整性核验（amend/explain/triage 共用）。
    broken=True 的四类：live 记录缺失 / 快照记录缺失 / 快照实体缺失 /
    快照哈希对不上记录（疑似投毒）。返回诊断 dict（含 old_hash / snap_path 等）。
    快照键新键（.frozen/<snap_key>）优先，找不到回退旧键（.frozen/<basename>）；
    命中旧键置 legacy=True。new_snap_* 恒为迁移目标键，供 amend 迁移用。"""
    ws_path = _ws_path(task_dir)
    file_path = os.path.join(task_dir, file_rel)
    filename = os.path.relpath(file_path, task_dir)
    basename = os.path.basename(file_path)
    key = _snap_key(task_dir, file_rel)
    new_snap_rel = f"{FROZEN_DIR}/{key}"
    new_snap_path = os.path.join(task_dir, FROZEN_DIR, key)
    old_snap_rel = f"{FROZEN_DIR}/{basename}"
    old_snap_path = os.path.join(task_dir, FROZEN_DIR, basename)
    try:
        with open(ws_path, "r", encoding="utf-8") as f:
            recorded = _parse_worksheet_hashes(f.read())
    except OSError:
        recorded = {}
    # 新键优先，找不到（记录与实体均无）再回退旧键
    legacy = False
    snap_rel, snap_path = new_snap_rel, new_snap_path
    if (new_snap_rel not in recorded and not os.path.exists(new_snap_path)
            and (old_snap_rel in recorded or os.path.exists(old_snap_path))):
        snap_rel, snap_path = old_snap_rel, old_snap_path
        legacy = True
    info = {"filename": filename, "snap_rel": snap_rel, "snap_path": snap_path,
            "new_snap_rel": new_snap_rel, "new_snap_path": new_snap_path,
            "legacy": legacy, "file_path": file_path,
            "old_hash": recorded.get(filename),
            "recorded_snap_hash": recorded.get(snap_rel),
            "broken": False, "reason": ""}
    if filename not in recorded:
        info["broken"], info["reason"] = True, "工作底稿缺 live 冻结记录行"
    elif snap_rel not in recorded:
        info["broken"], info["reason"] = True, "工作底稿缺快照冻结记录行"
    elif not os.path.exists(snap_path):
        info["broken"], info["reason"] = True, ".frozen 快照实体缺失"
    elif freeze_hash(snap_path) != recorded[snap_rel]:
        info["broken"], info["reason"] = True, "快照哈希对不上记录（疑似投毒）"
    return info


def _amend_yaml_block(entry: dict) -> str:
    """把一条 amend 记录序列化为独立 ```yaml 围栏块（append 进 journal）。"""
    lines = ["\n```yaml", "amend:",
             f"  - file: {json.dumps(entry['file'], ensure_ascii=False)}",
             f"    old_hash: \"{entry['old_hash']}\"",
             f"    new_hash: \"{entry['new_hash']}\"",
             f"    approver: {entry['approver']}",
             f"    reason: {json.dumps(entry['reason'], ensure_ascii=False)}",
             f"    diff_summary: {json.dumps(entry['diff_summary'], ensure_ascii=False)}"]
    if entry.get("direction"):
        lines.append(f"    direction: {entry['direction']}")
    lines.append(f"    ts: \"{entry['ts']}\"")
    lines.append("```\n")
    return "\n".join(lines)


def _append_journal_section_block(task_dir: str, heading: str, block: str) -> None:
    """把一个 markdown 块追加进 journal 指定 H2 节末尾（节不存在则新建）。"""
    journal_path = os.path.join(task_dir, "journal.md")
    try:
        with open(journal_path, "r", encoding="utf-8") as f:
            journal = f.read()
    except OSError:
        journal = "# journal\n"
    sec = _find_section(journal, heading)
    if sec:
        insert_at = sec[2]
        journal = journal[:insert_at] + block + journal[insert_at:]
    else:
        journal = journal.rstrip("\n") + f"\n\n## {heading}\n" + block
    with open(journal_path, "w", encoding="utf-8") as f:
        f.write(journal)


def parse_amend_entries(journal_content: str) -> list[dict]:
    """journal 里全部 ```yaml `amend:` 列表条目（按出现顺序）。"""
    entries = []
    for block in re.findall(r"```yaml\n(.*?)```", journal_content, re.DOTALL):
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and isinstance(data.get("amend"), list):
            entries.extend(e for e in data["amend"] if isinstance(e, dict))
    return entries


# ── 归因（verify-freeze --explain，T5）───────────────────────────
# 控制流只认一个判定 integrity_broken（快照缺失/快照哈希对不上记录/工作底稿记录
# 行被动）→ 直达人工；其余输出自由文本归因（快照 diff + hook-events + mtime 提示）。


def _read_hook_events(task_dir: str, basename: str | None = None) -> list[dict]:
    """读任务目录 hook-events.jsonl（容错：不存在/坏行都跳过）。
    basename 给定则只留 path 命中该 basename 的事件。"""
    path = os.path.join(task_dir, "hook-events.jsonl")
    events = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if basename is None or basename in str(ev.get("path", "")):
                    events.append(ev)
    except OSError:
        pass
    return events


def _build_attribution(integ: dict, diff_summary: str,
                       hook_events: list[dict], file_path: str) -> str:
    """自由文本归因叙述（证据源：完整性判定 + hook 事件 + mtime 提示）。"""
    parts = []
    if integ["broken"]:
        parts.append(f"完整性判定：integrity_broken（{integ['reason']}）"
                     "——快照或记录本身可疑，直达人工，不进盲审。")
    else:
        parts.append("完整性判定：快照与记录交叉一致；哈希信号来自 live 与快照"
                     f"的内容差异（{diff_summary or '无差异'}）。")
    blocked = [e for e in hook_events if e.get("decision") == "block"]
    if hook_events:
        last = hook_events[-1]
        parts.append(f"hook 事件：{len(hook_events)} 条涉及本文件"
                     f"（拦截 {len(blocked)}），最近一条 "
                     f"{last.get('ts', '?')} {last.get('tool', '?')}。")
    else:
        parts.append("hook 事件：无本文件相关记录（可能绕开工具直改，或 hook 未启用）。")
    try:
        mt = datetime.datetime.fromtimestamp(
            os.path.getmtime(file_path),
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        parts.append(f"live 文件 mtime={mt}（仅提示，不作判定依据——"
                     "复制/git 操作都会重置）。")
    except OSError:
        pass
    return " ".join(parts)


def explain_freeze(task_dir: str, file_rel: str) -> dict:
    """归因输出：{file, integrity_broken, diff_summary, attribution, hook_events}。"""
    integ = _freeze_integrity(task_dir, file_rel)
    file_path = integ["file_path"]
    snap_path = integ["snap_path"]
    basename = os.path.basename(file_path)
    diff_summary = ""
    if os.path.exists(snap_path) and os.path.exists(file_path):
        with open(snap_path, "rb") as f:
            old = f.read()
        with open(file_path, "rb") as f:
            new = f.read()
        diff_summary = _diff_summary(old, new)
    hook_events = _read_hook_events(task_dir, basename)
    return {
        "file": integ["filename"],
        "integrity_broken": integ["broken"],
        "diff_summary": diff_summary,
        "attribution": _build_attribution(integ, diff_summary, hook_events,
                                          file_path),
        "hook_events": hook_events,
    }


def amend_freeze(task_dir: str, file_rel: str, reason: str, approver: str,
                 direction: str | None = None) -> dict:
    """受控重冻结（原子）。approver ∈ {human, triage-auto}；phase==exec 拒绝。
    直接改 file_rel 冻结记录、快照与缓存，并把追认条目写进 journal。返回 amend 记录。"""
    if approver not in ("human", "triage-auto"):
        raise ValueError(f"未知 approver: {approver}（合法：human|triage-auto）")
    lockfile = os.path.join(task_dir, ".state.lock")
    fd = acquire_lock(lockfile, "MUTATION")
    try:
        if orch_get(task_dir).get("phase") == "exec":
            raise AmendPhaseError(
                "phase=exec 期间拒绝 amend-freeze（合法 amend 只发生在停点窗口，"
                "先由编排层把 phase 切出 exec）")
        integ = _freeze_integrity(task_dir, file_rel)
        if integ["broken"]:
            raise AmendIntegrityError(
                f"冻结完整性异常（{integ['reason']}），先跑 "
                f"verify-freeze --explain 分诊，不得直接 amend")
        file_path = integ["file_path"]
        if not os.path.exists(file_path):
            raise AmendIntegrityError("live 文件缺失，无法 amend")
        ws_path = _ws_path(task_dir)
        cur_snap_path = integ["snap_path"]          # 现快照（legacy 时=旧键）
        new_snap_path = integ["new_snap_path"]      # 迁移目标（新键）
        legacy = integ["legacy"]
        filename = integ["filename"]
        old_hash = integ["old_hash"]
        new_hash = freeze_hash(file_path)
        with open(cur_snap_path, "rb") as f:
            old_bytes = f.read()
        with open(file_path, "rb") as f:
            new_bytes = f.read()
        now = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        entry = {"file": filename, "old_hash": old_hash, "new_hash": new_hash,
                 "approver": approver, "reason": reason,
                 "diff_summary": _diff_summary(old_bytes, new_bytes),
                 "direction": direction, "ts": now}

        # 回滚快照（内存留存全部待改文件与 .frozen 实体的原始态）
        journal_path = os.path.join(task_dir, "journal.md")
        cur_v1 = cur_snap_path + V1_SUFFIX
        new_v1 = new_snap_path + V1_SUFFIX
        with open(ws_path, "rb") as f:
            ws_backup = f.read()
        journal_backup = None
        if os.path.exists(journal_path):
            with open(journal_path, "rb") as f:
                journal_backup = f.read()
        sidecar_backup = _read_state_json(task_dir)

        def _snap_bytes(p):
            if os.path.exists(p):
                with open(p, "rb") as fh:
                    return fh.read()
            return None

        def _restore_snap(p, data):
            if os.path.exists(p):
                os.chmod(p, 0o644)
                os.remove(p)
            if data is not None:
                with open(p, "wb") as fh:
                    fh.write(data)
                os.chmod(p, 0o444)

        # 涉及的四个 .frozen 实体（legacy 时新旧不同；否则新旧同路径）原始态快照
        snap_backups = [(cur_snap_path, old_bytes), (cur_v1, _snap_bytes(cur_v1)),
                        (new_snap_path, _snap_bytes(new_snap_path)),
                        (new_v1, _snap_bytes(new_v1))]

        try:
            _append_journal_section_block(task_dir, FREEZE_AMEND_HEADING,
                                          _amend_yaml_block(entry))   # ①
            update_freeze_hash(ws_path, file_path)                    # ② live 记录
            if os.path.exists(new_snap_path):                         # ③ 写新键快照
                os.chmod(new_snap_path, 0o644)
            shutil.copyfile(file_path, new_snap_path)
            os.chmod(new_snap_path, 0o444)
            update_freeze_hash(ws_path, new_snap_path)                # ②' 新键快照记录
            if legacy:                                                # ③' 迁移旧键
                if os.path.exists(cur_v1) and not os.path.exists(new_v1):
                    shutil.copyfile(cur_v1, new_v1)                   #   v1 → 新键
                    os.chmod(new_v1, 0o444)
                if os.path.exists(cur_v1):
                    os.chmod(cur_v1, 0o644)
                    os.remove(cur_v1)
                if os.path.exists(cur_snap_path):
                    os.chmod(cur_snap_path, 0o644)
                    os.remove(cur_snap_path)
                _remove_freeze_hash(ws_path, integ["snap_rel"])      #   删旧记录行
            frozen_paths = _rebuild_frozen_paths(task_dir)            # ④ 刷新缓存
            sidecar = _migrate_sidecar(_read_state_json(task_dir) or {})
            sidecar["frozen_paths"] = frozen_paths
            _write_state_json_atomic(task_dir, sidecar)
        except Exception:
            with open(ws_path, "wb") as f:
                f.write(ws_backup)
            if journal_backup is not None:
                with open(journal_path, "wb") as f:
                    f.write(journal_backup)
            elif os.path.exists(journal_path):
                os.remove(journal_path)
            for p, data in snap_backups:
                _restore_snap(p, data)
            if sidecar_backup is not None:
                _write_state_json_atomic(task_dir, sidecar_backup)
            raise
        return entry
    finally:
        release_lock(fd)


# ── 信号分诊管线（triage，T6）───────────────────────────────────
# 哈希不匹配是信号，非篡改定论。分诊：① 快照完整性前置校验（integrity_broken →
# 直达人工，不分诊）② diff 产出 ③ 模式闸 + 盲审输入 ④ 收尾入账（tighten/equivalent
# 自动追认；loosen/redirect 置 gate 停点）⑤ 熔断（journal 现场统计 triage-auto 连数）。

AUTORATIFY_LIMIT = 3  # 熔断阈值：自上次人批 amend 以来 triage-auto 条数 ≥ 此值 → 无条件 gate
TRIAGE_VERDICTS = ("tighten", "equivalent", "loosen", "redirect")


class ProposalExistsError(RuntimeError):
    """已有未消费的 pending_proposal，拒绝覆盖（先走 continue 决策卡）"""


def _read_triage_mode(task_dir: str) -> str:
    """从任务协议单读 triage_mode 契约字段（manual|assisted），缺省 assisted。
    契约字段随清单冻结，优先读 .frozen 快照——被审计的 live 若被改动不影响档位判定。"""
    snap = _snap_path_for(task_dir, "任务协议单.md")
    path = snap if os.path.exists(snap) else os.path.join(task_dir, "任务协议单.md")
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return "assisted"
    m = re.search(r"triage_mode\s*[:：]\s*\**(manual|assisted)", content)
    if m:
        return m.group(1)
    m2 = re.search(r"信号处置\s*[:：]\s*\**(manual|assisted|人工|盲审)", content)
    if m2:
        return "manual" if m2.group(1) in ("manual", "人工") else "assisted"
    return "assisted"


def _contract_excerpt(task_dir: str) -> str:
    """任务协议单 v1（.frozen 快照优先）的成功画像 + 验收清单，供盲审受保护输入。"""
    snap = _snap_path_for(task_dir, "任务协议单.md")
    path = snap if os.path.exists(snap) else os.path.join(task_dir, "任务协议单.md")
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return ""
    parts = []
    for h in ("成功画像", "验收清单"):
        sec = _find_section(content, h, stop_at_subsection=(h == "验收清单"))
        if sec:
            parts.append(f"## {h}\n" + content[sec[1]:sec[2]].strip())
    return "\n\n".join(parts)


def _autoratify_streak(journal_content: str) -> int:
    """journal 现场统计：自上次 approver=human 的 amend 以来的 triage-auto 连数。"""
    streak = 0
    for e in parse_amend_entries(journal_content):
        if e.get("approver") == "human":
            streak = 0
        elif e.get("approver") == "triage-auto":
            streak += 1
    return streak


def _autoratify_streak_current(task_dir: str) -> int:
    try:
        with open(os.path.join(task_dir, "journal.md"), encoding="utf-8") as f:
            return _autoratify_streak(f.read())
    except OSError:
        return 0


def set_pending_signal(task_dir: str, signal: dict | None) -> None:
    """写 sidecar 顶层 pending_signal（提示位，编排层消费）。持 MUTATION 锁。"""
    lockfile = os.path.join(task_dir, ".state.lock")
    fd = acquire_lock(lockfile, "MUTATION")
    try:
        sidecar = _migrate_sidecar(_read_state_json(task_dir) or {})
        sidecar["pending_signal"] = signal
        _write_state_json_atomic(task_dir, sidecar)
    finally:
        release_lock(fd)


def triage(task_dir: str, file_rel: str) -> dict:
    """triage --file：step0 完整性 + diff + （assisted 且完整）盲审输入。"""
    integ = _freeze_integrity(task_dir, file_rel)
    file_path = integ["file_path"]
    snap_path = integ["snap_path"]
    diff_text = ""
    if os.path.exists(snap_path) and os.path.exists(file_path):
        with open(snap_path, "rb") as f:
            old = f.read()
        with open(file_path, "rb") as f:
            new = f.read()
        diff_text = unified_freeze_diff(old, new, integ["snap_rel"], file_rel)
    mode = _read_triage_mode(task_dir)
    result = {"step0": "integrity_broken" if integ["broken"] else "integrity_ok",
              "diff": diff_text, "triage_mode": mode}
    if integ["broken"] or mode == "manual":
        result["disposition"] = "escalate_human"
        result["review_inputs"] = None
        return result
    result["disposition"] = "await_review"
    result["review_inputs"] = {"v1_path": integ["snap_rel"],
                               "contract_excerpt": _contract_excerpt(task_dir)}
    return result


def triage_finalize(task_dir: str, file_rel: str, verdict: str) -> dict:
    """triage --review-verdict 收尾：按方向 + 模式 + 熔断分流。
    tighten/equivalent → auto_ratify（amend-freeze triage-auto）；loosen/redirect
    或 manual/熔断/integrity_broken → escalate_human（置 pending_signal 停点）。"""
    if verdict not in TRIAGE_VERDICTS:
        raise ValueError(f"未知盲审方向: {verdict}（合法：{TRIAGE_VERDICTS}）")
    integ = _freeze_integrity(task_dir, file_rel)
    mode = _read_triage_mode(task_dir)
    streak = _autoratify_streak_current(task_dir)
    filename = integ["filename"]

    if integ["broken"]:
        reason = f"integrity_broken：{integ['reason']}"
    elif mode == "manual":
        reason = "triage_mode=manual：全部信号人工处置"
    elif streak >= AUTORATIFY_LIMIT:
        reason = (f"熔断：自上次人批以来已连续 {streak} 条 triage-auto 追认"
                  f"（≥{AUTORATIFY_LIMIT}），本次无条件转人工")
    elif verdict in ("tighten", "equivalent"):
        entry = amend_freeze(task_dir, file_rel,
                             f"triage auto-ratify: {verdict}", "triage-auto",
                             direction=verdict)
        set_pending_signal(task_dir, None)  # 信号已消解
        return {"disposition": "auto_ratify", "verdict": verdict,
                "file": filename, "amend": entry}
    else:  # loosen / redirect
        reason = f"盲审判定 {verdict}：放松/改向须人工"

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    signal = {"file": filename, "verdict": verdict, "reason": reason,
              "integrity_broken": integ["broken"], "ts": now}
    set_pending_signal(task_dir, signal)
    return {"disposition": "escalate_human", "verdict": verdict,
            "file": filename, "reason": reason, "pending_signal": signal}


def propose_change(task_dir: str, found: str, change: str, evidence: str,
                   round_no) -> dict:
    """执行者变更提案：校验四字段 → 写 sidecar 顶层 pending_proposal（嵌套 dict）。
    已有未消费提案 → 拒绝覆盖（ProposalExistsError）。"""
    if not found or not found.strip():
        raise ValueError("found 不能为空")
    if not change or not change.strip():
        raise ValueError("change 不能为空")
    ev_list = [e.strip() for e in (evidence or "").split(",") if e.strip()]
    try:
        round_int = int(round_no)
    except (TypeError, ValueError):
        raise ValueError("round 必须是整数")
    lockfile = os.path.join(task_dir, ".state.lock")
    fd = acquire_lock(lockfile, "MUTATION")
    try:
        sidecar = _migrate_sidecar(_read_state_json(task_dir) or {})
        if sidecar.get("pending_proposal"):
            raise ProposalExistsError(
                "已有未消费的 pending_proposal，先走 continue 决策卡处置再提新提案")
        proposal = {"found": found.strip(), "change": change.strip(),
                    "evidence": ev_list, "round": round_int}
        sidecar["pending_proposal"] = proposal
        _write_state_json_atomic(task_dir, sidecar)
        return proposal
    finally:
        release_lock(fd)


def clear_proposal(task_dir: str) -> None:
    """清除 sidecar pending_proposal（continue 决策卡 approve/reject 消费后调用）。
    幂等：sidecar 缺失或字段已空均无副作用。defer（被阻塞）保留提案不清。"""
    lockfile = os.path.join(task_dir, ".state.lock")
    fd = acquire_lock(lockfile, "MUTATION")
    try:
        sidecar = _read_state_json(task_dir)
        if sidecar is None:
            return
        sidecar["pending_proposal"] = None
        _write_state_json_atomic(task_dir, sidecar)
    finally:
        release_lock(fd)


# ── 轨迹审计（audit-chain，T8）──────────────────────────────────
# finish 终审用，核验追认链的真实性与合法性，四件事：
# ① 追认链哈希衔接：每文件按 ts 序，entry N old_hash == entry N-1 new_hash，
#    首条 old_hash == .frozen 初版基线（.v1）哈希——断裂即伪造链。
# ② 停点窗口核对（威胁模型落地）：每条 amend 的 ts 必须落在 phase!=exec 窗口内。
#    窗口开启点 = 终态声明（人可见停点）+ 信号留痕（哈希信号待处置停点），关闭点 =
#    其后第一条 exec evidence（observed_at，标志 exec 恢复）。approver=human 的
#    条目还必须落在「终态声明」窗口内（人批须对应人可见停点）。exec 期 amend = 伪造审批。
# ③ 放松检测：direction ∈ {loosen,redirect} 的条目 approver 必须是 human
#    （triage-auto 只能 tighten/equivalent；出现自动放松即伪造）。
# ④ v1→vN 全量 diff：.frozen/<name>.v1 对当前 live；无 .v1 则降级为逐次 diff 摘要串联。
# ISO 8601 定宽 UTC 串按字典序即时间序，窗口比对直接用字符串大小。
# 残余风险：执行者同时伪造 journal 全链 + 快照 + 底稿记录可骗过——超出本层防护目标。


def _parse_terminal_records(journal_content: str) -> list[dict]:
    """终态声明节全部记录（按出现序）。返回 [{state, ts}]。"""
    sec = _find_section(journal_content, "终态声明")
    if not sec:
        return []
    section = journal_content[sec[1]:sec[2]]
    out = []
    for rec in re.split(r"\n(?=-\s*日期/轮次\s*[:：])", section):
        tm = _TERMINAL_RE.search(rec)
        ts_m = re.search(r"日期/轮次\s*[:：]\s*(\S+)", rec)
        if tm and ts_m:
            out.append({"state": tm.group(1), "ts": ts_m.group(1)})
    return out


def _parse_signal_records(journal_content: str) -> list[dict]:
    """信号留痕节全部 signal 条目（按出现序）。返回 [{ts, source}]。"""
    out = []
    for block in re.findall(r"```yaml\n(.*?)```", journal_content, re.DOTALL):
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and isinstance(data.get("signal"), list):
            for s in data["signal"]:
                if isinstance(s, dict) and s.get("ts"):
                    out.append({"ts": str(s["ts"]), "source": s.get("source")})
    return out


def _stop_windows(journal_content: str) -> list[dict]:
    """重建 phase!=exec 停点窗口。开启点=终态声明 + 信号留痕；关闭点=其后第一条
    exec evidence（observed_at）。返回 [{start, end|None, kind}]，kind∈{terminal,signal}。"""
    ev_ts = sorted(str(e.get("observed_at", ""))
                   for e in parse_evidence_entries(journal_content)
                   if e.get("observed_at"))
    openers = ([{"ts": t["ts"], "kind": "terminal"}
                for t in _parse_terminal_records(journal_content)]
               + [{"ts": s["ts"], "kind": "signal"}
                  for s in _parse_signal_records(journal_content)])
    windows = []
    for op in openers:
        end = next((e for e in ev_ts if e > op["ts"]), None)
        windows.append({"start": op["ts"], "end": end, "kind": op["kind"]})
    return windows


def _in_window(ts: str, windows: list[dict], kind: str | None = None) -> bool:
    """ts 是否落在某个（指定 kind 的）停点窗口 [start, end) 内。"""
    for w in windows:
        if kind is not None and w["kind"] != kind:
            continue
        if w["start"] <= ts and (w["end"] is None or ts < w["end"]):
            return True
    return False


def _v1_path_for(task_dir: str, filename: str) -> str:
    """初版基线实体路径：新键（.frozen/<snap_key>.v1）优先，旧键（.frozen/<basename>.v1）
    回退；都不存在返回新键路径（调用方按不存在处理）。"""
    new_v1 = os.path.join(task_dir, FROZEN_DIR,
                          _snap_key(task_dir, filename) + V1_SUFFIX)
    if os.path.exists(new_v1):
        return new_v1
    old_v1 = os.path.join(task_dir, FROZEN_DIR,
                          os.path.basename(filename) + V1_SUFFIX)
    if os.path.exists(old_v1):
        return old_v1
    return new_v1


def _v1_hash_for(task_dir: str, filename: str) -> str | None:
    """文件初版基线哈希：新键优先、旧键回退的 .v1 规范化哈希；不存在返回 None。"""
    v1 = _v1_path_for(task_dir, filename)
    return freeze_hash(v1) if os.path.exists(v1) else None


def audit_chain(task_dir: str) -> dict:
    """终审轨迹审计。返回 {passed, failures, chain, diffs}。"""
    try:
        with open(os.path.join(task_dir, "journal.md"), encoding="utf-8") as f:
            journal = f.read()
    except OSError:
        journal = ""
    entries = parse_amend_entries(journal)
    windows = _stop_windows(journal)
    failures = []

    # 按文件分组、按 ts 排序（journal 追加序通常即时间序，稳妥仍排）
    by_file: dict[str, list[dict]] = {}
    for e in entries:
        by_file.setdefault(e.get("file", "?"), []).append(e)
    for evs in by_file.values():
        evs.sort(key=lambda e: str(e.get("ts", "")))

    # ① 追认链哈希衔接
    for fname, evs in by_file.items():
        v1h = _v1_hash_for(task_dir, fname)
        prev = v1h
        for i, e in enumerate(evs):
            old = e.get("old_hash")
            if i == 0:
                if v1h is not None and old != v1h:
                    failures.append({"code": "v1_anchor_mismatch", "file": fname,
                                     "detail": f"首条 old_hash={old} 对不上初版基线 {v1h}"})
            elif old != prev:
                failures.append({"code": "chain_break", "file": fname,
                                 "detail": f"第 {i + 1} 条 old_hash={old} "
                                           f"对不上前条 new_hash={prev}"})
            prev = e.get("new_hash")

    # ② 停点窗口核对 + ③ 放松检测
    for e in entries:
        ts = str(e.get("ts", ""))
        approver = e.get("approver")
        direction = e.get("direction")
        if not _in_window(ts, windows):
            failures.append({"code": "amend_outside_window", "file": e.get("file"),
                             "detail": f"amend ts={ts} 不在任何停点窗口内"
                                       f"（疑似 exec 期伪造审批）"})
        elif approver == "human" and not _in_window(ts, windows, kind="terminal"):
            failures.append({"code": "human_amend_no_stop_record",
                             "file": e.get("file"),
                             "detail": f"人批 amend ts={ts} 无对应终态声明停点记录"})
        if direction in ("loosen", "redirect") and approver != "human":
            failures.append({"code": "unapproved_loosen", "file": e.get("file"),
                             "detail": f"方向 {direction} 未经人批（approver={approver}）"})

    # ④ v1→vN 全量 diff
    diffs = {}
    try:
        with open(_ws_path(task_dir), encoding="utf-8") as f:
            recorded = _parse_worksheet_hashes(f.read())
    except OSError:
        recorded = {}
    for fname in [k for k in recorded if not k.startswith(FROZEN_DIR + "/")]:
        v1_path = _v1_path_for(task_dir, fname)
        live_path = os.path.join(task_dir, fname)
        if os.path.exists(v1_path) and os.path.exists(live_path):
            with open(v1_path, "rb") as f:
                v1b = f.read()
            with open(live_path, "rb") as f:
                lb = f.read()
            diffs[fname] = unified_freeze_diff(
                v1b, lb, f"{os.path.basename(v1_path)}", fname)
        else:
            # 降级：无初版拷贝，逐次 amend diff 摘要串联
            diffs[fname] = {"degraded": True,
                            "summaries": [f"{e.get('ts')}: {e.get('diff_summary')}"
                                          for e in by_file.get(fname, [])]}

    chain = [{"file": e.get("file"), "old_hash": e.get("old_hash"),
              "new_hash": e.get("new_hash"), "approver": e.get("approver"),
              "direction": e.get("direction"), "ts": e.get("ts")}
             for e in entries]
    return {"passed": not failures, "failures": failures,
            "chain": chain, "diffs": diffs}


# ── 校验节奏 + 信号交接（should-verify / 钩子接入，T7）──────────────
# 三锚点（gate/resume/finish）恒查；周期抽查挂契约字段「校验档位」（严格=每轮；
# 标准=每 3 轮或 30 分钟先到；宽松=仅锚点）+ 事件升频（hook block 或盲审发生过
# → 临时每轮，安静 3 轮回落）。轮次/时间/升频态存 sidecar 顶层（提示性质，被删只
# 是多查一次，无安全依赖）。钩子层只做确定性检出，不起 LLM。
#
# T7c 修正：校验档位是独立契约字段，不复用 tolerance_tier（后者是自动化档位
# 全自动/平衡/保守，词表与职责都不同，混用会打架）。档位随清单冻结，优先读
# .frozen 快照，防 live 篡改翻档（与 triage_mode 同法）。

VERIFY_ROUND_PERIOD = 3          # 标准档：每 N 轮抽查
VERIFY_TIME_PERIOD_SEC = 30 * 60  # 标准档：或距上次校验 30 分钟先到
VERIFY_ESCALATION_ROUNDS = 3     # 升频后维持的安静轮数（含触发轮之后的回落窗口）
FREEZE_SIGNAL_HEADING = "信号留痕"


def _tier_kind(tier: str | None) -> str:
    """把校验档位值归一到 strict/standard/loose（缺省/未知 = standard）。"""
    t = (tier or "").strip()
    if t in ("严格", "strict"):
        return "strict"
    if t in ("宽松", "loose"):
        return "loose"
    return "standard"


def _read_verify_tier(task_dir: str) -> str:
    """从任务协议单读「校验档位」契约字段，归一到 strict/standard/loose，缺省 standard。
    契约字段随清单冻结，优先读 .frozen 快照——被审计的 live 若被改档不影响节奏判定。"""
    snap = _snap_path_for(task_dir, "任务协议单.md")
    path = snap if os.path.exists(snap) else os.path.join(task_dir, "任务协议单.md")
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return "standard"
    m = re.search(r"校验档位\s*[:：]\s*\**(严格|标准|宽松|strict|standard|loose)",
                  content)
    if m:
        return _tier_kind(m.group(1))
    return "standard"


def _count_escalation_triggers(task_dir: str) -> int:
    """升频触发源计数（单调不减）：hook block 事件数 + 盲审 amend（带 direction）数。"""
    n = 0
    for ev in _read_hook_events(task_dir):
        if ev.get("decision") == "block":
            n += 1
    try:
        with open(os.path.join(task_dir, "journal.md"), encoding="utf-8") as f:
            n += sum(1 for e in parse_amend_entries(f.read()) if e.get("direction"))
    except OSError:
        pass
    return n


def _bump_cadence_anchor(task_dir: str, now: float | None = None) -> None:
    """锚点校验发生：重置轮次计数与上次校验时间戳。"""
    now = now if now is not None else time.time()
    lockfile = os.path.join(task_dir, ".state.lock")
    fd = acquire_lock(lockfile, "MUTATION")
    try:
        sidecar = _migrate_sidecar(_read_state_json(task_dir) or {})
        sidecar["verify_round_counter"] = 0
        sidecar["verify_last_ts"] = now
        _write_state_json_atomic(task_dir, sidecar)
    finally:
        release_lock(fd)


def should_verify(task_dir: str, context: str, now: float | None = None) -> bool:
    """校验节奏判定。context ∈ {gate,resume,round,finish}：三锚点恒真；
    round 按档位 + 升频态。round 调用有副作用（推进轮次/时间/升频状态）。"""
    if context in ("gate", "resume", "finish"):
        _bump_cadence_anchor(task_dir, now)
        return True
    if context != "round":
        return True  # 未知 context 保守取真
    now = now if now is not None else time.time()
    tier = _read_verify_tier(task_dir)
    triggers = _count_escalation_triggers(task_dir)
    lockfile = os.path.join(task_dir, ".state.lock")
    fd = acquire_lock(lockfile, "MUTATION")
    try:
        sidecar = _migrate_sidecar(_read_state_json(task_dir) or {})
        counter = int(sidecar.get("verify_round_counter", 0)) + 1
        last_ts = float(sidecar.get("verify_last_ts", 0) or 0)
        if last_ts == 0:
            last_ts = now  # 首轮初始化，避免与 epoch 0 的巨大时差误触发
        streak = int(sidecar.get("verify_escalation_streak", 0))
        seen = int(sidecar.get("verify_trigger_seen", 0))
        newly = triggers > seen
        if newly:
            streak = VERIFY_ESCALATION_ROUNDS
        escalated = streak > 0
        if tier == "strict" or escalated:
            due = True
        elif tier == "loose":
            due = False
        else:  # standard
            due = (counter >= VERIFY_ROUND_PERIOD
                   or (now - last_ts) >= VERIFY_TIME_PERIOD_SEC)
        if not newly:
            streak = max(0, streak - 1)
        if due:
            counter = 0
            last_ts = now
        sidecar["verify_round_counter"] = counter
        sidecar["verify_last_ts"] = last_ts
        sidecar["verify_escalation_streak"] = streak
        sidecar["verify_trigger_seen"] = triggers
        _write_state_json_atomic(task_dir, sidecar)
    finally:
        release_lock(fd)
    return due


def record_hook_signal(task_dir: str, mismatches: list[dict]) -> dict:
    """钩子层检出哈希信号：写 pending_signal + journal 留痕（确定性，不起 LLM）。"""
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files = sorted({m.get("file", "?") for m in mismatches})
    signal = {"source": "subagent-stop-hook", "files": files,
              "count": len(mismatches),
              "reason": "round 锚点哈希不匹配（钩子确定性检出，待编排层 triage）",
              "ts": now}
    set_pending_signal(task_dir, signal)
    block = ("\n```yaml\nsignal:\n  - source: subagent-stop-hook\n"
             f"    files: {json.dumps(files, ensure_ascii=False)}\n"
             f"    count: {len(mismatches)}\n"
             f"    ts: \"{now}\"\n```\n")
    _append_journal_section_block(task_dir, FREEZE_SIGNAL_HEADING, block)
    return signal


def stop_hook_round_check(task_dir: str) -> dict | None:
    """subagent-stop 钩子入口：round 锚点校验，检出哈希信号则落 pending_signal +
    journal 留痕。返回 signal（有信号）或 None。纯确定性，不起 LLM。"""
    if not should_verify(task_dir, "round"):
        return None
    mismatches = verify_freeze_hashes(task_dir)
    if not mismatches:
        return None
    return record_hook_signal(task_dir, mismatches)


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
STATE_JSON_SCHEMA_VERSION = "2"

# sidecar 顶层预留字段（与 orch 并列）：冻结清单缓存 + 信号/提案提示位。
# 均无保护，只放缓存与提示类字段——任何安全判定不得以 sidecar 为唯一依据。
_SIDECAR_TOP_DEFAULTS = {
    "frozen_paths": lambda: [],   # list[str]：冻结文件 realpath 缓存（真相源=工作底稿）
    "pending_signal": lambda: None,    # dict|null：钩子检出的哈希信号标记
    "pending_proposal": lambda: None,  # dict|null：执行者变更提案
}


def _migrate_sidecar(sidecar: dict) -> dict:
    """迁到 schema v2：回填缺失的顶层预留字段（缺则置默认），bump schema_version。
    幂等——旧 v1 sidecar 读取后经此补全，不炸。原地修改并返回同一 dict。"""
    sidecar["schema_version"] = STATE_JSON_SCHEMA_VERSION
    for k, factory in _SIDECAR_TOP_DEFAULTS.items():
        if k not in sidecar:
            sidecar[k] = factory()
    return sidecar


def _write_frozen_paths_sidecar(task_dir: str, frozen_paths: list[str]) -> None:
    """持 MUTATION 锁刷新 sidecar frozen_paths 缓存（并顺手迁到 v2）。"""
    lockfile = os.path.join(task_dir, ".state.lock")
    fd = acquire_lock(lockfile, "MUTATION")
    try:
        sidecar = _migrate_sidecar(_read_state_json(task_dir) or {})
        sidecar["frozen_paths"] = frozen_paths
        _write_state_json_atomic(task_dir, sidecar)
    finally:
        release_lock(fd)


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
    p_verify.add_argument("--explain", action="store_true",
                          help="归因输出（integrity_broken 判定 + 自由文本归因）")
    p_verify.add_argument("--file",
                          help="--explain 时指定要归因的冻结文件（相对 task-dir）")

    p_update = sub.add_parser("update-freeze", help="更新工作底稿的冻结哈希")
    p_update.add_argument("--task-dir", required=True)
    p_update.add_argument("--files", required=True, nargs="+",
                          help="要更新哈希的文件名（相对 task-dir）")

    p_val = sub.add_parser("validate-schema", help="校验 YAML schema")
    p_val.add_argument("--type", required=True, choices=["evidence", "progress8"])
    p_val.add_argument("--input", required=True, help="YAML 文件路径")

    p_freeze = sub.add_parser("freeze-journal", help="冻结 journal 并更新工作底稿哈希（finish 第 0 步）")
    p_freeze.add_argument("--task-dir", required=True)

    p_amend = sub.add_parser("amend-freeze", help="受控重冻结（原子；exec 阶段拒绝）")
    p_amend.add_argument("--task-dir", required=True)
    p_amend.add_argument("--file", required=True, help="冻结文件（相对 task-dir）")
    p_amend.add_argument("--reason", required=True, help="变更理由一句话")
    p_amend.add_argument("--approver", required=True, choices=["human", "triage-auto"])

    p_triage = sub.add_parser("triage", help="信号分诊：完整性 → diff → 盲审输入 / 收尾入账")
    p_triage.add_argument("--task-dir", required=True)
    p_triage.add_argument("--file", required=True, help="出信号的冻结文件（相对 task-dir）")
    p_triage.add_argument("--review-verdict", choices=list(TRIAGE_VERDICTS),
                          help="给定则收尾入账；不给则输出 step0+diff+盲审输入")

    p_sv = sub.add_parser("should-verify", help="校验节奏判定（三锚点恒真 + 周期抽查）")
    p_sv.add_argument("--task-dir", required=True)
    p_sv.add_argument("--context", required=True,
                      choices=["gate", "resume", "round", "finish"])

    p_propose = sub.add_parser("propose", help="执行者变更提案（写 sidecar pending_proposal）")
    p_propose.add_argument("--task-dir", required=True)
    p_propose.add_argument("--found", required=True, help="发现了什么，一句话")
    p_propose.add_argument("--change", required=True, help="建议改哪个字段/条目，改成什么")
    p_propose.add_argument("--evidence", default="", help="journal evidence_id 列表（逗号分隔，可空）")
    p_propose.add_argument("--round", required=True, help="第几轮（整数）")

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

    p_auc = sub.add_parser(
        "audit-chain",
        help="终审轨迹审计（追认链衔接 + 停点窗口核对 + 放松检测 + v1→vN diff）")
    p_auc.add_argument("--task-dir", required=True)

    p_clp = sub.add_parser("clear-proposal",
                           help="清除 sidecar pending_proposal（决策卡消费后）")
    p_clp.add_argument("--task-dir", required=True)

    args = parser.parse_args()
    if args.cmd == "verify-freeze":
        if args.explain:
            if not args.file:
                parser.error("verify-freeze --explain 需要 --file")
            result = explain_freeze(args.task_dir, args.file)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(1 if result["integrity_broken"] else 0)
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
        freeze_files(args.task_dir, args.files)
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
    elif args.cmd == "amend-freeze":
        try:
            result = amend_freeze(args.task_dir, args.file, args.reason,
                                  args.approver)
        except AmendPhaseError as e:
            print(json.dumps({"error": str(e), "kind": "phase"},
                             ensure_ascii=False), file=sys.stderr)
            sys.exit(2)
        except AmendIntegrityError as e:
            print(json.dumps({"error": str(e), "kind": "integrity"},
                             ensure_ascii=False), file=sys.stderr)
            sys.exit(3)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)
    elif args.cmd == "triage":
        if args.review_verdict:
            try:
                result = triage_finalize(args.task_dir, args.file,
                                         args.review_verdict)
            except AmendPhaseError as e:
                print(json.dumps({"error": str(e), "kind": "phase"},
                                 ensure_ascii=False), file=sys.stderr)
                sys.exit(2)
            except AmendIntegrityError as e:
                print(json.dumps({"error": str(e), "kind": "integrity"},
                                 ensure_ascii=False), file=sys.stderr)
                sys.exit(3)
            print(json.dumps(result, ensure_ascii=False))
            sys.exit(0 if result["disposition"] == "auto_ratify" else 1)
        result = triage(args.task_dir, args.file)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1 if result["step0"] == "integrity_broken" else 0)
    elif args.cmd == "should-verify":
        due = should_verify(args.task_dir, args.context)
        print(json.dumps({"verify": due, "context": args.context},
                         ensure_ascii=False))
        sys.exit(0 if due else 1)
    elif args.cmd == "propose":
        try:
            proposal = propose_change(args.task_dir, args.found, args.change,
                                      args.evidence, args.round)
        except ProposalExistsError as e:
            print(json.dumps({"error": str(e), "kind": "exists"},
                             ensure_ascii=False), file=sys.stderr)
            sys.exit(3)
        except ValueError as e:
            print(json.dumps({"error": str(e), "kind": "validation"},
                             ensure_ascii=False), file=sys.stderr)
            sys.exit(2)
        print(json.dumps(proposal, ensure_ascii=False))
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
    elif args.cmd == "audit-chain":
        result = audit_chain(args.task_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result["passed"] else 1)
    elif args.cmd == "clear-proposal":
        clear_proposal(args.task_dir)
        sys.exit(0)


if __name__ == "__main__":
    main()
