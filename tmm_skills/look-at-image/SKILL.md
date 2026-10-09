---
name: look-at-image
description: "看图：理解图片内容（场景/物体/颜色/布局/图表）。用户给出图片路径或链接并问图里有什么时使用。"
tags: [看图, 识图, 图片理解, 视觉, vision, 图里有什么, 看看这张图, 这张图片]
triggers: [这张图, 看图, 识图, 图片里有什么, 图里有什么, 看看这张图, 看看这图, 这张图片, 图里面有什么, 看看图片]
platforms: [windows, linux]
requires_tools: [vision]
params:
  image:
    type: text
    required: false
    desc: 图片路径或链接（工具自己会从原话里提取）
  question:
    type: text
    required: false
    desc: 想问的问题（默认让它详细描述）
steps:
  - id: look
    tool: vision
    input:
      image: $message
      question: $message
    on_error: stop
---

# 看图

让 TMM 真正"看"图片内容 —— 走视觉模型（DashScope qwen-vl），
不是纯文本模型瞎猜。

## 什么时候用
- "看看这张图里有什么" / "这张截图是什么" / "图里有什么东西"
- 用户给了图片路径（`D:\x\a.png`）或图片链接，并问内容
- 只想**抽文字** → 用 ocr（离线 Tesseract），不要用本技能

## 参数
工具会自己从原话里提取图片路径/URL，所以 `image` 传整句也没关系。

## 注意
- 旧的 pipeline 视觉路径（ollama 编码 + 模型兜底）在 ollama 无视觉模型时会
  **回落到纯文本模型而产生编造描述** —— 本技能走 DashScope VL，结果可信。
- 凭证：环境变量 `DASHSCOPE_API_KEY` 或 keys.json 的 `qwen.key`。
