---
name: continue
description: 长程任务停点续跑——执行环停在某个终态后，用户回来续跑。等同 /portolan:long-horizon-task continue 的直达入口
---

## 入口

`/portolan:continue`，可带任务目录参数：`/portolan:continue .portolan/my-task/`

不带参数：扫 `.portolan/*/工作底稿.md` 找状态为"执行中"的任务。多个则列出让用户选，一个则直接进入。

## 规程

读 [continue 规程](../long-horizon-task/references/continue.md) 并严格执行。
