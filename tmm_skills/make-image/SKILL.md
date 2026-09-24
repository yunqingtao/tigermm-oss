---
name: make-image
description: "生图：文生图，用文字描述生成图片并保存到本地。用户要求画/生成/做一张图时使用。"
tags: [生图, 生成图片, 画图, 文生图, 图片生成, image, 画一张, 做张图]
triggers: [画一张, 画个, 画幅, 生成图片, 生成一张, 生图, 文生图, 做一张图, 做张图, 来一张图, 画一幅]
platforms: [windows, linux]
requires_tools: [image_gen]
params:
  prompt:
    type: text
    required: false
    desc: 图片描述（工具会自动剥掉"画一张"这类引导词）
  path:
    type: path
    required: false
    desc: 保存位置（可以给目录，默认桌面）
steps:
  - id: gen
    tool: image_gen
    input:
      prompt: $message
      path: $params.path
    on_error: stop
---

# 生图（文生图）

用文字描述生成图片，走阿里百炼通义万相（默认 `wan2.2-t2i-flash`，快且便宜）。

## 什么时候用
- "画一张戴墨镜的柴犬" / "生成图片：赛博朋克城市" / "做张海报，主题是…"
- ★ 与 `diagram` 区分：`diagram` 画的是 **PlantUML 结构图**（架构图/流程图/时序图，
  需要 DSL 源码）；本技能画的是**画面内容**（照片感/插画/海报）。

## 参数
- `prompt`：整句传进来即可，工具会剥掉"画一张/帮我生成"这类引导词。
- `path`：不给就存桌面，文件名 `TMM_图_<时间戳>.png`。

## 凭证
环境变量 `DASHSCOPE_API_KEY` 或 keys.json 的 `qwen.key`。
