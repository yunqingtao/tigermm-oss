---
name: second-opinion
version: "1.0"
description: "双模型审阅：本地模型先审一遍，云端模型独立复核并挑错，汇总成审阅意见存 Word。稿子/方案/代码要过一遍时使用。"
permission: file_read
timeout: 300
tags: [审阅, 复核, 挑错, 双模型, 意见, 稿子]
triggers: [双模型审阅, 二次审阅, 交叉审阅, 换个模型审一遍, 让另一个模型看看, 帮我审一下这份稿子, 独立复核一下, 互相审一下]
platforms: [windows, linux]
requires_tools: [file_ops, llm, tiger_office]
params:
  path:
    type: path
    required: false
    desc: 要审的文件路径（会从原话里自动取）
  out:
    type: path
    required: false
    desc: 审阅意见保存位置（缺省落桌面）
steps:
  - id: read
    tool: file_ops
    action: read
    input:
      path: $params.path
  - id: first
    tool: llm
    input:
      model: ollama
      system: 你是严格的审阅者, 只说具体问题, 不空泛夸奖。
      prompt: |
        审读下面这份内容, 给出:
        1) 最严重的 3 个问题 (每条指出位置或原句)
        2) 可以改好的地方 (给出具体改法)
        3) 整体判断 (能用 / 需修 / 重写)
        内容如下:
        $steps.read.output
      max_tokens: 1500
  - id: second
    tool: llm
    input:
      model: deepseek
      system: "你是独立复核者。不要复述前一位的意见, 只做三件事: 认同哪些、反对哪些、补漏什么。"
      prompt: |
        内容原文:
        $steps.read.output
        前一位审阅者的意见:
        $steps.first.text
        请输出复核结论:
        一、我同意的 (理由)
        二、我反对或认为过度的 (理由)
        三、他漏掉的问题 (最重要)
        四、最终建议
      max_tokens: 1800
  - id: save
    tool: tiger_office
    action: write_word
    input:
      title: 双模型审阅意见
      content: $steps.second.text
      path: $params.out
---

# second-opinion

两个模型各审一遍：本地 ollama 先审 → deepseek 独立复核（认同/反对/补漏）→ 汇总存 Word。
为什么用两个模型：单一模型的盲点它自己看不见。
