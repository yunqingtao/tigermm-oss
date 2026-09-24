---
name: pc-checkup
version: "1.0"
description: "电脑体检：读磁盘/系统/进程 → 让模型判读风险并给建议 → 存 Word 报告。想知道电脑状态或空间时使用。"
permission: system
timeout: 180
tags: [体检, 系统, 磁盘, 空间, 进程, 健康]
triggers: [电脑体检, 系统体检, 体检电脑, 电脑健康状况, 电脑状态报告, 给电脑做个体检, 给电脑做体检, 做个体检, 电脑体检报告, 体检一下电脑]
platforms: [windows, linux]
requires_tools: [system_info, llm, tiger_office]
params:
  out:
    type: path
    required: false
    desc: 报告保存位置（缺省落桌面）
steps:
  - id: disk
    tool: system_info
    action: disk
    input: {}
  - id: sys
    tool: system_info
    action: sysinfo
    input: {}
  - id: rep
    tool: llm
    input:
      system: 你是电脑体检助手。只依据给出的机器数据, 不许编造配置; 数据缺失就跳过那项。
      prompt: |
        依据下面的机器数据写一份中文体检报告, 分四段:
        一、总体结论 (一句话: 健康/注意/需处理)
        二、磁盘 (每个盘使用率; 剩余低于 15% 的点名提醒)
        三、系统与内存
        四、建议 (最多 3 条, 要能照着做)
        环境: Windows 本机, 用户常做视频剪辑与 AI 推理 (磁盘内存较吃紧)。
        磁盘数据:
        $steps.disk.output
        系统数据:
        $steps.sys.output
      max_tokens: 1800
  - id: save
    tool: tiger_office
    action: write_word
    input:
      title: 电脑体检报告
      content: $steps.rep.text
      path: $params.out
---

# pc-checkup

读磁盘/系统数据 → llm 判读 → 存 Word 体检报告。
只依据真实数据，缺项跳过，不编配置。
