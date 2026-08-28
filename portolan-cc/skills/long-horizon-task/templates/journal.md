# journal：{{任务名}}

> 写者：执行环。环节粒度：每环节一条，有新证据才追加；证据写指针，不写"已验证"三个字。
> 首条记录先自报本会话执行模型（finish 核对评审模型不弱于执行者用）。
> 全部环节行追加进同一张环节表，不新开第二张表；终态声明与收尾报告节全文各只有一个，多轮收束在终态声明节内追加条目。
> （按 Turn 分节，详见下方"Turn 组织形式"节；环节粒度的追加原则不变）

| 环节 | 动作 | 证据指针 | 下一步 |
|---|---|---|---|
| {{环节名}} | {{一个有界动作}} | {{文件路径 / 命令输出位置 / commit}} | {{下一动作}} |

## 终态声明（每次收束追加一条，以最新一条为当前有效终态）

> 本节条目由 `state-guard declare-terminal` 追加（勿手写留空骨架）。每条格式：
> `- 日期/轮次` / `- 终态：<五值>` / `- 依据：一两句 + 证据指针` /
> `- 复核方式`（被阻塞→断掉的依赖 + 一条反引号包住的可重跑核验命令，verify-blocked 只识别反引号内命令；需批准→逐项待批清单；其余"无"）/
> `- 下一步建议`（给 continue 的参考，不构成批准）。

### 收尾报告（仅无进展时写，紧跟终态声明）
{{熔断前最后几轮的尝试、结果和失败原因}}

## 结构化 evidence

本节的 evidence 由 `state-guard run-check` 亲跑验收命令时**自动追加**，10 字段由工具代算——
**执行者不手写**（手写 YAML 视为无效证据，finish 不采信）。下面是字段格式参考，仅示意、
非真实条目（故意用 `text` 围栏，避免被证据解析当成真 evidence）：

```text
evidence:
  - evidence_id: ev-001            # 唯一 ID（本 journal 内不重复）
    producer_attempt_id: turn-3     # 产生 evidence 的 turn 标识
    kind: shell                     # 取值：shell | grep
    locator: /tmp/output.log        # 证据所在的文件路径/thread/tool_call_id
    content_hash: abc123...         # 证据内容的 sha256
    observed_at: "2026-08-17T10:00:00Z"    # ISO 8601 wall-clock
    fresh_until: "2026-08-17T12:00:00Z"    # 时效截止（超过则 finish 必须重跑）
    command_or_method: "grep 'PASS' /tmp/output.log"  # 可复跑的命令
    exit_status_or_verdict: 0       # 退出码 | pass/fail
    verifier_id: finish-session-uuid1     # 由 finish 会话开始时填入
```

## Turn 组织形式

journal 按 turn 分节，每 turn 一节：

- `## Turn N`（N 从 1 开始）后跟：
  - 自由散文正文（记 observe/choose/act/verify/记录/继续或收束 六步节拍）
  - `### Evidence`（该 turn 产生的 evidence YAML 小节，格式见上）
