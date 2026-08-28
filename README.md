# Portolan

> 长程任务管理 · Claude Code 插件市场
> Long-horizon task management for Claude Code.（插件交互为中文）

让 agent 跑长任务时不偏离目标、不假报完成、跑完能独立复核。开工前对齐目标、冻结契约，执行中机械判停，跑完换一个干净会话独立验收。

「Portolan」是古代为长途航海导航的海图——这个插件为 agent 的长程任务导航。

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
| portolan-dsh | — | DSH 轨，另仓（规划中） |

## 许可

MIT
