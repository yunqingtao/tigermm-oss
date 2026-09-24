---
name: artifacts-inventory
version: "1.0"
description: "产物清单：回答「今天产出了什么 / 成品在哪 / 最近生成的报告图表格」，只读不移动文件"
# 查询类技能: 形态是「问一句 -> 技能取数 -> 模型总结」, 没有"写/生成"字样也没有缓存路径。
# 必须声明 answer_mode, 否则接不到请求 (会落到通用兜底)。
answer_mode: true
permission: read
timeout: 120
tags: [产物清单, 交付物在哪, 最近生成的文件]
# tags 用有辨识度的词组 (禁单概念泛词 —— 泛词会抢无关请求)
triggers: [产出了什么, 产出了啥, 交付物, 产物清单, 成品在哪, 成片在哪, 今天做了什么,
           生成了什么, 生成的文件在哪, 最近生成, 输出文件, 文件清单, 片子在哪]
platforms: [windows, linux]
requires_tools: [artifacts, llm]
params:
  kind:
    type: text
    required: false
    desc: 只看某一类 (video/audio/image/doc/sheet/slide/code)
  days:
    type: number
    required: false
    desc: 盘点最近几天 (默认 1)
steps:
  - id: inv
    tool: artifacts
    action: list
    input:
      kind: $params.kind
      limit: 20
    output: data
    on_error: stop
  - id: scan
    tool: artifacts
    action: scan
    input:
      days: $params.days
      limit: 20
    output: data
    on_error: continue
  - id: rep
    tool: llm
    input:
      system: "你是交付助理。只依据给出的**真实**清单汇报，不许编造文件名、路径或数量。清单为空就直说没有。不要开场白。"
      prompt: |
        下面两组数据都来自真实文件系统（第 1 组是**登记过的**产物，第 2 组是**目录盘点**发现的）：

        【登记过的产物】
        $steps.inv.data

        【目录盘点（未登记，只读发现）】
        $steps.scan.data

        请用中文写一份清单（6 行以内）：
        1. 一句总量（几个、合计多大；没有就说"暂无产物"）
        2. 逐条列出**最近**的几个：时间 · 类型 · 大小 · **完整路径**
        3. 如果盘点到的东西没在登记里，最后加一句提示可以登记（一句即可）
        路径必须原样照抄，不要简写、不要编造。
      temperature: 0.2
      max_tokens: 900
    output: text
    on_error: stop
---

# artifacts-inventory（产物清单）

**触发**：产出了什么 / 交付物 / 成品在哪 / 最近生成的文件 / 片子在哪。

**做什么**：两条腿一起用，都**只读**：
```text
① list   — 读产物登记表 (data/artifacts.jsonl) 里**登记过**的产物
② scan   — 盘点常见目录里最近 N 天的产物 (桌面 / 项目 reports,tmp), 无需事先登记
```
然后让模型把两组数据合成一份带**完整路径**的清单。

**登记表**（append-only，只追加，不改用户文件）
```text
路径: data/artifacts.jsonl      (可用 TMM_ARTIFACTS_FILE 覆盖 → 门禁/探针隔离用)
条目: path · name · kind · title · size · mtime · at · source · tags
去重: 同一路径只保留最新一条
诚实: ① 登记一个**不存在**的路径会被拒绝 (不制造假信息)
      ② 查询时**过滤掉已不在盘上**的记录 (不给过期结论)
```

**边界**
- 不会移动/删除/改名任何文件 —— 它只是"记账 + 盘点"。
- `scan` 只认已知产物后缀（视频/音频/图/文档/表格/幻灯片/代码），跳过
  `.git` `__pycache__` `backups` `AppData` 等目录；可用 `days` 控制时间窗。
- 想让它记住某个产物：让引擎调 `artifacts` 的 `add`（或以后由产出类工具自动登记）。
