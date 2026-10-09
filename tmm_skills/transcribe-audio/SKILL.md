---
name: transcribe-audio
version: "1.0"
description: "录音转写纪要：音频文件 → 本地 Whisper 转文字 → 整理成结构化纪要 → 存 Word。会议录音/语音备忘/播客时使用。"
permission: file_read
timeout: 600
tags: [录音, 转写, 语音, 音频, 纪要, 播客]
triggers: [转写这段录音, 录音转文字, 音频转文字, 会议录音转写, 语音转文字, 播客转写, 录音整理成纪要, 把录音转成文字]
platforms: [windows, linux]
requires_tools: [whisper, llm, tiger_office]
params:
  path:
    type: path
    required: false
    desc: 音频文件路径（mp3/wav/m4a, 会从原话里自动取）
  out:
    type: path
    required: false
    desc: 纪要保存位置（缺省落桌面）
steps:
  - id: tr
    tool: whisper
    input:
      path: $params.path
      lang: zh
  - id: mm
    tool: llm
    input:
      system: 你是会议纪要整理助手。只依据转写文本, 不许补编内容。
      prompt: |
        把下面的录音转写文本整理成中文纪要:
        一、主题 (一句话)
        二、要点 (按讨论顺序, 3-8 条)
        三、结论与待办 (没有就写"无")
        听不清的地方标注 [听不清], 不要猜。
        转写文本如下:
        $steps.tr.text
      max_tokens: 2500
  - id: save
    tool: tiger_office
    action: write_word
    input:
      title: 录音转写纪要
      content: $steps.mm.text
      path: $params.out
---

# transcribe-audio

音频转纪要：whisper 本地转写（不联网、不上传）→ llm 整理 → 存 Word。
支持 mp3/wav/m4a。长音频耗时较长（timeout 600s）。
