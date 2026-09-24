---
name: pdf-extract
version: "1.0"
description: "PDF 提取整理：把 PDF 里的文字抽出来，整理成结构化文档存 Word。读报告/论文/说明书时使用。"
permission: file_read
timeout: 300
tags: [PDF提取, PDF整理, PDF转文字, PDF摘要, 文档整理]
# tags 是弱线索(+2 分), 但**泛词会抢别的请求** —— 实测: '整理' 让本条抢走
# 了 '帮我把表格整理一下' (既有路由基线因此红)。所以 tags 一律用有辨识度的词组。
triggers: [PDF提取, 提取PDF, 提取这个PDF, 读这个PDF, 读一下这个PDF, PDF摘要, PDF转文字, PDF整理成文档, PDF内容总结, 这个PDF讲了什么]
platforms: [windows, linux]
requires_tools: [tiger_office, llm]
params:
  path:
    type: path
    required: false
    desc: PDF 文件路径（会从原话里自动取）
  out:
    type: path
    required: false
    desc: 整理结果保存位置（缺省落桌面）
steps:
  - id: pdf
    tool: tiger_office
    action: extract_pdf_text
    input:
      path: $params.path
  - id: org
    tool: llm
    input:
      system: "你是文档整理助手。只依据提取出来的 PDF 原文, 不许编造; 原文残缺或为空就如实说明。"
      prompt: |
        把下面 PDF 的正文整理成中文结构化文档:
        一、这份文件是什么 (标题/类型/大致篇幅)
        二、主要内容 (按原文顺序分点, 保留关键数字和结论)
        三、需要留意的条款或数据 (没有就写"无")
        如果原文有明显缺失或乱码, 在末尾单列一段说明。
        PDF 正文如下:
        $steps.pdf.output
      max_tokens: 3000
  - id: save
    tool: tiger_office
    action: write_word
    input:
      title: PDF 整理
      content: $steps.org.text
      path: $params.out
---