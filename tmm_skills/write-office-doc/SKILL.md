---
name: write-office-doc
version: "1.0"
description: 写办公文档（周报 / 日报 / 会议纪要 / 工作总结 / 报告）并存成 Word
permission: system
timeout: 150
tags: [周报, 日报, 纪要, 总结, 报告, 文档, word, 汇报]
triggers: [写周报, 写日报, 写月报, 写季报, 写会议纪要, 写总结, 写报告, 写文档, 写简报, 写工作总结, 生成周报, 生成报告, 生成会议纪要, 周报, 日报, 会议纪要, 工作总结, 简报]
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
      system: 你是中文办公写作助手。输出干净、可直接入档的文档正文。
      prompt: |
        用户的原始要求：
        $message

        请据此写出文档正文。要求：
        - 用 markdown：一级标题用 "# "，小节用 "## "，条目用 "- "
        - 内容具体可提交，不要留占位符、不要写"（此处填写）"这类空话
        - ★ 只写你**确实知道**的内容（来自上面用户的原话）。
          绝对不要声称"某个任务已执行完毕/已按要求完成"，因为**你这个环节只负责写文档**，
          并不知道任何任务是否被执行过。没有依据的地方写"（本文档未涉及/待补充）"，
          宁可留白也不许编造执行结果。
        - ★★ 上面那条只在**用户要求"执行某任务并出报告"**时才额外说明一句：
          正文开头写"说明：本文档为文字稿，未包含任务执行结果（本环节不执行任务）。"
          **正常写作请求（周报 / 日报 / 纪要 / 简历 / 方案 / 述职 …）绝对不要加这句** ——
          那类请求里"文档本身就是交付物"，加了反而把正文挤没、显得答非所问。
        - 只输出正文，不要开场白、不要解释、不要代码块标记
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

# write-office-doc

**触发**：用户要写周报 / 日报 / 会议纪要 / 工作总结 / 报告 / 一般办公文档。

**做什么**：先让模型按用户原话写出文档正文（markdown 风格），再存成 `.docx`。

- 标题从正文第一个 `# ` 自动抽取
- 用户没给路径 → 存桌面（与 `file_ops` 的裸名约定一致）
- 中文字体已设 `微软雅黑`（含 `w:eastAsia`，避免 Word 打开乱码）

**例**：
- `写周报，本周修了 MCP 泄漏和技能接线`
- `写会议纪要 到 D:/会议.docx`
- `写一份季度工作总结`
