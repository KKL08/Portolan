# portolan

> portolan 插件市场 · Claude Code
> A Claude Code plugin marketplace for long-horizon agent tasks.（插件交互为中文）

长程任务的准备-分发-验收层。让 agent 跑长任务时不偏离目标、不假报完成、跑完能独立复核。

## 安装

Claude Code 里两步：

```
/plugin marketplace add KKL08/portolan
/plugin install portolan@portolan
```

需要 Python 3.10+（`state-guard` 用到 `fcntl`，仅 macOS/Linux）和 PyYAML。

## 插件

| 插件 | 位置 | 状态 |
|---|---|---|
| **portolan** | [`portolan-cc/`](portolan-cc/) | Claude Code 轨，可用。详见 [portolan-cc/README](portolan-cc/README.md) |
| portolan-dsh | `portolan-dsh/` | DSH 轨，规划中 |

## 许可

MIT
