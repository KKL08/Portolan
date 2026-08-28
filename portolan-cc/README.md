# Portolan

> 长程任务的准备-分发-验收层 · Claude Code 插件
> A preparation–dispatch–verification layer for long-horizon agent tasks in Claude Code.（本插件交互为中文）

让 agent 跑长任务，最怕三件事：跑着跑着偏离目标、把没做完的说成做完了、跑完没人能独立复核。Portolan 不替 agent 干活，只在它外面搭一层管控——开工前把目标和验收标准对齐、冻死，执行中用确定性的机械闸判断能不能停，跑完换一个干净会话独立重验。

## 安装

Claude Code 里两步（本插件通过 `portolan` 插件市场分发）：

```
/plugin marketplace add KKL08/portolan
/plugin install portolan@portolan
```

需要 Python 3.10+（`state-guard` 用到 `fcntl`，仅 macOS/Linux）和 PyYAML。

## 三个入口

| 命令 | 干什么 |
|---|---|
| `/portolan:long-horizon-task` | 发起：摸底、定验收标准、冻结契约、试跑、分发、编排，全流程 |
| `/portolan:continue` | 停点续跑：审批、被阻塞、无进展等停点回来接着跑 |
| `/portolan:finish` | 终审：开一个干净会话独立验收，不带执行期上下文 |

## 怎么用

一句话描述任务，发起：

```
/portolan:long-horizon-task 给 katex 加一个 \coloneqq 命令，jest 测试要过
```

Portolan 会追问几个关键问题，和你敲定成功画像、验收命令，冻结契约，然后派执行 subagent 去做，机械闸盯着终态。跑完或停下后，开新会话终审：

```
/portolan:finish
```

## 它凭什么靠谱

Portolan 的价值在几条硬保证，不靠 agent 自觉：

- **冻结契约**：目标和验收标准开工时存 SHA-256 哈希，执行期偷改当场被抓——agent 不能中途把目标改简单。
- **冻评分标准**：判分逻辑和期望答案一起冻，agent 改不了考卷。
- **独立终审**：finish 在干净会话里**亲自重跑**验收命令，不采信执行者自报的结果。
- **终态校验纯代码**：任务能不能停、证据合不合规、哈希完不完整，全走确定性代码判定，不掺 LLM 推理。
- **单写者规则**：每个文件只有一个写手，谁写什么划死。

## 工作原理

1. **发起** — 摸底任务、定验收清单、选评估策略、冻结契约
2. **试跑** — 分发前先小规模点火，验执行计划可行
3. **分发** — 组装执行规程，冻结基线
4. **编排** — 父会话派执行 subagent、按终态机械判停、失败重派
5. **续跑** — 审批 / 阻塞 / 无进展等停点，人来拍板
6. **终审** — 干净会话盲验 + 程序审计

执行交给 Claude Code 原生的 subagent 和 session，Portolan 只管准备、分发、验收，不造第二套执行引擎。

## 许可

MIT
