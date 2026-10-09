---
name: grill-with-docs
version: "1.0"
description: "逼问式澄清 + 把定下来的决策立刻写进文档，问完就有据可查。需求聊完就丢、后面又忘时用。"
permission: file_read
timeout: 300
tags: [逼问, 决策留档, 边问边写]
triggers: [逼问完把决策写进文档, 一边问一边记下来, 把定下来的决策存成文档, 问到哪写到哪]
platforms: [windows, linux]
requires_tools: [llm, file_ops]
params:
  topic:
    type: text
    required: false
    desc: 那个还含糊的想法/需求
  out:
    type: path
    required: false
    desc: 决策文档保存位置（缺省落桌面 决策_<主题>.md）
steps:
  - id: grill
    tool: llm
    input:
      model: deepseek
      system: |
        你是"逼问者 + 记录员"。一次只问一个问题 (规则同 grilling: 逼决策、给候选、说倾向、
        中文短句、只问一题)。**但**在问题前面先写一段"本轮已定"——把这一轮里已经定下来的
        决策按 `- 决策: <内容> (为什么)` 的格式写清, 这段是要**落盘**的正式内容, 不许写成
        聊天口吻。
        输出结构:
        <!-- DOC -->
        - 决策: ...
        <!-- /DOC -->
        这一题: <一个问题>
        候选: A) ... B) ... (倾向 + 理由)
      prompt: |
        要问清楚的事: $params.topic
        用户的原始要求: $message
      max_tokens: 1500
  - id: save
    tool: file_ops
    action: write
    input:
      path: $params.out
      content: |
        # 决策记录 — $params.topic

        本文件由 grill-with-docs 逐轮追加/覆盖, 记的是**当场定下来的**决策 (含为什么)。
        有疑问对着这里对, 不靠回忆。

        $steps.grill.output
---

# grill-with-docs

逼问式澄清 + 定下来的决策当场落盘。为什么值得分出一个技能：聊出来的决策**不留档就等于没定**，
下次还得重新问一遍；本技能要求每轮的"已定"段直接是落盘的正式内容（不是聊天口吻）。

来源：本机版重写自 Meta Muse Code 的内置技能 `/grill-with-docs`。
