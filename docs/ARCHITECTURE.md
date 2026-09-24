# Tiger.M.M Architecture

## Decision Pipeline
User Input -> Input Guardrails -> Intent Reasoner -> Brain (13-layer) -> Model Client -> ChainEngine -> Output Guardrails -> Inbox Approval -> Response

## Kernel Architecture
- Hermes Kernel: StealthSession, JSReverseEngineer, AntiBlockEngine, CrawlPlanner
- OpenClaw Kernel: Win32Core, WindowEngine, FileSystemEngine, ProcessInjector
- Hermes Bridge: 9 tools local direct connect

## Safety Layers (6-layer defense)
1. Input Guardrails (prompt injection / dangerous cmd)
2. Risk Classifier (5-tier: READ..NETWORK)
3. Inbox Queue (async approval + timeout)
4. Sandbox (isolated execution)
5. Output Guardrails (key/password redaction)
6. Security Audit (S+~D rating for skills/plugins)

## Key Design Decisions
- Batch command approval (not per-tool)
- Risk auto-escalation (READ skips inbox, NETWORK always needs approval)
- Session memory (/approve always persists)
- Self-evolution (AutoGuide: 463 rules learned)
