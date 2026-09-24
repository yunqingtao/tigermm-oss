---
name: diagram
description: "Generate architecture diagrams, flowcharts, and sequence diagrams using PlantUML."
tags: [画图, 架构图, 流程图, 时序图, 图表, 可视化, diagram, plantuml, architecture]
# ★ 2026-09-19: 去掉裸"画" —— 它把所有生图请求都吞了("画一张柴犬"→diagram 要 dsl)。
#   本技能画的是 PlantUML **结构图**, 触发词必须是结构图语义。
triggers: [架构图, 流程图, 时序图, 设计图, uml, plantuml, 类图, 状态图, 甘特图, draw diagram, generate diagram, architecture diagram]
platforms: [windows, linux]
requires_tools: [plantuml]
params:
  dsl:
    type: text
    required: false
    desc: PlantUML 源码（可省略 —— 会由模型根据你的描述生成）
  path:
    type: path
    required: false
    desc: 输出位置（缺省落桌面）
steps:
  # ★ 2026-09-19 深测修: 原来只有一步 plantuml(content=$params.dsl), 而 dsl 是**必填**
  #   且没有任何步骤去生成它 → 用户按技能自己的触发词说话 ("画个架构图") **必然**
  #   得到 "✗ diagram 缺必填参数: dsl"。现在前面加一个 llm 步骤把描述变成 PlantUML 源码。
  - id: plan
    tool: llm
    input:
      system: 你是 PlantUML 专家，输出可直接编译的源码。
      prompt: |
        用户的需求：$message

        请输出 PlantUML 源码。要求：
        - 必须以 @startuml 开头、@enduml 结尾
        - 按需求选图形类型：架构/组件/流程/时序/类/ER/状态/甘特
        - 只输出源码，不要解释、不要 markdown 代码块标记
      temperature: 0.3
      max_tokens: 1200
    output: text
    on_error: stop
  - id: draw
    tool: plantuml
    input:
      # ★ 必须用 plantuml 的真实参数名 `code` —— 原技能写的是 `content`,
      #   而 plantuml.run(code=...) 没有 content 参数 → 源码传不进去 →
      #   "No PlantUML code provided" (旧技能一直坏在这, 深测才暴露)。
      code: $steps.plan.text
      path: $params.path
    on_error: stop
---

# Diagram Skill

Generate professional diagrams using PlantUML.

## When to use
- User asks to draw/draw/generate any kind of diagram
- Architecture diagrams, flowcharts, sequence diagrams, class diagrams
- Visual representation of system design

## Steps
1. Understand what kind of diagram the user wants
2. Write PlantUML code in the appropriate syntax
3. Call `plantuml` tool with the PlantUML code as the content parameter
4. The tool will generate a PNG and save it to the user's Desktop
5. Tell the user the file path

## PlantUML Syntax Reference
- Sequence diagram: `@startuml\nAlice -> Bob: Hello\n@enduml`
- Component diagram: `@startuml\n[Component] --> [Database]\n@enduml`
- Use Chinese labels for Chinese users

## Constraints
- Always save output to Desktop
- Use black-gold theme colors where possible
- Keep diagrams clean and readable
