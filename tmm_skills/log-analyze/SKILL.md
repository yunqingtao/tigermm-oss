---
name: log-analyze
version: "1.0"
description: "日志分析：读取日志文件，搜索 ERROR/WARN/Exception 等异常关键词，统计频次和分布，定位问题根因并给出排查建议。"
answer_mode: true
permission: read
timeout: 180
tags: [日志分析, 日志排查, 异常定位, 错误日志, 日志模式]
triggers: [分析日志, 看一下日志, 日志里有什么问题, 帮我查日志, 日志报错, 排查日志, 日志分析, 这个日志怎么回事, 读一下日志文件]
platforms: [windows, linux]
requires_tools: [file_ops, shell_exec, tiger_office]
params:
  path:
    type: path
    required: true
    desc: 要分析的日志文件路径
  keyword:
    type: text
    required: false
    desc: 额外要搜索的关键词（默认已覆盖 ERROR/WARN/Exception/FATAL/Traceback）
  out:
    type: path
    required: false
    desc: 报告保存位置（缺省落桌面）
steps:
  - id: head
    tool: file_ops
    action: read
    input:
      path: $params.path
      offset: 0
      limit: 200
  - id: grep_err
    tool: shell_exec
    action: run
    input:
      cmd: "powershell -NoProfile -Command \"(Select-String -Path '$params.path' -Pattern 'ERROR').Count\""
  - id: sample_err
    tool: shell_exec
    action: run
    input:
      cmd: "findstr /N /I /C:\"ERROR\" /C:\"WARN\" /C:\"Exception\" /C:\"FATAL\" /C:\"Traceback\" \"$params.path\" 2>nul"
  - id: tail
    tool: shell_exec
    action: run
    input:
      cmd: "powershell -NoProfile -Command \"Get-Content -Tail 50 '$params.path'\""
  - id: rep
    tool: llm
    input:
      system: "你是日志分析专家。只依据给出的日志数据作判断, 不许编造内容。"
      prompt: |
        依据下面的数据对日志文件 $params.path 做分析:
        一、概况: 文件开头部分 (判断是什么服务/应用的日志)
        二、异常统计: ERROR/WARN/Exception/FATAL/Traceback 的总命中行数, 以及自定义关键词 "$params.keyword" 的命中行数
        三、异常样例 (最多 10 条典型错误, 标注行号)
        四、根因分析: 根据错误模式判断可能的根本原因
        五、排查建议: 具体的下一步操作 (越具体越好)
        六、尾部最近日志: 最后 50 行中有无值得关注的内容
        硬规矩:
        - 只描述数据里真实看得到的; 缺哪项数据就直说缺, 不要推断
        - 行号必须来自原始数据, 不许编造
        文件开头:
        $steps.head.output
        异常总行数:
        $steps.grep_err.output
        自定义关键词命中行数:
        $steps.grep_kw.output
        异常样例:
        $steps.sample_err.output
        文件尾部:
        $steps.tail.output
      max_tokens: 2000
  - id: save
    tool: tiger_office
    action: write_word
    input:
      title: 日志分析报告
      content: $steps.rep.text
      path: $params.out
---