# Tiger.M.M 下一阶段进阶计划

## 一、当前已落地

| 模块 | 状态 | 说明 |
|------|------|------|
| intent_router | DONE | 14 action + 3 delivery, 零模型路由 |
| POP3 收发 | DONE | 连接池/重试/keyword过滤/附件 |
| PerceptionEngine | DONE | 主动合成 suggest() -> prompt 注入 |
| 链式执行 | DONE | orchestrator 回退 + 依赖感知断链 |
| openmeteo | DONE | 中文输出 + WMO 天气码 |
| ModelRouter | DONE | 成本感知 + 成功率学习 |
| MemoryGovernor | DONE | 四层统一治理 + /memory 面板 |
| Skill 生态 | DONE | SkillRegistry + v2 schema + /skills |
| SkillExecutor | DONE | $params/$steps 解析 + 逐步执行 |
| TaskPlanner | DONE | deepseek 拆解 + PlanExecutor 执行 |
| 启动自愈 | DONE | Layer 1 预检 + Layer 2 优雅降级 |

## 二、未完成

| 任务 | 优先级 | 预估行数 |
|------|--------|----------|
| A. PlanExecutor 稳定性补丁 | P0 | ~80 |
| B. Watchdog 守护 | P1 | ~150 |
| C. HiveMind 多Agent | P2 | ~400 |
| D. 多模态感知 | P3 | ~200 |

## 三、A. PlanExecutor 稳定性补丁

已知问题:
- POP3 连接在 list->read 链中复用导致 read 失败 (已修: read 强刷连接)
- deepseek 有时生成硬编码 ID 而非 $step1.emails[0].id 引用
- 实体解析只在 send_email.to 上触发
- 路径解析仅支持"桌面"

修改点:
- check_mail: stat 也强刷新连接
- task_planner: 实体解析扩展到所有含 name/recipient/to 的 param
- task_planner: 路径解析补全 文档/下载/D盘

## 四、B. Watchdog 守护进程

架构:
  watchdog.py (独立进程, 纯标准库)
    -> 启动 main.py --cli (subprocess.Popen)
    -> 捕获 stdout/stderr/exit_code
    -> 解析 traceback -> 匹配已知故障模式
    -> 执行修复 (删WAL / 恢复key / 切conda)
    -> 重启 (最多3次, 间隔 2s/5s/10s)

故障匹配:
  ModuleNotFoundError -> cd 到 PROJECT_ROOT
  sqlite3.OperationalError -> 删 .db-wal + .db-shm
  PermissionError .crypto.key -> 从备份恢复
  其他未知 -> 记录 crash_log, 通知用户

## 五、C. HiveMind 多Agent集群

概念: 主Agent把子任务分发给多个子Agent，
每个子Agent是独立pipeline实例，只能访问特定工具集。

示例:
  用户: "分析邮件情感，查天气，画架构图"
  主Agent ->
    子Agent1 (check_mail+send_email): 读邮件, 判断情感
    子Agent2 (openmeteo): 查天气
    子Agent3 (plantuml): 画架构图
  主Agent 汇总 -> "邮件偏中性, 北京晴31C, 架构图已保存"

核心文件:
  core/hive_mind.py: HiveMind 主调度器
    class SubAgent: 独立 pipeline + 工具白名单
    class HiveMind: 拆解 -> 分发 -> 汇总

风险:
- 子Agent数量多 -> API调用成本高
- 子Agent间无法通信
- 需要并发控制 (Semaphore)

## 六、D. 多模态感知

已有: OCR + Whisper + 截图
缺失: 三者串联, 语音输入->pipeline->TTS回复
方案: 截图->OCR提取文字->deepseek分析 (不需要真正视觉理解)

## 七、实施顺序

  Week 1: A (PlanExecutor稳定性) -> 30分钟
  Week 1: B (Watchdog) -> 2小时
  Week 2: C (HiveMind核心) -> 4-6小时
  Week 3: D (多模态串联) -> 2小时
