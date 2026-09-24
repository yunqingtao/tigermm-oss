---
name: session-logs
version: "1.0"
description: "翻对话记录：查你和 TMM 聊过什么、什么时候聊的、某件事在哪次提过。只读历史，不改任何数据。"
# 查询类技能: 形态是「问一句 -> 技能取数 -> 模型总结」, 没有'写/生成'字样也没有文件路径
# 所以声明 answer_mode 豁免产出守卫 (否则永远接不到, 会落到通用兜底)
answer_mode: true
permission: read
timeout: 120
tags: [聊天记录, 对话历史, 历史查询, 回忆对话, 记录检索]
# tags 是弱线索(+2 分), 但**泛词会抢别的请求** —— 实测: '整理' 让本条抢走
# 了 '帮我把表格整理一下' (既有路由基线因此红)。所以 tags 一律用有辨识度的词组。
triggers: [聊天记录, 对话历史, 我们聊过什么, 以前说过什么, 历史记录, 聊过几次, 查一下记录, 翻翻记录, 之前提过吗, 之前聊过, 我们聊过, 我说过什么, 提过这件事吗, 会话日志, 会话记录, 最近的会话, 看看会话, 聊过什么, 之前的记录, 最近的记录]
platforms: [windows, linux]
requires_tools: [session_logs, llm]
params:
  keyword:
    type: text
    required: false
    desc: 要找的关键词（不给就列最近聊的内容）
steps:
  - id: logs
    tool: session_logs
    action: recent
    input:
      limit: 60
      keyword: $params.keyword
  - id: ans
    tool: llm
    input:
      model: ollama
      system: "你是 TMM 的回忆助手。只依据给出的对话记录回答, 记录里没有的就直说没有, 不许编造。"
      prompt: |
        用户的问题: $message

        下面是数据库里的真实对话记录 (含统计头和逐条消息):
        $steps.logs.output

        请回答用户的问题:
        - 问"聊过什么/几次"→ 按记录里的事实说, 引用具体日期和原话片段
        - 问"某件事提过吗"→ 明确说有/没有, 有就指出在哪天哪条
        - 记录为空或没找到 → 直接说没找到, 别猜
        回答控制在 200 字内, 用平实的口语。
      max_tokens: 800
---