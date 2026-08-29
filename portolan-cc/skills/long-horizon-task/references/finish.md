# finish 换会话终审

> 定位：portolan skill 的 reference。全新会话执行，不带执行期上下文——终审不走执行者叙事通道。

## 红线

- 验收只认环境状态和产物，journal 是线索不是证据本体
- "执行 agent 说做完了"不构成任何通过依据
- 不引用 journal 历史输出当通过依据——亲自重跑

## 入口

`/portolan:finish`，可带任务目录参数：`/portolan:finish .portolan/my-task/`

不带参数：扫 `.portolan/*/工作底稿.md` 找状态为"执行中"的任务。多个则列出让用户选。

读全件：任务协议单、工作底稿、批注区、journal、rubric.md（如存在）。

## 终审规程

整条规程是**两阶段**，按步骤走、不跳步：

- **第一阶段（盲验）** = 第 0 步冻结 + 第 0.5 步锚定 + 第一阶段亲自重跑。输入只认
  任务协议单、rubric.md、`evidence-list` 取回的 evidence、代码目录与环境状态，**不读
  journal 正文**，产出初步裁决。
- **第二阶段（调查 + 审计）** = 按需打开 journal 定位 + 来路审计 + diff 审计 + 软 eval
  评审。发现违规**可推翻第一阶段的通过**；反向不成立（第二阶段不能把硬 eval 不过救活）。

## 第一阶段（盲验）

第 0/0.5 步是盲验的准备（冻结验证对象 + 锚定量尺），然后亲自重跑。

### 第零步：冻结 journal + 核对文件哈希

finish 会话开工第一动作：

```bash
# 先冻结 journal（同时把 sidecar phase 置 verify：此后执行侧 run-check /
# declare-terminal 一律被状态机拒绝，exit 2——旧执行者无法再写移动 finish 的验证对象）
state-guard freeze-journal --task-dir .portolan/<slug>/
# 再核对冻结哈希（含刚冻结的 journal 与声明的评分标准文件）
state-guard verify-freeze --task-dir .portolan/<slug>/
```

- 退出码非 0 → finish 判不通过，将不匹配详情摆给用户；不进入后续步骤
- 若 journal 冻结失败（e.g. 执行环正在追加）→ 等 3s 重试一次；仍失败 → 报错
  停在此步等用户处理

verify-freeze 详情：核对工作底稿"冻结哈希"节记录的任务协议单/rubric.md/execution.md 及任务协议单"评分标准基线"声明的**评分标准文件**（判分逻辑 + 期望答案）的 SHA-256 哈希与实际文件内容是否一致。不符 = 协议或评分标准在执行期被篡改：记 ledger 一行（结局 + finish 判定"不通过（协议/标准篡改）"），把哈希差异与可疑改动摆给用户，建议恢复冻结版本后再定重跑与否；不出常规不通过决策卡，不进后续步骤。底稿没有冻结哈希记录的，标注"无冻结基线可核"后继续，不视为不通过。

**评分标准无法按文件冻结时**（标准与工作产物挤在同一文件、或 eval 本质会变的快照/录制模式——任务协议单"评分标准基线"已声明"无独立标准文件"）：verify-freeze 兜不住，finish 第一阶段重跑 evidence 命令时**必须以 finish 独立留存的纯净标准参照**（不采信执行侧那份变成什么样），这是这类任务的唯一防篡改防线。

**若 finish 判不通过**（本步或后续任何一步）：任务进入"需修订"状态，走 continue
模式生成新 execution.md 和新 journal 段；**journal 不解冻**（旧内容只追加不改写）。
continue 重派新 attempt 前需 `orch-set phase exec` 解除 verify 闸，否则新执行者写不进
journal（解 verify 闸只放行追加，不违反"不解冻"）。

### 第 0.5 步：量尺锚定核对

跑 `state-guard anchor-check --task-dir <任务目录>`。
exit 1（存在未锚定 evidence——command_or_method 对不上任务协议单任何验收命令）→
这些 evidence 全部拒收，不得作为任何验收项的支撑；若拒收后某硬 eval 验收项失去
全部 evidence 支撑，finish 直接判不通过（理由：量尺不符）。

### 亲自重跑全部验收检查

**输入只有**任务协议单、rubric.md、`state-guard evidence-list --task-dir <任务目录>` 取回的 evidence JSON、代码目录与环境状态——**不读 journal 正文**。逐条重跑任务协议单验收清单里的每项检查：
- 硬 eval：亲手跑断言/测试命令；对每条 evidence **重跑其 command_or_method 取实时结果，不采信 exit_status_or_verdict 自报值**（即便多数 evidence 由执行期 run-check 亲手记，finish 独立性仍要求重跑）。reward/exit 不过即该项不过，无裁量
- 反向断言：对照工作底稿起点事实表中的基线锚点逐条验证——不该改的没改（diff 起点 commit）、不该发的没发、范围外没被碰
- 不可逆动作不重跑——验证其留下的环境痕迹（送达记录、日志、产物 mtime、registry 查版本）
- 时变输入的检查只断言已产出产物的性质，不重新消费外部数据
- **不引用 journal 里记的历史输出**——重跑，用当前环境的真实结果

产出初步裁决，进第二阶段。

## 第二阶段（调查 + 审计）

盲验有异常时打开 journal 找线索（只用于定位，不改变第一阶段硬 eval 结论）；无异常也必做规程审计。第二阶段发现违规可推翻第一阶段的通过，反向不成立。

### 逐环节硬 eval + 产物来路审计

