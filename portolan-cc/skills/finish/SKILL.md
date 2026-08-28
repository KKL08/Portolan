---
name: finish
description: 长程任务终审——新会话独立验收，不带执行期上下文。等同 /portolan:long-horizon-task finish 的直达入口
---

## 入口

`/portolan:finish`，可带任务目录参数：`/portolan:finish .portolan/my-task/`

不带参数：扫 `.portolan/*/工作底稿.md` 找状态为"执行中"的任务。多个则列出让用户选。

## 规程

读 [finish 规程](../long-horizon-task/references/finish.md) 并严格执行。

读全件：任务协议单、工作底稿、批注区、journal、rubric.md（如存在）。
