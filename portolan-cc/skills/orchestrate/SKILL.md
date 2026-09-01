---
name: orchestrate
description: 长程任务编排循环——主 session 自动派发执行 subagent、监控终态、按档位决策重派。等同编排规程的直达入口
---

## 入口

`/portolan:orchestrate`，可带任务目录参数：`/portolan:orchestrate .portolan/my-task/`

不带参数：扫 `.portolan/*/工作底稿.md` 找状态为"执行中"的任务。多个则列出让用户选。

## 规程

读 [orchestrate 规程](../long-horizon-task/references/orchestrate.md) 并严格执行。

铁律：判停热路径无 LLM——所有分支判定只依赖 state-guard 输出与终态字面量。
