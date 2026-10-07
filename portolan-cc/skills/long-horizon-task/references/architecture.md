# 架构与组件速查

> 定位：portolan skill 的 reference。让 agent 了解插件的完整组件版图，知道哪些工具可用、文件在哪、彼此怎么协作。具体操作规程见各模式 reference（dispatch / orchestrate / continue / finish / trial-run / eval-design-guide）。

## 整体分层

portolan 管三段：**准备 → 分发 → 验收**。执行由 CC 原生 subagent 在隔离上下文完成。

```
用户
 │ /portolan:long-horizon-task …
 ▼
┌──────────────────────────────────────────────┐
│ skill portolan（主 session）                  │
│  发起模式 → trial-run → dispatch → orchestrate │
│  continue（续跑）    finish（终审）            │
└──────────┬───────────┬───────────┬────────────┘
           │调用       │派 subagent │读写任务目录
           ▼           ▼           ▼
     state-guard    执行 subagent   .portolan/<slug>/
     (21 子命令)    (opus, 隔离)    (五件套 + sidecar)
           ▲           │派 worker   ▲
           │           ▼            │
           │触发   worker（有界）   │block/恢复
     四类 hook ─────────────────────┘
```

## 组件清单

### 1. skill portolan（入口）

路径：`skills/long-horizon-task/SKILL.md` + `references/` + `templates/`

三模式：
- **发起**（`/portolan:long-horizon-task`）：摸底 → 聚焦追问 → 选档 → 确认协议 → 试跑 → 分发 → 编排
- **续跑**（`/portolan:continue [<目录>]`）：读停点 → 判断类型 → 组装续跑产物 → 重新进入编排
- **终审**（`/portolan:finish [<目录>]`）：换会话派独立 subagent 做两阶段验收

### 2. state-guard（确定性工具层）

路径：`bin/state-guard`，21 个 CLI 子命令。

| 子命令 | 用途 | 典型调用者 |
|---|---|---|
| `verify-freeze` | 核对冻结哈希（含必核文件白名单） | orchestrate、finish 第 0 步 |
| `update-freeze` | 写入/更新冻结哈希（执行期拒绝已冻结文件的内容变更重冻，走 amend-freeze） | 准备期、dispatch 首冻 |
| `validate-schema` | YAML schema 校验 | evidence 格式检查 |
| `freeze-journal` | 冻结 journal（finish 前） | finish 第 0 步 |
| `amend-freeze` | 受控追认重冻结（原子；exec 阶段拒绝） | triage 自动追认、continue 人批入账 |
| `triage` | 信号分诊：完整性前置 → diff → 盲审输入 / 收尾入账 | orchestrate（哈希信号处置） |
| `should-verify` | 校验节奏判定（三锚点恒真 + 按档位抽查） | orchestrate |
| `propose` | 执行者变更提案（写 sidecar pending_proposal） | 执行 subagent |
| `clear-proposal` | 清除已消费提案（幂等） | continue 决策卡 |
| `orch-get` | 读编排状态（JSON，sidecar 权威） | orchestrate、continue |
| `orch-set` | 写编排状态单字段（MUTATION 锁） | orchestrate |
| `verify-blocked` | 验证"被阻塞"终态（exit 0/1/2） | orchestrate |
| `evidence-delta` | attempt 边界 evidence 增量对比 | orchestrate |
| `checklist-flips` | 验收项翻转计数 | orchestrate |
| `anchor-check` | 量尺锚定校验（未锚定拒收） | finish 第 0.5 步 |
| `run-check` | 亲跑验收命令 + 把 evidence 写进 journal | 执行 subagent |
| `evidence-list` | 列出 journal 全部 evidence（JSON） | finish 第一阶段 |
| `declare-terminal` | 声明命名终态（写 journal + sidecar） | 执行 subagent |
| `clear-terminal` | 清除 sidecar 最新终态（派下一 attempt 前） | orchestrate、continue |
| `audit-chain` | 终审审计：追认链衔接 + 停点窗口 + 放松检测 + 无主漂移 + v1→vN diff | finish 第二阶段 |
| `orch-step` | 编排决策：从盘上状态推出下一动作（只决策不执行、幂等，每次决定留痕 `decisions.jsonl`） | orchestrate |

