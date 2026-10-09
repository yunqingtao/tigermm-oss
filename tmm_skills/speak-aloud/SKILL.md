---
name: speak-aloud
version: "1.0"
description: "本地播报：把一段文字或一个文本文件用语音念出来（朗读 / 念给我听 / 读出声）"
permission: execute
timeout: 120
tags: [语音播报, 念给我听, 读出声]
triggers: [播报, 语音播报, 念给我听, 念一下, 念出来, 读出来, 读给我听, 朗读, 读一遍, 说出来给我听]
requires_tools: [voice]
params:
  text:
    type: text
    required: false
    desc: 要念的文字（用「」或引号括起来最稳）
  src:
    type: path
    required: false
    desc: 要念的文本文件（不给 text 时念这个文件的内容）
steps:
  - id: speak
    tool: voice
    action: speak
    input:
      text: $params.text
      file: $params.src
    on_error: stop
---

# speak-aloud（本地播报）

**触发**：用户要"把话说出来" —— 播报 / 念给我听 / 念一下 / 读出来 / 朗读 / 读给我听。

**做什么**：把文字交给 `voice` 工具用 Edge TTS 念出来（`zh-CN-YunxiNeural` 男声，
本机离线生成 mp3 再播放；Edge TTS 不可用时退到 Windows SAPI）。

**两种用法**

```text
念给我听：「今天天气不错，适合出门。」
说出来给我听：这个方案下周一开始执行
念一下 D:/报告/摘要.txt          ← 只给文件不给文字 → 工具读文件再念
```

**参数是怎么抽出来的（设计约束，实测过）**：`text` 走 pipeline 的规则提取 ——
引号/「」里的内容优先，其次 `写入|内容|正文|说:` 之后的内容。
所以**要念的原话最好用引号或「」括起来**，或者写成"说：……"；
随手一句"念一下今天挺好"没有引号时 `text` 会是空的 —— 那时工具会诚实报
"没有要朗读的文字"，不会瞎念你的指令（宁可不念，也不念错）。

**没走模型**：整条链只有一步工具调用（不花 token、不联网调 LLM），
所以是**纯产出型技能**，不需要 `answer_mode`。

**边界**：这是"念已有的字"。要"总结完再念"的复合请求（查资料→播报）现在不支持，
拆成两步说。
