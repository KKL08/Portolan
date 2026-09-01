# continue 停点续跑

> 定位：portolan skill 的 reference。执行环停在某个终态后，用户回来续跑。

> **与 orchestrate.md 的边界**：要人拍板的续跑走本篇（决策卡三态 approve/reject/defer——需批准、被阻塞 confirmed、finish 不通过找人等停点）；无需人的自动重派（无终态恢复、假阻塞拒收重派、无进展换思路）走 orchestrate.md，不进本篇。

## 第 0 步：核对冻结哈希（resume 必查锚点）

**恢复是三个必查校验锚点之一**（另两个：gate 停点前、finish 全量），不随校验档位
变——冷启动、compact 热恢复、长停摆续跑都必须先核对。**在读任何协议文件之前**，
先跑 state-guard 核对四文件（任务协议单 / rubric / execution.md / journal）冻结哈希：

```bash
state-guard verify-freeze --task-dir .portolan/<slug>/
```

- 退出码 0 → 协议未被篡改，继续入口
- 退出码 1 → 报错 hash mismatch，**阻止推进**；先对出信号的文件跑
  `state-guard verify-freeze --task-dir .portolan/<slug>/ --explain --file <相对路径>`
  拿归因（integrity_broken 直达人工），再按下方"信号分诊"处置，把 diff 摆给用户等决策
- state-guard 未装机 → 退化为口头提醒"请人工核对任务协议单/rubric/execution.md
  从上一模式退出后是否被改过"，不强制阻断

## 信号分诊（pending_signal 消费声明）

**编排层派发下一 attempt 前，见 sidecar `pending_signal` 非空必先走 triage 流程**，
不得直接重派：

```bash
state-guard triage --task-dir .portolan/<slug>/ --file <出信号文件>
```

- `step0 == integrity_broken` → 直达人工，不盲审（快照或记录本身可疑）
- `triage_mode == manual` → 全部信号直接找人，不盲审
- 否则（assisted + 完整）→ 按 `review_inputs` 派盲审 subagent（协议见
  `references/triage-review.md`），拿到方向后
  `state-guard triage --file <path> --review-verdict <tighten|equivalent|loosen|redirect>`
  收尾：tighten/equivalent 自动追认继续跑，loosen/redirect 置 gate 停点摆给人
- 熔断：自上次人批 amend 以来 triage-auto 连续 3 条，第 4 次信号无条件转人工

pending_signal 是提示位（无安全依赖），被删则下一锚点重新检出、journal 已留痕，
不构成绕过。

## 生成"当前进展 8 项"结构化恢复包

生成结构化 8 项恢复包（无需全量重读工作底稿/批注区/journal）：

```yaml
current_progress:
  goal_revision: <任务协议单当前 revision hash>
  work_items:
    accepted: [<已通过的 work item id 列表>]
    pending: [<未推进的 work item id 列表>]
  last_decision: <最近一次决策卡 approve/reject/defer>
  active_approvals: [<待批的 approval 详情列表>]
  verified_facts:
    - fact: <事实描述>
      fresh_until: <ISO 8601 时间戳>
  unknown_outcomes: [<外部动作结果未知的清单>]
  next_action: <下一条合法动作描述>
  needs_reverify: [<需要重新验证的证据 evidence_id 列表>]
```

**8 项必须齐全**；缺项时执行环拒绝执行并要求补齐。

`verified_facts` 除自然语言事实，还应带**机读判据要点**——测试对产物的实现级硬约束（如"pattern 须恰含一个捕获组，否则 findall 返回 tuple"、"输出必须是 UTF-8 无 BOM"这类）。冷启动接手者只有恢复包、够不着测试源时，缺这些要点会踩坑（B2 dogfood 教训）。

用 state-guard 校验：
```bash
state-guard validate-schema --type progress8 --input current_progress.yaml
```

## 入口

`/portolan:continue`，可带任务目录参数：`/portolan:continue .portolan/my-task/`

不带参数：扫 `.portolan/*/工作底稿.md` 找状态为"执行中"的任务。多个则列出让用户选，一个则直接进入。

## 审计

读四件文件：任务协议单、工作底稿、批注区、journal。

- journal 缺轮只标注（"第 X 环节后无记录"），**不做取证式补建**——缺了就是缺了，finish 会据此判
- 终态声明缺应有细节的（需批准无待批清单、被阻塞无复核方式）视同执行记录缺陷——同缺轮一样只标注不代写，finish 据此判
- 底稿状态不是"执行中"→ 告诉用户当前状态，确认是否要继续

## 按终态分流

读 journal 最新一条终态声明，按类型处理：

### 需批准

两种子类型：

**不可逆动作**：摆详情——要做什么、为什么不可逆、有没有替代方案。该条目有前置条件的，continue 先亲手重跑核验命令，实测结果与详情一并摆给用户，前置不满足则不进入批准。同协议内的后续不可逆点可在同次停点逐项摆详情、逐项批准，批注区各记一行。用户点头后记批注区"已批准 + 日期"，续跑。批准的动作由续跑的 `/goal` 执行环执行，continue 会话本身不执行。

