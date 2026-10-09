---
name: git-ops
version: "1.0"
description: "Git 操作：查看仓库状态、分支管理、提交推送、冲突排查、提交历史查看，一站式处理常见 Git 工作流。"
answer_mode: true
permission: write
timeout: 180
tags: [Git操作, 分支管理, 提交推送, 冲突排查, 仓库状态]
triggers: [git status, 提交一下, 推送到远程, 有没有冲突, git 操作, 分支管理, 帮我 commit, 帮我 push]
platforms: [windows, linux]
requires_tools: [shell_exec]
params:
  path:
    type: path
    required: false
    desc: 仓库路径（缺省为当前目录）
  action:
    type: text
    required: false
    desc: 要执行的操作（status/log/commit/push/branch/conflict，缺省 status）
  message:
    type: text
    required: false
    desc: 提交信息（commit 时必填）
  out:
    type: path
    required: false
    desc: 报告保存位置（缺省落桌面）
steps:
  - id: status
    tool: shell_exec
    action: run
    input:
      cmd: "cd \"$params.path\" && git status"
  - id: log
    tool: shell_exec
    action: run
    input:
      cmd: "cd \"$params.path\" && git log --oneline -20"
  - id: branch
    tool: shell_exec
    action: run
    input:
      cmd: "cd \"$params.path\" && git branch -a"
  - id: diff
    tool: shell_exec
    action: run
    input:
      cmd: "cd \"$params.path\" && git diff --stat"
  - id: rep
    tool: llm
    input:
      system: "你是 Git 助手。只依据给出的 Git 输出作判断, 不许编造内容。"
      prompt: |
        依据下面的 Git 信息给用户一份简报:
        一、当前状态: 分支、暂存区、工作区情况
        二、最近 20 条提交记录
        三、所有分支（本地+远程）
        四、待提交的变更统计
        五、建议: 接下来该做什么（是否需要 commit/push/merge）
        如果用户指定了操作 "$params.action"，则针对该操作给出具体命令和注意事项。
        提交信息（如有）: $params.message
        硬规矩:
        - 只描述 Git 输出里真实看得到的; 不要推断未显示的内容
        - 不要自动执行危险操作 (force push/reset --hard 等), 只给命令让用户确认
        Git 状态:
        $steps.status.output
        提交历史:
        $steps.log.output
        分支情况:
        $steps.branch.output
        变更统计:
        $steps.diff.output
      max_tokens: 1500
  - id: save
    tool: tiger_office
    action: write_word
    input:
      title: Git 操作报告
      content: $steps.rep.text
      path: $params.out
---