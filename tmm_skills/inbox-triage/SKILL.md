---
name: inbox-triage
version: "1.0"
description: "邮件分诊：读收件箱，分拣出要立刻处理的、可以晚点看的、不用理的，并抽出待办。邮件多到不想看时使用。"
# 查询类技能: 形态是「问一句 -> 技能取数 -> 模型总结」, 没有'写/生成'字样也没有文件路径
# 所以声明 answer_mode 豁免产出守卫 (否则永远接不到, 会落到通用兜底)
answer_mode: true
permission: network
timeout: 300
tags: [邮件分诊, 收件箱整理, 邮件待办, 邮件摘要, 收件箱]
# tags 是弱线索(+2 分), 但**泛词会抢别的请求** —— 实测: '整理' 让本条抢走
# 了 '帮我把表格整理一下' (既有路由基线因此红)。所以 tags 一律用有辨识度的词组。
triggers: [邮件分诊, 整理收件箱, 邮件摘要, 收件箱有哪些重要的, 待处理邮件, 邮件待办, 帮我看看邮件, 收件箱整理, 邮件要紧吗, 有什么新邮件]
platforms: [windows, linux]
requires_tools: [check_mail, llm, tiger_office]
params:
  out:
    type: path
    required: false
    desc: 分诊结果保存位置（缺省落桌面）
steps:
  - id: mail
    tool: check_mail
    action: list
    input:
      count: 15
  - id: tri
    tool: llm
    input:
      system: "你是邮件助理。只依据给出的邮件内容判断, 不许编造发件人或内容。"
      prompt: |
        下面是收件箱最近邮件的清单, 请做分诊:
        一、要立刻处理 (列出: 发件人 + 事由 + 建议动作)
        二、可以晚点看 (一行一封)
        三、不用理 (广告/通知类, 只给数量)
        四、抽出待办清单 (能做的一件事一行)
        收件箱内容:
        $steps.mail.output
      max_tokens: 1800
  - id: save
    tool: tiger_office
    action: write_word
    input:
      title: 邮件分诊
      content: $steps.tri.text
      path: $params.out
---