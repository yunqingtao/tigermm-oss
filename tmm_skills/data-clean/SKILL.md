---
name: data-clean
version: "1.0"
description: "数据清洗：读取 CSV/表格文件，检测并处理重复行、空值、格式不一致、异常值，输出清洗后的文件和清洗报告。"
answer_mode: true
permission: write
timeout: 180
tags: [数据清洗, 去重, 空值处理, CSV清理, 表格清洗]
triggers: [清洗数据, 清理表格, 去重, 处理空值, 数据整理一下, CSV 清洗, 表格里有重复, 数据有脏数据, 帮我洗一下数据]
platforms: [windows, linux]
requires_tools: [file_ops, table_ops, tiger_office]
params:
  path:
    type: path
    required: true
    desc: 要清洗的数据文件路径（CSV/XLSX）
  out:
    type: path
    required: false
    desc: 清洗后文件保存位置（缺省在原文件同目录，文件名加 _cleaned）
  report:
    type: path
    required: false
    desc: 清洗报告保存位置（缺省落桌面）
steps:
  - id: read
    tool: file_ops
    action: read
    input:
      path: $params.path
      limit: 100
  - id: stats
    tool: table_ops
    action: info
    input:
      path: $params.path
  - id: dup
    tool: table_ops
    action: dedupe
    input:
      path: $params.path
  - id: rep
    tool: llm
    input:
      system: "你是数据清洗专家。只依据给出的数据统计作判断, 不许编造内容。"
      prompt: |
        依据下面的数据对文件 $params.path 的清洗结果做总结:
        一、概况: 原始数据的列名、行数、数据类型
        二、发现的问题: 重复行数、空值分布、格式不一致、异常值
        三、已执行的清洗操作: 去重、空值填充/删除、格式统一
        四、清洗后数据的变化: 行数变化、各列空值情况
        五、建议: 是否需要进一步人工检查的列或行
        硬规矩:
        - 只描述数据里真实看得到的; 缺哪项数据就直说缺, 不要推断
        - 具体数字必须来自原始数据, 不许编造
        原始数据前100行:
        $steps.read.output
        数据统计:
        $steps.stats.output
        重复行情况:
        $steps.dup.output
      max_tokens: 1500
  - id: save
    tool: tiger_office
    action: write_word
    input:
      title: 数据清洗报告
      content: $steps.rep.text
      path: $params.out
---