所有子命令通过 `state-guard <子命令> --task-dir <任务目录> [参数]` 调用。

### 3. 任务目录（五件套 + sidecar）

位置：`.portolan/<slug>/`

| 文件 | 写手 | 作用 |
|---|---|---|
| `任务协议单.md` | portolan 三模式 | 唯一正式协议：成功画像、验收清单、不可逆点、档位 |
| `工作底稿.md` | portolan 三模式 | 环节清单、起点事实、编排状态投影、冻结哈希、运行参数 |
| `execution.md` | portolan（dispatch） | 执行环工作规程：六步节拍、熔断规则、终态五值 |
| `journal.md` | 执行 subagent | 执行记录：散文 + 结构化 evidence + 终态声明 |
| `rubric.md` | 发起模式、finish | 软 eval 评审标准（有软 eval 才生成；执行者不读） |
| `批注区.md` | 用户、portolan | 停点期间的人机批注（执行者只读） |
| `ledger.md` | finish、continue | 终审判定记录 |
| `state.json` | state-guard 独占 | sidecar——编排状态权威源，16 字段 + 终态四字段 |
| `checks/` | run-check | 验收命令的输出日志目录 |
| `decisions.jsonl` | orch-step 独占（append-only） | 编排决定留痕，不入冻结面 |
| `hook-events.jsonl` | hook 层（PreToolUse 拦截留痕、SubagentStop 压缩计数） | hook 事件留痕：triage 归因读取；带 `decision` 字段的事件计入校验升频 |

worker（执行环派出的并行分工 subagent）不写上表任何文件，也不读 `rubric.md`；它的结论经执行环合并、亲跑 run-check 后才进 journal。

### 4. 模板

路径：`skills/long-horizon-task/templates/`

7 个模板文件：`任务协议单.md` / `工作底稿.md` / `execution.md` / `journal.md` / `rubric.md` / `ledger.md` / `批注区.md`。发起模式按模板 + 用户输入生成实际文件。

### 5. 四类 hook

路径：`hooks/`，由 `hooks.json` 声明，插件系统自动注册。

| Hook | 触发时机 | 行为 |
|---|---|---|
| **Stop** | 主 session 退出 | 有执行中任务且 <2h → 真 block；ScheduleWakeup 类 stop 放行（兼容 /loop） |
| **SessionStart** | 新会话启动；上下文压缩后 | 有未完成任务 → 打印恢复提示；压缩后输出再入卡（状态摘要、目标首行，再列出要亲自读回的原文：编排规程、成功画像、批注区，已派出未回报的 subagent 先等回报，最后跑 orch-step） |
| **SubagentStop** | 执行者结束（matcher 限定 `portolan:execution-loop`，worker 与其他 subagent 不触发） | round 锚点哈希校验，检出信号落 pending_signal + journal 留痕；数执行者 transcript 的压缩次数，非零则向 `hook-events.jsonl` 追加 `executor_compaction` 事件（不计入升频）；不向任何上下文输出文字 |
| **PreToolUse** | 工具调用前 | 守卫式检查 |

所有 hook 遵循 fail-open：portolan 不成为妨碍者。

### 6. reference 文档（操作规程）

| 文件 | 覆盖什么 |
|---|---|
| `trial-run.md` | 试跑三必测 + 按需项 |
| `dispatch.md` | execution.md 组装 + /goal 命令文本 + 冻结哈希 |
| `orchestrate.md` | attempt 循环 + 判定序 + 终态处理分支 + 触发检测 |
| `continue.md` | 停点续跑：读四件 → 判断类型 → 组装 → 重入编排 |
| `finish.md` | 两阶段协议：盲验 → 按需调查 → 判定 |
| `eval-design-guide.md` | 验收清单设计：eval 三分流 + rubric 编写 |
| `triage-review.md` | 哈希信号盲审：方向裁决（tighten/equivalent/loosen/redirect）提示词 |
| `architecture.md`（本文件） | 组件版图速查 |

