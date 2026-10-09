---
name: model-usage
version: "1.0"
description: "模型用量：看一共调用了多少次、哪个模型用得多、平均多快、失败几次、token 用量。关心花销/性能时使用。"
# 查询类技能: 形态是「问一句 -> 技能取数 -> 模型总结」, 没有'写/生成'字样也没有文件路径
# 所以声明 answer_mode 豁免产出守卫 (否则永远接不到, 会落到通用兜底)
answer_mode: true
permission: read
timeout: 120
tags: [模型用量, 用量统计, 调用统计, token用量, 开销统计]
# tags 是弱线索(+2 分), 但**泛词会抢别的请求** —— 实测: '整理' 让本条抢走
# 了 '帮我把表格整理一下' (既有路由基线因此红)。所以 tags 一律用有辨识度的词组。
triggers: [模型用量, 用量统计, 花了多少钱, 调用次数, 用了多少token, 模型统计, 用量的情况, 花了多少, 哪个模型用得多]
platforms: [windows, linux]
requires_tools: [usage_stats, llm]
params:
  days:
    type: text
    required: false
    desc: 统计窗口天数（不给=全部）
steps:
  - id: st
    tool: usage_stats
    action: summary
    input: {}
  - id: read
    tool: llm
    input:
      model: ollama
      system: "你是用量分析助手。只依据给出的统计数据说话, 不许编造金额或单价。"
      prompt: |
        用户问: $message

        下面是真实的模型调用统计:
        $steps.st.output

        请用两三句平实的话说明: 总量、用得最多的模型、有没有异常(失败率高或特别慢)。
        注意: 数据里**没有金额** —— 如果用户问钱, 就说只统计了次数和 token,
        金额要按各自的实际计费标准换算, 不要自己编单价。
      max_tokens: 700
---

# model-usage

看模型用量与开销。数据来源是每次调用后追加的 data/model_usage.jsonl；
探针/验证流量不记账，所以数字反映真实使用。
