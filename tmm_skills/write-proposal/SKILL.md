---
name: write-proposal
version: "1.0"
description: 写方案类文档（项目方案 / 策划案 / 需求文档 / PRD / 计划书 / 立项报告）
permission: system
timeout: 180
tags: [策划, 立项, 需求文档, 产品文档, 计划书]
triggers: [方案, 策划案, 策划书, PRD, 需求文档, 项目计划书, 商业计划书, 立项报告, 产品需求]
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
      system: 你是中文商业方案写作专家。输出结构完整、可直接提交的方案正文。
      prompt: |
        用户的原始要求：
        $message

        请据此写出完整方案。结构与要求：
        - 一级标题 "# 方案名"（具体、含对象与目的）
        - 用 "## " 分节，建议包含：背景与目标 / 现状与问题 / 方案设计 / 实施步骤与时间表 / 资源与分工 / 风险与应对 / 预期效果
        - 具体可执行：写清做什么、谁做、什么时候做完；不要"（此处补充）"这类占位
        - 数字与时间要具体，无法确定的写合理假设并标注"（假设）"
        - 只输出方案正文，不要开场白、不要解释、不要代码块标记
      temperature: 0.6
      max_tokens: 3000
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

# write-proposal

**触发**：用户要写方案 / 策划案 / PRD / 需求文档 / 计划书 / 立项报告。

**做什么**：模型按用户原话写完整方案（带背景/目标/实施/资源/风险/预期），存成 `.docx`。

**触发词设计**：`方案` 这类话题词命中即算（靠 `_looks_like_creation` 守卫拦住读类请求 ——
"把方案发给涛哥"、"这个方案怎么改" 不含 写/做/生成 等动词 → 不触发，走原链路）。

**例**：
- `写个新功能上线的推广方案`
- `写一份XX项目的立项报告 到 D:/立项.docx`
- `帮我拟一份产品需求文档，做在线题库`
