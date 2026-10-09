---
name: write-self-review
version: "1.0"
description: 写述职 / 总结 / 自评类材料（述职报告 / 年度总结 / 个人总结 / 晋升材料 / 转正申请）
permission: system
timeout: 180
tags: [述职, 自评, 晋升, 转正, 绩效]
triggers: [述职报告, 述职, 年度总结, 年终总结, 个人总结, 自我评价, 晋升材料, 转正申请, 个人小结]
requires_tools: [tiger_office, llm]
params:
  out_path:
    type: path
    required: false
    desc: 输出文件路径（可省略，默认存桌面）
steps:
  - id: draft
    tool: llm
    input:
      system: 你是中文职场材料撰写顾问。擅长把零散工作事项整理成有说服力的述职材料。
      prompt: |
        用户的原始要求：
        $message

        请写出这份个人材料。要求：
        - 一级标题 "# 标题"（含姓名/部门/周期，若用户没给就用"（填写姓名）"这类明确占位）
        - 结构：本期职责与目标 / 主要工作与成果 / 数据与亮点 / 不足与改进 / 下期计划
        - **成果尽量量化**：能写成数字的写成数字；用户没给数字的地方写"（建议补充：XX 指标）"提示他填
        - 语气客观、有底气，不要空话套话和口号
        - 只输出正文，不要解释、不要代码块标记
      temperature: 0.6
      max_tokens: 2500
    output: text
    on_error: stop
  - id: save
    tool: tiger_office
    action: write_word
    input:
      path: $params.out_path
      content: $steps.draft.text
    on_error: stop
---

# write-self-review

**触发**：用户要写述职报告 / 年度总结 / 个人总结 / 自我评价 / 晋升材料 / 转正申请。

**做什么**：把用户说的零散事项整理成结构化材料，**主动提示该补的量化数据**，存成 `.docx`。

**与 write-office-doc 的分工**：那个是通用办公文档；这个是个人评价类，
专门要求"成果量化 + 缺数据时提示补充"。

**例**：
- `写述职报告，今年做了三个项目，带了两个新人`
- `帮我写年度总结，重点是客服响应速度提升了`
- `写份晋升材料 到 D:/晋升.docx`
