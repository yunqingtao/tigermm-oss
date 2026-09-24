# Tiger.M.M (MARY III) 架构文档

## 一、项目结构

```
mary3/
├── main.py                 # 入口: preflight → startup → CLI
├── core/
│   ├── pipeline.py         # Level4Pipeline: 请求处理总调度
│   ├── intent_router.py    # 意图分类 + 链式执行计划
│   ├── model_router.py     # 云端模型调度 (deepseek/mimo)
│   ├── model_client.py     # LLM API 客户端
│   ├── tool_gateway.py     # 工具调用网关
│   ├── tool_orchestrator.py # 工具编排 + 回退链
│   ├── plugin_manager.py   # 插件扫描 + 安全管理
│   ├── skill_registry.py   # 技能注册中心 (v2 schema)
│   ├── skill_executor.py   # 技能执行器
│   ├── task_planner.py     # 任务规划 + 计划执行
│   ├── hive_mind.py        # 多Agent并行集群
│   ├── perception.py       # 感知引擎 (模式学习+建议)
│   ├── memory.py           # 短期记忆 (会话上下文)
│   ├── memory_long.py      # 长期记忆 (SQLite)
│   ├── memory_quality.py   # 记忆质量追踪
│   ├── memory_vector.py    # 向量记忆
│   ├── memory_governor.py  # 记忆统一治理
│   ├── compactor.py        # 对话压缩
│   ├── code_engine.py      # 代码执行引擎
│   └── session_manager.py  # 会话管理
├── tools/                  # 27个工具插件
│   ├── check_mail.py       # POP3 邮件
│   ├── send_email.py       # SMTP 邮件
│   ├── openmeteo.py        # 天气
│   ├── file_ops.py         # 文件操作
│   ├── whisper.py          # 语音转文字
│   ├── web_search.py       # 网络搜索
│   └── ...
├── skills/                 # 技能定义 (skill.json)
├── data/                   # 数据库 + 配置
└── docs/                   # 文档
```

## 二、请求处理流程

```
用户输入
  → pipeline.process()
  → Layer 0: /slash 命令 (/plan, /hive, /skills, /memory, /stats, /cost)
  → Layer 0.5: 确认执行 (y/n)
  → Layer 1: 空输入/噪音过滤
  → Layer 2: intent_router.classify() → 链式执行
  → Layer 3: SkillRegistry.match() → SkillExecutor
  → Layer 4: ModelRouter → deepseek/mimo 处理
  → 返回响应
```

## 三、命令面板

| 命令 | 功能 |
|------|------|
| /plan <目标> | 自主任务规划 (deepseek拆解→执行) |
| /hive <目标> | 多Agent并行任务 |
| /skills | 技能列表 + 成功率 |
| /memory | 记忆统计 |
| /memory prune | 记忆修剪 |
| /stats | 运行统计 |
| /cost | Token用量 |
| /tool <名> <参> | 直接调工具 |

## 四、关键设计决策

1. ModelRouter 只调度云端模型，不涉及 Ollama
2. 工具命名: check_mail, send_email 等 (动词_名词)
3. 每次改动后清 __pycache__
4. Skill 触发按 pattern 长度排序 (越具体越优先)
5. PlanExecutor 自动修复 deepseek 的空对象占位
6. POP3 read 强制新连接 (避免 list→read 复用)
7. Python .format() 中 { } 需转义为 {{ }}
