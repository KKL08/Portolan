# rubric：{{任务名}}

> 写者：仅发起模式与 finish 写；执行环不读此文件（单写者规则，隔离设计）。
> 每个软 eval 环节一节，维度逐条原子化，独立判定，给"不确定"留出口防止硬编。

## 环节：{{环节名}}
{{维度 / 判定标准（好例 vs 坏例）/ 通过阈值；每个维度独立判，"不确定"升级为人工验收点}}

## Locator 不重叠硬规则（finish 第 3 步 subagent 检查）

**"审查证据"与"验收证据"的 locator 不重叠**——两者的 locator 集合不能有交集：
同一次工具调用、同一 artifact、同一 thread 不能既充当"审查通过"的依据、又充当
"验收通过"的依据（防自证闭环）。

finish 第 3 步 subagent 提取 evidence 的 locator 集合后：
- 分类为审查（review/QA）证据 vs 验收（acceptance）证据
- 若两集合有交集 → **judge 不通过**并在报告里指明重叠的 locator

⚠️ 本节由 finish subagent 读取；执行环**不可见** rubric（延续独有隔离设计）。
