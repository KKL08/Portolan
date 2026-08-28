# Portolan

> 长程任务管理 · Claude Code 插件市场
> Long-horizon task management for Claude Code.

Portolan 是古代为长途航海导航的海图——这个插件市场为 agent 的长程任务导航。

## 安装

Claude Code 里两步：

```
/plugin marketplace add KKL08/Portolan
/plugin install portolan@portolan
```

需要 Python 3.10+（`state-guard` 用到 `fcntl`，仅 macOS / Linux）和 PyYAML。

## 插件

| 插件 | 位置 | 状态 |
|---|---|---|
| **portolan** | [`portolan-cc/`](portolan-cc/) | Claude Code 轨，可用。详见 [portolan-cc/README](portolan-cc/README.md) |
| portolan-dsh | — | DSH 轨，另仓（规划中） |
