---
name: write-speech
version: "1.0"
description: 写讲话类文稿（演讲稿 / 发言稿 / 致辞 / 主持词 / 会议开场白）
permission: system
timeout: 180
tags: [致辞, 讲稿, 主持词, 口播稿, 讲话]
triggers: [演讲稿, 发言稿, 致辞, 讲话稿, 讲稿, 开场白, 主持词, 颁奖词]
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
      system: 你是中文演讲稿撰写人。写出来要能直接照着念，口语顺畅、有节奏、有感染力。
      prompt: |
        用户的原始要求：
        $message

        请写一份讲话稿。要求：
        - **第一行**必须是 "# 讲话标题"（如 "# 年会致辞"，会成为文件名，必须写）
        - 直接写**可以念出口的稿子**（口语化短句，避免书面长句和生僻词）
        - 一千字约念四分钟；按用户要求的场合控制长度
        - 结构：称呼开场 → 切入主题 → 2-3 个要点（每个配一个具体例子或细节）→ 收尾呼应
        - 适当加停顿提示，如"（停顿）"；不要加舞台指示以外的注解
        - 只输出讲稿正文，不要标题解释、不要"以下是稿子"之类的话
      temperature: 0.7
      max_tokens: 2200
    output: text
    on_error: stop
  - id: save
    tool: tiger_office
    action: write_word
    input:
      path: $params.out_path
      content: $steps.draft.text
    on_error: stop
---

# write-speech

**触发**：用户要写演讲稿 / 发言稿 / 致辞 / 主持词 / 讲话稿。

**做什么**：模型写出**可直接照念**的口语稿，存成 `.docx`。

**特点**：口播导向 —— 短句、有停顿提示、按"千字约四分钟"控时长。

**例**：
- `写一份年会致辞，感谢团队这一年`
- `帮我写个产品发布会的开场白，3 分钟`
- `写份优秀员工颁奖词`
