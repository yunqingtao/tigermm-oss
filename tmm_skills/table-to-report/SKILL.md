---
name: table-to-report
version: "1.0"
description: 把 Excel 表格转成分析报告（读表 → 分析 → 写 Word）
permission: system
timeout: 240
tags: [数据分析, 分析报告, xlsx, 表格报告]
triggers: [做分析报告, 写分析报告, 生成分析报告, 出个分析报告, 出份分析报告, 分析报告, 数据报告, 表格分析, 报表分析, 数据洞察, 表格报告]
requires_tools: [tiger_office, llm]
params:
  path:
    type: path
    required: true
    desc: 要分析的 Excel 文件（.xlsx）
  out_path:
    type: path
    required: false
    desc: 报告输出路径（可省略，默认存桌面）
steps:
  - id: read
    tool: tiger_office
    action: analyze_table
    input:
      path: $params.path
    output: content
    on_error: stop
  - id: analyze
    tool: llm
    input:
      system: 你是数据分析师。基于给出的表格统计写出有结论、有洞察的分析报告。
      prompt: |
        用户的原始要求：
        $message

        这是系统对该表格的结构化统计（列名、种类数、取值分布）：
        ——————————
        $steps.read.content
        ——————————

        请据此写出分析报告。要求：
        - 一级标题 "# 数据分析报告"（含表格文件名）
        - 用 "## " 分节：数据概况 / 关键发现（3-5 条，每条给出**具体数字与占比**） / 异常与问题 / 结论与建议
        - ★ **只使用上面统计里给出的数字**。没有的数字不要编；需要但缺失的写"（需补充：XX 统计）"
        - 结论要落到"所以该怎么做"，不要只复述表格
        - 只输出报告正文，不要解释、不要代码块标记
      temperature: 0.5
      max_tokens: 2500
    output: text
    on_error: stop
  - id: save
    tool: tiger_office
    action: write_word
    input:
      path: $params.out_path
      content: $steps.analyze.text
    on_error: stop
---

# table-to-report

**触发**：用户要给 Excel **做分析报告**（"分析报告/数据报告/表格分析/报表分析/数据洞察"）。

**这是三步 DAG**（技能系统的多步能力示例）：
```
read     tiger_office.analyze_table   读表 → 列名/种类数/取值分布     $steps.read.content
analyze  llm                          基于统计写报告                $steps.analyze.text
save     tiger_office.write_word      存成 .docx
```
参数通过 `$steps.<id>.<field>` 在步骤间流动；任一步失败按 `on_error: stop` 中断，不产出半成品。

**★ anti-hallucination**：prompt 里明确"只使用统计里给出的数字"，避免模型编数据。

**注意**：`path` 是**必填**（没给会诚实报"缺必填参数"）。

**例**：
- `把这个表格做成分析报告 D:/销售.xlsx`
- `D:/客户表.xlsx 出一份数据集洞察报告 到 D:/洞察.docx`