对每个环节：
- 核硬 eval 结果（第一阶段已跑）
- 产物来路审计：查 mtime、commit 历史、journal 证据指针对应的产物——"这是真做出来的吗"
- 内容型任务（无 git 环境）：抽样核查引用 URL 可访问性与关键数据点、比对 journal 证据指针里的检索痕迹
- **重点查任务协议单反向断言里标注的作弊路径条目**（如：挖 git 历史抄现成实现、偷看判分脚本）
- 快速通道任务：来路审计按不可逆点与作弊面裁剪，可抽样

### diff 审计（sloppy-fallback）

扫 `git diff` / `git diff --cached` / 未跟踪新文件，检测下列可疑模式：

- `except:.*pass` / `except Exception:.*pass`（吞异常无 grounding 注释）
- `# TODO` / `# FIXME` 出现在**关键路径代码**里（不是测试或 fixture）
- `return None` / `return []` / `return {}` 出现在应该返回真实结果的函数末尾
- `assert True` / `pass  # not implemented`
- 完整实现的函数体只包含日志打印

每匹配一处 → 记录到"可疑改动清单"；发现未 grounding（e.g. 无对应 issue/comment
说明原因）的可疑改动 → **finish 判不通过**。

**熔断**：正则误报有 fingerprint（正则 pattern + 匹配 line）+ repeat_count；同一
fingerprint 触发 3 次以上说明可能是正则问题 → 放行并记警告（不无限阻塞）。

### 追认链轨迹审计（audit-chain）

冻结文件在执行期若走过受控变更（amend-freeze），这些追认必须经得起核验——合法通道
被滥用、或绕开工具直改文件，都在这一步现形：

```bash
state-guard audit-chain --task-dir .portolan/<slug>/
```

一次核四件事，任一不通过即 **finish 判不通过**：

- **追认链哈希衔接**：每个冻结文件的 amend 逐条按时间序，本条 old_hash 必须接上
  前条 new_hash，首条接初版基线（`.frozen/<name>.v1`）。断链 = 有人手工改了记录
  或漏了一段。
- **停点窗口核对**：每条 amend 的时间戳必须落在 phase≠exec 的停点窗口内（窗口由
  终态声明、信号留痕开启，由其后第一条 evidence 关闭）。落在执行期 = 伪造审批。
- **人批对应停点**：approver=human 的条目必须落在终态声明开启的窗口里——人批一定
  对应一个人可见的停点，凭空的人批不认。
- **放松未经人批**：direction 为 loosen / redirect 的条目 approver 必须是 human。
  出现 triage-auto 的放松 = 盲审分流被绕过。

退出码非 0 → 差异摆给用户，记 ledger 一行（finish 判定"不通过（追认链异常）"），
按不通过决策卡走。`chain`（逐条追认）与 `diffs`（v1→vN 全量 diff）一并呈给用户看
改动全貌。无 amend 历史的任务此步恒通过（空链）。

### 软 eval 派评审 subagent

软 eval 环节派新上下文的评审 subagent：
- 模型不弱于执行者（核对 journal 首条自报的执行模型；未自报的，标注后按不弱于本 finish 会话的模型从强选取）
- 读任务目录下 `rubric.md` 与产物
- **不读 journal 叙事**——评审者不应被执行者的自述影响
- 每个维度独立判定，给"不确定"留出口
- "不确定"维度升级为人工验收点，摆证据给用户判

subagent 收到 rubric 全文 + 任务 evidence 全部；除了软 eval 判定外，**必须**
额外检查：

- **Locator 不重叠**：把 evidence 分类为审查（review/QA）与验收（acceptance）
  两组；两组的 locator 集合必须不重叠（同一次工具调用/artifact/thread 不能
  同时充当两者）。若重叠 → judge 不通过并在报告里指明重叠的 locator。
- **evidence freshness**：每条 evidence 的 `fresh_until` 与 finish 会话开始时的
  wall-clock 对比；过期 evidence → 亲自重跑 `command_or_method`；重跑结果不一致
  → judge 不通过。

### 人工验收点

标为人工点的环节（含软 eval 评审升级的），摆证据给用户判：
- 展示环节产出
- 对比参照解（如有）
- 用户判过/不过，判定结果记批注区审批记录一行

## 判定

### 不通过

差异清单摆给用户。先记 ledger 一行（结局 + finish 判定"不通过" + 差异摘要），再出决策卡：

```
【决策卡】验收不通过，怎么办？
要决什么：有 {{N}} 项检查未通过。
为什么现在问：需要决定是打回重做还是调整预期。
推荐 A（理由：{{根据差异性质判断}}）
A. 打回执行：用户在新会话运行 `/portolan:continue`，续跑修好未通过项
B. 回修预期：当前 finish 会话修订任务协议单（走协议修订流程），然后用户在新会话运行 `/portolan:continue` 重跑
回复 A 或 B。
```

### 通过

通过后两步：

**回执归档**：
- 任务协议单最终版 + 验收结果 + 证据指针汇总，存任务目录
- 底稿状态改"已收尾"

**debrief**：
- 归因四分类：预期设计 / 执行 / 工具 / 环境
- 归因含"预期设计"的，必须再答一问：**起草时哪个问题没问、问了就能拦住这个洞？** 答案作为追问清单或 eval 指引的增量提议给用户，点头后修入对应文件——对齐能力随任务数复利
- **只提一条**最小改进建议
- 可复用任务提议任务协议单模板化入库——问用户"要不要把这份任务协议单存为模板？存到 `.portolan/templates/` 下，下次同类任务把路径粘给 `/portolan:long-horizon-task` 即可复用。"
- 连同判定写进 `.portolan/ledger.md` 一行

然后问归档（底稿状态改"已归档"）。

重复型任务（含任务协议单决议记录标"沉淀候选"的）补一句："全自动定时暂不提供。可以给你一条手动 `/loop` 或 `/schedule` 的方案——要的话说一声。"