**人工验收点**：摆证据给用户判——环节产出、eval 结果、参照解对比。用户判过/不过，记批注区。

**变更提案（sidecar `pending_proposal` 非空）**：执行者发现目标/方案不合理，挂了结构化
提案。摆决策卡三态给用户：

```
【决策卡】执行者提了变更提案，怎么办？
要决什么：{{提案 found + change 一句话}}
证据：{{pending_proposal.evidence 的 evidence_id 列表}}
推荐 {{A/B/C}}（理由：{{按提案与证据判断}}）
A. approve：认可，按提案改冻结文件并追认入账
B. reject：不认可，回发起修订
C. defer：先搁置，标被阻塞下次再议
回复 A 或 B 或 C。
```

- **approve** → 冻结文件是人批改动，走停点窗口内的受控变更（本期一律人批，不代批）：
  ```bash
  # ① 切出 exec（amend-freeze 在 exec 期拒绝执行，合法 amend 只在停点窗口）
  state-guard orch-set --task-dir .portolan/<slug>/ --field phase --value verify
  # ② 人在编辑器按提案改冻结文件（continue 会话本身不代改内容）
  # ③ 受控重冻结，追认条目入 journal（approver=human）
  state-guard amend-freeze --task-dir .portolan/<slug>/ --file <改的文件> \
    --reason "<提案摘要>" --approver human
  # ④ 清掉已消费的提案，再切回 exec 续跑
  state-guard clear-proposal --task-dir .portolan/<slug>/
  state-guard orch-set --task-dir .portolan/<slug>/ --field phase --value exec
  ```
  批注区记一行"提案已批准 + 日期 + 改了什么"，然后按"续跑"重派。
- **reject** → `state-guard clear-proposal` 清提案，回"修订"流程（改协议或驳回理由记
  批注区），不改冻结文件。
- **defer** → 标"被阻塞"，**保留 pending_proposal 不清**（下次 continue 再议），等用户。

### 被阻塞

按 journal 终态声明的复核方式亲手重跑核验（声明里没写复核方式的，自己从依据推导并标注记录缺陷）：
- 通了 → 续跑
- 还没通 → 告诉用户，等下次 continue

### 无进展

摆收尾报告（journal 终态声明下的收尾报告内容），和用户定下一步：

```
【决策卡】无进展，怎么办？
要决什么：执行者连续多轮没有新进展，下一步怎么走。
为什么现在问：继续跑大概率还是空转。
推荐 A（理由：{{根据收尾报告判断最可能的出路}}）
A. 改协议：修正成功标准或 eval 设计，重新分发
B. 换路子：保持目标不变，改执行策略（如拆更小步、换工具、手动辅助某环节）
C. 放弃：体面收尾
回复 A 或 B 或 C。
```

### 完成

提示用户该走 finish，不在 continue 里验收。

### 无事可做

仅在协议明确了空轮语义（成功画像写了"输入为空时无事可做是正常收束"）时视为正常单轮结束。确认是否还要再跑一轮（重新分发），或者进 finish。协议未约定空轮语义的，视同异常摆给用户。

### 无终态声明

轮次耗尽（`or stop after N turns`）、会话中断、权限提示悬置等残局：journal 环节表有新进展，但终态声明区没有对应轮次/日期的声明（按日期与轮次比对，不按文件行序判）。审计 journal 进展与环境实况，摆给用户后按情况归入续跑或无进展决策卡。

## 修订与续跑

### 修订

修订记 delta 进底稿修订记录：改什么 / 为什么 / 谁批准。

触及任务协议单四字段（成功画像 / 验收清单 / 不可逆点 / 可靠性档位）的修订 → 走协议修订流程：记决议记录条目，走与初次确认同等分量的确认。凡写入任务协议单/rubric.md 后，重算冻结哈希更新进底稿起点事实表（见 SKILL.md 冻结哈希重算规则）。

### 续跑

approve 续跑第一步：跑 `state-guard orch-step --task-dir <任务目录> --context resume`，照返回 action 执行（resume 是恒查锚点，冻结哈希核对由它代办）。以下手动步骤仅在 orch-step 指引缺位时兜底：

重派新 attempt 前先解 verify 闸（finish 判不通过后 phase 停在 verify，不解则新执行者
run-check/declare-terminal 全被拒）：
```bash
state-guard orch-set --task-dir .portolan/<slug>/ --field phase --value exec
```
phase 本就是 exec 的（普通停点续跑，finish 没跑过）时此命令幂等无副作用。

再按 `references/dispatch.md` 重新组装执行产物：
- execution.md 有修订的同步更新
- 重新打印 `/goal` 命令给用户

### 放弃

用户要放弃的，体面收尾：
- ledger 如实记"中途放弃 + 原因"
- 底稿状态改"已归档"
- 不删任务目录（留作参考）