### 7. agent 定义

路径：`agents/`，插件系统默认扫描，命名空间为插件名。

| agent | 谁派 | 作用 |
|---|---|---|
| `portolan:execution-loop` | 编排者（`dispatch_next_attempt`、`recover_subagent` 的 cold_restart） | 执行环。system prompt 只装身份、五条硬规则、续接后先重读清单；规程正文在任务目录 execution.md |
| `portolan:worker` | 执行环 fan-out；试跑按需项 7 验路径 | 有界 worker，不能再派 subagent（`disallowedTools: Agent`）；改代码的由派发方按次加 `isolation: worktree`；回报五项事实，不下完成或通过结论 |

system prompt 是 subagent 上下文里唯一不被压缩摘要的位置。两个 agent 正文全部静态（不含任务目录、日期、attempt），执行者压缩续接后按正文清单重读 execution.md、任务协议单、批注区与 journal 取回规程。同时活跃 worker 数的硬限在 execution.md「并行建议」节（底稿运行参数「并行上限」，合法 1–8，默认 6）。

## 判定四层

终态"能不能收、停在哪、通没通过"分四层，任何一层可否决，通过则接力向上：

1. **执行者自判**（转向权 + 认输权）——声明命名终态
2. **机械判定**（准入权）——主 session + state-guard 子命令出 pass/fail
3. **语义 review**（建议权，选装）——触发检测
4. **finish 终局验收**（裁决权）——独立 subagent 亲跑重跑

**权威链**：用户 > 冻结协议 > finish > 机械闸 > 语义 review > 执行者自判

**终态校验是纯代码**——终态是否有效、证据是否合规、哈希是否完整走 state-guard 确定性判定，不走 LLM 推理。LLM 广泛参与任务生命周期各环节，但终态校验这一环节必须是纯代码。

## 量尺锚定链

协议验收命令 → run-check 复制亲跑 → evidence 落 journal → checklist-flips 计数 → anchor-check 审计 → finish 亲跑重跑。执行者从不手写命令或 exit code。

## 命名约定（单一来源）

进 hash / 接口 / wire 的契约标识符以本节为唯一来源，各文件引用它、不另造词：

- **终态五值**（封闭枚举，`TERMINAL_STATES`）：`完成 / 无事可做 / 被阻塞 / 需批准 / 无进展`
- **生命周期状态四值**：`准备中 / 执行中 / 已收尾 / 已归档`
- **自动化档三值**：`全自动 / 平衡 / 保守`
- **eval 三分流**：`硬 eval / 软 eval / 人工点`
- **裁判层级四级**（按序、不可倒置）：条件核对 → 机械验证 → 质量评审 → 人工判定
- **置信三标**：`✅` 稳 / `🟡` 存疑 / `🔴` 未验证
- **编排状态字段**：state-guard `ORCH_FIELDS`（16 字段，见 state-guard 与 implementation spec）
- **verifier_id 类型**：`finish-session-{uuid}` / `subagent-{uuid}` / `user:{git_username}` / `run-check-{uuid8}`
- **结构化字段**：验收命令 `- 验收命令：\`X\``；复核命令 `复核方式：\`X\``（反引号硬要求）

## 底线规则

动这些需要充分理由和用户同意：
1. 单写者规则（谁能写什么，见上方文件表）
2. finish 独立性（亲跑重跑，不采信自报）
3. 协议冻结（SHA-256 哈希，finish 第 0 步核对；含声明的评分标准文件）
4. 终态五值封闭枚举（完成/无事可做/被阻塞/需批准/无进展）
5. 执行者与 worker 不读 rubric，执行者派 worker 时也不转述其内容（防信息污染 finish）
