---
name: summarize-web
version: "1.0"
description: "网页摘要归档：抓取网页正文 → 生成结构化中文摘要 → 存成 Word。给了链接要摘要/提炼要点时使用。"
permission: network
timeout: 180
tags: [网页摘要, 文章摘要, 网页抓取, 链接摘要, 归档整理]
triggers: [网页摘要, 文章摘要, 摘要归档, 整理成摘要, 存成摘要, 提炼网页要点, 这篇整理成摘要, 网页提炼要点]
platforms: [windows, linux]
requires_tools: [firecrawl, llm, tiger_office]
params:
  url:
    type: text
    required: false
    desc: 网页链接（会从原话里自动取）
  path:
    type: path
    required: false
    desc: 保存位置（缺省落桌面）
  style:
    type: text
    required: false
    desc: 摘要风格（默认结构化要点）
steps:
  - id: fetch
    tool: firecrawl
    input:
      url: $params.url
      max_chars: 12000
  - id: sum
    tool: llm
    input:
      system: 你是资料整理助手。只依据给定正文, 不许编造; 正文为空就如实说抓取失败。
      prompt: |
        把下面网页正文整理成中文摘要, 严格按三段输出:
        一、一句话主旨
        二、要点 (3-6 条, 每条不超过 40 字, 前缀 "- ")
        三、值得记住的数字或结论 (没有就写"无")
        附加风格要求: $params.style
        正文如下:
        $steps.fetch.result
      max_tokens: 2000
  - id: save
    tool: tiger_office
    action: write_word
    input:
      title: 网页摘要
      content: $steps.sum.text
      path: $params.path
---

# summarize-web

给链接要摘要时走这条：firecrawl 抓正文 → llm 出结构化摘要 → tiger_office 存 Word。
缺输出位置就落桌面。
