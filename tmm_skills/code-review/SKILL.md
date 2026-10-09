---
name: code-review
version: "1.0"
description: "代码审查：读取代码文件，检查语法问题、安全漏洞、性能隐患、代码规范，给出改进建议和质量评分。"
answer_mode: true
permission: read
timeout: 180
tags: [代码审查, 代码检查, 代码质量, 安全检查, 代码规范]
triggers: [审查代码, 看一下代码, 代码有没有问题, 帮我 review, 代码检查, 代码质量怎么样, 帮我看看这个代码, 代码审查]
platforms: [windows, linux]
requires_tools: [file_ops, shell_exec, tiger_office]
params:
  path:
    type: path
    required: true
    desc: 要审查的代码文件或目录路径
  language:
    type: text
    required: false
    desc: 编程语言（缺省自动检测）
  out:
    type: path
    required: false
    desc: 审查报告保存位置（缺省落桌面）
steps:
  - id: read
    tool: file_ops
    action: read
    input:
      path: $params.path
      limit: 500
  - id: grep_sec
    tool: shell_exec
    action: run
    input:
      cmd: "findstr /S /N /I /C:\"eval\" /C:\"exec\" /C:\"subprocess\" /C:\"shell=True\" /C:\"SELECT * FROM\" /C:\"password\" /C:\"token\" /C:\"secret\" /C:\"hardcode\" \"$params.path\" 2>nul"
  - id: grep_todo
    tool: shell_exec
    action: run
    input:
      cmd: "findstr /S /N /I /C:\"TODO\" /C:\"FIXME\" /C:\"HACK\" /C:\"XXX\" /C:\"BUG\" \"$params.path\" 2>nul"
  - id: rep
    tool: llm
    input:
      system: "你是资深代码审查专家。只依据给出的代码内容作判断, 不许编造内容。"
      prompt: |
        依据下面的代码对文件 $params.path 做审查:
        一、概况: 文件类型、语言、大致行数、主要功能
        二、安全问题: SQL 注入、命令注入、硬编码密钥、eval/exec 滥用等
        三、性能隐患: 循环嵌套、不必要的重复计算、大数据未分页等
        四、代码规范: 命名、注释、函数长度、重复代码
        五、TODO/FIXME/HACK 标记汇总
        六、改进建议: 按优先级排列 (高/中/低)
        七、质量评分: 1-10 分，附简短理由
        硬规矩:
        - 只描述代码里真实看得到的; 不要推断未显示的代码
        - 安全问题必须给出具体行号和修复方案
        - 不要建议不存在的库或框架
        代码内容:
        $steps.read.output
        安全关键词搜索结果:
        $steps.grep_sec.output
        TODO/FIXME 搜索结果:
        $steps.grep_todo.output
      max_tokens: 2000
  - id: save
    tool: tiger_office
    action: write_word
    input:
      title: 代码审查报告
      content: $steps.rep.text
      path: $params.out
---