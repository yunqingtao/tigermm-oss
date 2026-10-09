---
name: make-slides
version: "1.0"
description: 做 PPT 演示文稿（汇报 / 演示 / 讲解）并存成 .pptx
permission: system
timeout: 150
tags: [ppt, 幻灯片, 演示, 汇报, 演示文稿]
triggers: [做PPT, 做ppt, 做个PPT, 汇报PPT, 生成PPT, 做幻灯片, 做演示文稿, 做汇报,
           PPT, ppt, 幻灯片, 演示文稿, 汇报材料]
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
      system: 你是中文商务演示文稿撰稿人。输出精炼、有层次的幻灯片文案。
      prompt: |
        用户的原始要求：
        $message

        请输出幻灯片文案。格式要求（严格遵守）：
        - **第一行**必须是 "# 演示标题"（这会成为封面标题，必须写）
        - 之后每页以 "## 页标题" 开头
        - 每页 3-5 条要点，每条以 "- " 开头，每条不超过 30 字
        - 内容具体，不要"（此处展开）"这类占位符
        - 第一页 "## 概览" 总起，最后一页 "## 小结"
        - 只输出文案，不要解释、不要代码块标记
      temperature: 0.6
      max_tokens: 2000
    output: text
    on_error: stop
  - id: save
    tool: tiger_office
    action: write_ppt
    input:
      path: $params.out_path
      content: $steps.draft.text
    on_error: stop
---

# make-slides

**触发**：用户要做 PPT / 幻灯片 / 演示文稿 / 汇报。

**做什么**：模型按用户原话写幻灯片文案（`## 页标题` + `- 要点`），再生成 `.pptx`（自动加封面）。

- 用户没给路径 → 存桌面
- 页数 = 文案里的 `## ` 数量
- 想改样式：改 `tools/tiger_office.py` 的 `_write_ppt`（模板取 `slide_layouts[0/1]`）

**例**：
- `做PPT，讲一下我这季度的项目进展`
- `做个汇报PPT 到 D:/周会.pptx`
