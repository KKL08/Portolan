# orchestrate：父 session 编排规程

> 读者：dispatch 组装完成后的父 session（portolan 三模式之一）。
> 边界：要人拍板的续跑走 continue.md（决策卡三态）；本篇只管无需人的自动路径。
> 铁律：判停热路径无 LLM——本篇所有分支判定只依赖 state-guard 输出与终态字面量。
> 铁律：派 subagent 的唯一正当理由是需要干净独立上下文（execution / finish）；机械检查一律 state-guard。
> 铁律：本编排激活期间勿开 watch.py daemon（两者对同一终态各自反应会双重派发）——watch 是无人值守 fallback，与本篇父 session 编排互斥。

## 编排循环（attempt 循环）

前置：五件套已组装、冻结哈希已写、任务协议单自动化档位已签、编排状态已初始化
（`state-guard orch-set --field tolerance_tier --value <档位>`）。

每个 attempt：

1. **派执行 subagent**（background，run_in_background=true）：prompt = execution.md 全文
   + 任务目录路径 + 当前进展 8 项（首 attempt 为空）。派发前先
   `state-guard clear-terminal --task-dir X` 清掉上一轮终态（防旧终态滞留：
   否则重派后新执行期仍被 stop-hook 按陈旧"需批准"误放行、watch 重复拉起），
   再 attempt 计数 +1（`orch-set --field attempt`）。
2. **等 task-notification 唤回**。期间父 session 可与用户交互；用户新指令写批注区，
   不直接转发给运行中的 subagent（禁止向运行中的 subagent 注入指令）。
3. **subagent 返回 → 机械判定序**（按序执行，命中即分支）：

   a. `state-guard verify-freeze --task-dir X`——冻结哈希核对，不过 → 停，通知用户（疑似篡改）
   b. 读 journal 最新终态（无终态 → 见"无终态返回"）
   c. 终态分支：
      - **完成** → `checklist-flips`：satisfied == 硬 eval 验收项总数 → 派 finish subagent（见下）；未达则拒收，带缺项清单重派（连续 3 次未达则升级通知用户，防死循环）。注：anchor-check 不参与完成门判定，仅用于 finish 期审计（拒收对不上协议的作弊 evidence）
      - **被阻塞** → `verify-blocked`：
        - exit 1（refuted，假阻塞）→ 拒收终态，带驳斥理由重派；连续 refuted 达 2 次 → 升级通知用户
        - exit 0（confirmed）→ 按档位：全自动=记录并退避重试外部依赖（上限 3 次）；平衡/保守=通知用户等 continue
        - exit 2（unverifiable）→ 一律升级通知用户
      - **需批准** → 一律通知用户等 continue（硬底线）
      - **无进展** → `evidence-delta`：确有零增量 → 按档位（全自动=换思路重派上限 2 次后找人；平衡/保守=找人）；有增量（执行者误判）→ 带增量事实重派
      - **无事可做** → 仅协议有空轮语义时收束，否则视为异常终态升级人
   d. 每次接受终态后：`evidence-delta --update`、`checklist-flips --update`、
      `orch-set --field terminal_watermark --value <终态>@<journal mtime>`

## 无终态返回（subagent 溜号/中断）

先 SendMessage 原地恢复（保留其上下文）："检查你的任务状态，继续执行直到声明命名终态"。
恢复失败或再次无终态返回 → 派新 subagent 冷启动（prompt 加当前进展 8 项，
从 journal 与工作底稿重建）。计入 MAX_BLOCKS（上限 2，超限 fail-open 通知用户）。

## 结果分类与门控优先级序（高压低，永不反向）

user_stop（用户主动停，永不自动覆盖）
> 健康类（auth 失败 / context overflow / API 配额 → quota 退避 60s 起指数回退，不计 MAX_BLOCKS）
> 人工决策类（需批准 / 被阻塞 confirmed）
> quota 退避
> 熔断计数（error 累计 MAX_BLOCKS=2）

## 触发检测（机械件）

每次 attempt 边界顺带计算，命中任一信号 → 对话提示 + 批注区转向建议表各落一条
（"建议人工看一眼：<信号>"），并 `orch-set --field trigger_count` 自增：

- **零翻转连击**：`checklist-flips` 返回 flips==0 且 `evidence-delta` new_count>0，
  连续达 `ZERO_FLIP_K` 个 attempt（zero_flip_streak 由编排维护：flips==0 时自增，>0 时清零）
- **反复换思路**：rethink_count 达 `RETHINK_MAX`（执行者 journal 声明"换思路"时编排自增）
- **兜底**：attempt 计数达 `FALLBACK_EVERY` 的倍数

静音：上次触发以来 evidence 零增量 → 跳过；同信号连续第二次 → 只写批注区不再对话提示。

### 触发阈值（校准参数，单一来源）

| 参数 | 当前默认 | 含义 |
|---|---|---|
| `ZERO_FLIP_K` | 3 | 零翻转连续多少 attempt 才提示 |
| `RETHINK_MAX` | 2 | 换思路累计多少次才提示 |
| `FALLBACK_EVERY` | 5 | 每多少 attempt 兜底提示一次 |

三值是**待校准参数**（backlog C1）：调大 = 漏报晚提示，调小 = 误报乱提示。当前是初始
猜值、未经真实数据校准——**校准前别凭直觉改**，改也只改这张表一处。

### 校准记录与协议

触发每次**起火**时，除自增 trigger_count，向 `.portolan/ledger.md` 追加一条校准样本：
`触发样本 | <日期> | <任务slug> | <信号> | attempt=<n> streak/count=<值> | 值不值得看：<待人/finish 回判>`。

校准流程（攒够样本后执行——本次不做，迄今 0 起火样本）：
1. 收集足量真实起火样本（含"值不值得看"的人工/finish 回判）
2. 算每信号误报率（起火但没问题）与漏报率（真跑偏却没起火）
3. 误报多→调大对应阈值；漏报多→调小。只改上面参数表一处。

## finish 派发

新 finish subagent（干净上下文），prompt 按 finish.md 两阶段协议：
第一阶段输入只给任务协议单 + rubric + evidence 重跑指引 + 代码目录（不给 journal）。
finish 通过 → 呈回执等用户终审；不通过 → 按档位（全自动=带 finish 反馈自动重派 1 次，
再不过找人；平衡/保守=直接找人）。

## 编排状态维护纪律

全部状态经 `state-guard orch-get/orch-set`（字段权威清单=ORCH_FIELDS），
不建任何新 state 文件，不在对话内存里单独记账。
