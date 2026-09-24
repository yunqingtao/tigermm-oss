---
name: healthcheck
version: "1.0"
description: "服务体检：查引擎/ollama 这些本地服务在不在、通不通、多快，再结合进程情况给结论。功能突然不好用时使用。"
# 查询类技能: 形态是「问一句 -> 技能取数 -> 模型总结」, 没有'写/生成'字样也没有文件路径
# 所以声明 answer_mode 豁免产出守卫 (否则永远接不到, 会落到通用兜底)
answer_mode: true
permission: read
timeout: 180
tags: [服务检查, 端口检查, 服务存活, 引擎健康, 服务体检]
# tags 是弱线索(+2 分), 但**泛词会抢别的请求** —— 实测: '整理' 让本条抢走
# 了 '帮我把表格整理一下' (既有路由基线因此红)。所以 tags 一律用有辨识度的词组。
triggers: [服务检查, 端口检查, 服务还活着吗, 服务都好吗, 引擎通不通, 引擎还在吗, ollama 通不通, 服务存活检查, 检查一下服务, 引擎在跑吗]
platforms: [windows, linux]
requires_tools: [service_check, system_info, llm, tiger_office]
params:
  extra:
    type: text
    required: false
    desc: 额外的端口（形如 名字:端口, 名字:端口）
  out:
    type: path
    required: false
    desc: 报告保存位置（缺省落桌面）
steps:
  - id: svc
    tool: service_check
    action: ports
    input:
      extra: $params.extra
  - id: ps
    tool: system_info
    action: ps
    input: {}
  - id: rep
    tool: llm
    input:
      system: "你是运维助手。只依据给出的探测结果判断, 不许编造服务状态。"
      prompt: |
        依据下面的探测结果写一份简短的服务体检报告:
        一、结论 (一句话: 正常 / 有服务不通 / 需处理)
        二、逐个服务的状态 (在不在、延迟、说明)
        三、如果有不通的: 可能原因 + 该怎么办 (越具体越好)
        四、进程占用情况里值得注意的 (最多 3 条)
        关于第四段的硬规矩 (实测踩过):
        - 进程数据里**只有内存数值, 没有 CPU 百分比** → 不许据此推断"CPU 高负载"之类结论
        - System Idle Process / System / smss.exe / lsass.exe / services.exe 属正常系统进程,
          不要当成异常上报 (除非数值明显离谱)
        - 只描述数据里真实看得到的; 缺哪项数据就直说缺, 不要替它推断
        服务探测结果:
        $steps.svc.output
        进程情况:
        $steps.ps.output
      max_tokens: 1500
  - id: save
    tool: tiger_office
    action: write_word
    input:
      title: 服务体检报告
      content: $steps.rep.text
      path: $params.out
---