---
name: design-taste
version: "1.0"
description: "一页'别做成 AI 味'的视觉底线清单：本机风格是黑金、拒绝玻璃拟态与花哨按钮。出 UI/海报/封面/网页前过一遍。"
permission: read
timeout: 180
tags: [视觉底线, AI味, 审美清单, 黑金风格]
triggers: [别做出AI味, 这个看起来太AI了, 过一遍审美底线, 按我的风格来别乱配色, 视觉上要克制]
platforms: [windows, linux]
requires_tools: [llm]
params:
  target:
    type: text
    required: false
    desc: 要出的东西（网页/封面/海报/UI/图）
steps:
  - id: taste
    tool: llm
    input:
      model: deepseek
      system: |
        你是**审美把关人**。给出一份扁平的"别这么做"清单, 用来在生成之前把 AI 味掐掉。
        本机已确立的风格 (硬约束, 不许违反):
        · 底色 #0a0502, 主色 #FFAC02 黑金; **禁用白色**; 拒绝玻璃拟态 (backdrop-blur +
          半透明白卡片那种); 拒绝花哨渐变按钮 / 霓虹描边 / 圆角药丸那种"默认好看"。
        · 视觉语言: 暗色日食 + 漂浮星光 + FAB + 极简克制。信息密度高、装饰少。
        · 中文排版照中文字距/标点规矩, 不要照搬英文 hero 大字居中那套。
        通用 AI 味清单 (一律避免):
        紫蓝渐变背景 / 居中 hero + 三卡宫格 / 千篇一律 emoji 图标 / "Level up your X" 句式 /
        无意义的 3D 小球与光斑 / 假数据图表 / 通篇同一灰阶。
        输出: 分三段的扁平清单 —— (1) 这次**不许出现**的 (逐条短句) (2) 必须**守住**的 (3) 一条
        "如果只能守一条, 守哪条"。不要解释、不要夸、不要加总结段。
      prompt: |
        要出的东西: $params.target
        用户的原始要求: $message
      max_tokens: 1200
---

# design-taste

一份扁平的"别做成 AI 味"清单，出 UI / 海报 / 封面 / 网页之前过一遍。为什么是扁平清单而不是
审美论述：生成的时候没人读长文，短句才能当场拦住默认值。本机版把**本机已确立的黑金风格**
（#0a0502 + #FFAC02、禁白、拒玻璃拟态与花哨按钮）写成硬约束，再叠加通用 AI 味清单。

来源：本机版重写自 Meta Muse Code 的内置技能 `/taste`。
