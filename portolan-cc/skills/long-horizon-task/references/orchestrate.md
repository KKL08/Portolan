# orchestrate：主 session 编排规程

> 读者：dispatch 组装完成后的主 session（portolan 三模式之一）。
> 边界：要人拍板的续跑走 continue.md（决策卡三态）；本篇只管无需人的自动路径。
> 铁律一：编排分支判定一律出自 `state-guard orch-step`，不凭对话记忆分支。
> 铁律二：派 subagent 的唯一正当理由是需要干净独立上下文（execution / finish）。
> 铁律三：本编排激活期间勿开 watch.py daemon（互斥，防双重派发）。用户新指令写批注区，不注入运行中的 subagent。

## 编排循环

前置：五件套已组装、冻结哈希已写、档位已签（`orch-set --field tolerance_tier`）。

```
循环：
  state-guard orch-step --task-dir <任务目录> [--host-signal <见下表>] [--finish-result pass|fail]
  → 按下方动作绑定表执行返回的 action
  → dispatch 类动作等 task-notification 唤回后回到循环
  → wait_user / settle 出循环
```

advisories 非空时：对话里提示一句 + 批注区转向建议表各落一条（notify=false 的只落批注区）。

## 动作绑定表

| action | 怎么执行 |
|---|---|
| `dispatch_next_attempt` | 派执行 subagent（background）：prompt = execution.md 全文 + 任务目录 + payload 里的 hints。hint_kind=换思路 时按下方「换思路指引」生成新思路写入 prompt；attempt 计数与终态清理 orch-step 已代办，勿再手动 orch-set |
| `recover_subagent` | method=send_message：向原 subagent 发固定恢复话术"检查你的任务状态，继续执行直到声明命名终态；若终态已声明但编排层未消费，用 declare-terminal 重新亲写一条"（此为唤醒不是新指令）。method=cold_restart：按 continue.md 生成 8 项恢复包，派新执行 subagent |
| `dispatch_finish` | 按 finish.md 两阶段协议组装 prompt 派干净 finish subagent：第一阶段只给 任务协议单 + rubric + evidence 重跑指引 + 代码目录（不给 journal）。finish 返回后读回执结论，以 `--finish-result pass|fail` 回报 orch-step |
| `run_triage_review` | 按 references/triage-review.md 派盲审 subagent（输入 = payload.review_inputs 与 diff），拿到方向跑 `state-guard triage --file <文件> --review-verdict <方向>` 收尾，然后回到循环 |
| `backoff_retry` | 等 payload.wait_seconds 秒（可用 `sleep N`），然后回到循环 |
| `wait_user` | 按 payload 的 reason_kind 与素材出决策卡（格式见 SKILL.md），停下等用户；用户回来走 continue.md |
| `settle` | finish_passed：把回执呈给用户。idle_normal：告知空轮正常收束，确认是否再跑一轮或进 finish |

## host-signal 分类指引

task-notification 或平台报错落在对话里时，把它分类成枚举值传给 orch-step。分类只看**报错的机制层**，不猜执行内容：

| 信号 | 判别标准 | 典型样例 |
|---|---|---|
| `user_stop` | 用户明确叫停（对话原话），或用户 Esc/interrupt | "先停一下"、"别跑了" |
| `quota` | 平台配额/限流类报错 | "rate limit"、"quota exceeded"、429 |
| `auth_failure` | 凭据失效类报错 | "authentication failed"、401、token 过期 |
| `context_overflow` | subagent 上下文溢出/被截断 | "context window exceeded"、压缩循环报错 |
| `subagent_error` | subagent 进程异常退出且非上述任一 | task 状态 error、无输出崩溃 |

对不上任何一行 → 不传 host-signal，让 orch-step 按盘面判。分类拿不准的按更保守的一档报（宁可 auth_failure 不可漏报为 subagent_error）。

## 换思路指引（hint_kind=换思路 时生成重派 prompt）

新思路必须与已试路径可区分，来源按序取：

1. **排除项**：读 journal 环节表归纳已试过的路径/工具/参数，逐条写成"不要再试 X"；
2. **用户建议**：批注区转向建议表有未消费条目的，优先采纳；
3. **换法启发**（前两者不足时选一）：拆更小步（把当前环节一分为二先证前半）、换工具（同目标换实现手段）、换观察面（先加日志/断言弄清失败点再动手）。

重派 prompt 里明写"本轮采用新思路：<一句话>；与上轮的区别：<一句话>"。

## 编排状态维护纪律

全部状态经 state-guard（orch-step 自动记账；字段权威清单=ORCH_FIELDS），
不建任何新 state 文件，不在对话内存里单独记账，不手动改计数字段。
