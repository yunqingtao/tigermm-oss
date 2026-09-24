---
name: video-summarize
version: "1.0"
description: "视频转写摘要：视频里的人声转成文字，再整理成要点纪要存 Word。看完视频要一份文字稿时使用。"
permission: file_read
timeout: 900
tags: [视频转写, 视频摘要, 视频纪要, 视频文字稿, 字幕稿]
# tags 是弱线索(+2 分), 但**泛词会抢别的请求** —— 实测: '整理' 让本条抢走
# 了 '帮我把表格整理一下' (既有路由基线因此红)。所以 tags 一律用有辨识度的词组。
triggers: [视频摘要, 视频转写, 视频转成文字, 视频内容总结, 这段视频讲了什么, 这个视频讲了什么, 视频讲了什么, 视频里说了什么, 视频纪要, 视频字幕稿, 把视频转文字]
platforms: [windows, linux]
requires_tools: [whisper, llm, tiger_office]
params:
  path:
    type: path
    required: false
    desc: 视频文件路径（mp4/mov/mkv, 会从原话里自动取）
  out:
    type: path
    required: false
    desc: 摘要保存位置（缺省落桌面）
steps:
  # 不需要先抽音轨: whisper 内部用 ffmpeg 解码, 实测能直接吃 mp4 (已真跑验证)
  - id: tr
    tool: whisper
    input:
      path: $params.path
      lang: zh
  - id: sum
    tool: llm
    input:
      system: "你是内容整理助手。只依据转写文本, 不许补编内容; 听不清的地方标注 [听不清]。"
      prompt: |
        把下面的视频转写文本整理成中文摘要:
        一、一句话主旨
        二、讲到的要点 (3-8 条, 按顺序)
        三、结论 / 待办 (没有就写"无")
        转写文本如下:
        $steps.tr.text
      max_tokens: 2500
  - id: save
    tool: tiger_office
    action: write_word
    input:
      title: 视频转写摘要
      content: $steps.sum.text
      path: $params.out
---