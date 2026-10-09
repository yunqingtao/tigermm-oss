---
name: write-resume
version: "1.0"
description: 写求职材料（简历 / 求职信 / 自荐信 / 个人简介）
permission: system
timeout: 180
tags: [求职, 自荐, 应聘, 个人简介]
triggers: [简历, 求职信, 自荐信, 求职简历, 个人简历, 应聘材料, 个人简介]
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
      system: 你是中文求职材料顾问。写出的简历要能被 HR 在 30 秒内抓到重点。
      prompt: |
        用户的原始要求：
        $message

        请写出求职材料。要求：
        - 用 markdown："# " 写姓名或"简历"，"## " 分节
        - 结构：个人信息 / 求职意向 / 核心优势（3 条，每条一行） / 工作经历（按倒序，用"做了什么 + 拿到什么结果"写，尽量带数字） / 项目经历 / 教育背景 / 技能证书
        - **成果导向**：每条经历都写成"动作 + 结果"，能用数字就用数字
        - 用户没提供的信息，写"（待补充）"，不要编造具体公司名、学校名、电话
        - 只输出正文，不要解释、不要代码块标记
      temperature: 0.5
      max_tokens: 2500
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

# write-resume

**触发**：用户要写简历 / 求职信 / 自荐信 / 个人简介。

**做什么**：产出成果导向的求职材料，**缺的信息写"（待补充）"而不编造**（避免假简历），存成 `.docx`。

**边界**：只写材料，不投递（投递走 `send-email-to-contact` 技能）。

**例**：
- `帮我写份简历，五年测试经验，擅长自动化`
- `写封求职信，投数据分析岗，强调我的 SQL 和看板能力`
