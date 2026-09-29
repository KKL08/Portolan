"""portolan hook 共用的任务定位。

hook 进程的 cwd 跟随 Claude 当前目录漂移（cd 进任务目录、进 worktree），
所以候选根取三处：会话项目根 CLAUDE_PROJECT_DIR、cwd、cwd 自身或最近一个
含 .portolan/ 的祖先目录。每个根下认 <根>/.portolan/*/工作底稿.md 与
<根>/*/.portolan/*/工作底稿.md 两种布局。"""
import glob
import os

_WORKSHEET = "工作底稿.md"


def _nearest_portolan_ancestor(cwd):
    d = os.path.abspath(cwd)
    while True:
        if os.path.isdir(os.path.join(d, ".portolan")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def candidate_worksheets(cwd=None):
    """返回所有候选任务的工作底稿路径：按 CLAUDE_PROJECT_DIR → cwd → 祖先的
    顺序，同一文件（按真实路径判重）只出现一次。不判断任务状态。"""
    if cwd is None:
        cwd = os.getcwd()
    roots = []
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if project:
        roots.append(project)
    roots.append(cwd)
    ancestor = _nearest_portolan_ancestor(cwd)
    if ancestor:
        roots.append(ancestor)

    seen_roots, seen, out = set(), set(), []
    for root in roots:
        real_root = os.path.realpath(root)
        if real_root in seen_roots:
            continue
        seen_roots.add(real_root)
        for pattern in ((".portolan", "*", _WORKSHEET),
                        ("*", ".portolan", "*", _WORKSHEET)):
            for ws in sorted(glob.glob(os.path.join(root, *pattern))):
                key = os.path.realpath(ws)
                if key not in seen:
                    seen.add(key)
                    out.append(ws)
    return out
