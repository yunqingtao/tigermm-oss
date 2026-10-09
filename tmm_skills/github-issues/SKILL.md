---
name: github-issues
version: "1.0"
description: "GitHub 巡检：看我的仓库、待办 issue、待办 PR，并给出该先动哪个的判断"
# 查询类技能: 形态是「问一句 -> 技能取数 -> 模型总结」, 没有"写/生成"字样也没有文件路径
# 所以声明 answer_mode 豁免产出守卫 (否则永远接不到, 会落到通用兜底)
answer_mode: true
permission: read
timeout: 180
tags: [GitHub巡检, 待办Issue, 仓库清单]
# tags 用有辨识度的词组 (禁单概念泛词 —— 泛词会抢无关请求)
triggers: [github, GitHub, 我的仓库, 我的github, 待办issue, 待办 issue, 待办事项,
           有什么issue, 我的issue, issue 有哪些, 有哪些 issue, 待办PR, 我的PR,
           代码仓库, 开源仓库, 仓库动态, github动静]
platforms: [windows, linux]
requires_tools: [github_api, llm]
params:
  repo:
    type: text
    required: false
    desc: 指定仓库 (owner/repo，建议写进引号)；不给则汇总最近的仓库
  state:
    type: text
    required: false
    desc: issue/PR 状态 (open / closed)，默认 open
steps:
  - id: gh
    tool: github_api
    action: digest
    input:
      repo: $params.repo
      limit: 5
    output: data
    on_error: stop
  - id: rep
    tool: llm
    input:
      system: "你是研发助理。只依据给出的 GitHub 查询结果汇报，不许编造仓库名、issue 号或数量。没查到就说没有。"
      prompt: |
        下面是刚从 GitHub API 取到的**真实**数据：

        $steps.gh.data

        请写一份简短巡检（5 行以内）：
        1. 身份与仓库：一句话
        2. 待办 issue / PR：逐条列出（没有就说"无待办"）
        3. 判断：如果只有 1~2 条积压就直说"不用管"；有 3 条以上才建议先动哪条，并说明理由
        不要开场白，不要"根据数据"这类套话。
      temperature: 0.3
      max_tokens: 800
    output: text
    on_error: stop
---

# github-issues（GitHub 巡检）

**触发**：github / 我的仓库 / 待办 issue / 我的 PR / 代码仓库动态。

**做什么**：真打 GitHub REST API（**不用 gh CLI** —— 本机没装，也省一个外部安装），
一次调用取回"身份 + 仓库列表 + 待办 issue + 待办 PR"，再让模型写 5 行巡检。

**凭据**（本机，不进 git）
```text
读取顺序: 环境变量 GH_TOKEN → keys_github.json → keys.json["github"]
keys_github.json 被 .gitignore 的 keys*.json 规则覆盖 → 不会进仓库
没有 token 时工具**诚实报错**并告诉你怎么配 (不会假装查到了)
```

**为什么用 digest 一次取全**：技能 DAG 是**静态**的（步骤写死），没法按问法分支。
所以把"身份/仓库/issue/PR"聚合成一个 `digest` 动作 —— 一句话问什么都能答。

**边界**
- 只读巡检是主用法；`create_issue` 是工具里唯一的写操作（需显式 `owner/repo` + 标题）。
- 指定单个仓库：`repo` 参数走规则抽取（引号最稳）；随手写的 "yunqingtao/tigermm-skills"
  没有引号时抽不到 → 会退化成汇总最近 5 个仓库（**不会报错、不会猜**）。
