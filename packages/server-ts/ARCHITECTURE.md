# Heurion Backend — 进化回路架构

## 数据回路 (Evolution Pipeline)

每一次用户交互都走完整的闭环:

```
1. INGEST    →  事件记录到不可变日志
     │           EventLog.append(chat_turn | file_upload | dicom_import)
     │
2. EXTRACT   →  LLM 从原始事件提取结构化洞察
     │           chat_ingester → findings, medications, timeline
     │           dicom_ingester → imaging_findings
     │           takeaway_extractor → qualitative_insights
     │
3. GRAPH     →  积累为患者临床图谱（累积、永不删除）
     │           clinical_graph_nodes (findings, medications, measurements)
     │           clinical_graph_edges (caused_by, related_to, contradicts)
     │
4. DISTILL   →  跨患者模式蒸馏为医生专属知识
     │           practitioner_facts (patterns across patients)
     │           practitioner_observations (per-encounter raw data)
     │
5. EVOLVE    →  智能体自我进化（可证伪、可回滚）
     │           5 个命名空间独立版本化:
     │           Facts ← 新事实确认
     │           Episodes ← 会话摘要
     │           Skills ← 学到的策略
     │           Persona ← 沟通风格适配
     │           Knowledge ← 长文蒸馏
     │           VerdictRunner ← 观察后裁决(kept|warning|reverted)
     │
6. RETRIEVE  →  为下一轮对话投影相关记忆
                 projection_memory(events, task, budget)
                 上下文注入 system prompt → 影响下一轮对话
```

## 模块映射

```
modules/
├── ingestion/           # 回路第1步: 原始事件摄入
│   ├── chat.router.ts
│   ├── files.router.ts
│   └── dicom.router.ts
│
├── extraction/          # 回路第2步: LLM 提取结构化数据
│   ├── chat-extractor.ts     # 对话 → findings/meds/timeline
│   ├── dicom-extractor.ts    # 影像 → imaging findings
│   └── takeaway-extractor.ts # 会话 → 定性洞察
│
├── graph/               # 回路第3步: 患者临床图谱
│   ├── graph.store.ts        # nodes + edges CRUD (只追加)
│   ├── graph.query.ts        # 患者视角查询
│   └── graph.cache.ts        # T1 预计算视图
│
├── practitioner/        # 回路第4步: 跨患者医生知识
│   ├── facts.store.ts        # 模式化事实
│   ├── observations.store.ts # 单次观察
│   └── composer.ts           # 组装 system prompt 上下文
│
├── evolution/           # 回路第5步: 智能体自我进化
│   ├── stores/
│   │   ├── facts.store.ts
│   │   ├── episodes.store.ts
│   │   ├── skills.store.ts
│   │   ├── persona.store.ts
│   │   └── knowledge.store.ts
│   ├── evolvers/
│   │   ├── memory-evolver.ts
│   │   ├── skill-evolver.ts
│   │   ├── persona-evolver.ts
│   │   └── knowledge-compiler.ts
│   └── verdict-runner.ts     # 观察 → 裁决 → 回滚
│
├── retrieval/           # 回路第6步: 记忆投影
│   ├── memory-projection.ts  # 为每轮对话选择相关记忆
│   ├── tier-classifier.ts    # T1/T2/T3 三级路由
│   └── vector-search.ts      # 语义检索
│
├── chat/                # 用户界面层: 对话编排
│   ├── chat.router.ts        # SSE 流式对话
│   ├── chat.orchestrator.ts  # 串联回路 1→2→3→4→5→6
│   └── session.router.ts     # 会话管理
│
├── patients/            # 用户界面层: 患者管理
│   ├── patients.router.ts    # CRUD
│   └── patients.query.ts     # 聚合查询
│
├── research/            # 用户界面层: 研究工作台
│   ├── studies.router.ts
│   ├── roster.router.ts
│   ├── eligibility.ts
│   └── safety.ts
│
├── documents/           # 用户界面层: 写作工作台
│   ├── docs.router.ts
│   ├── polish.ts
│   └── phi-scanner.ts
│
├── auth/                # 基础设施
├── settings/
├── skills/
├── admin/
│
└── core/                # 共享基础设施
    ├── event-log.ts          # 不可变追加日志 (SQLite)
    ├── contracts.ts          # 行为契约 + 漂移评分
    ├── llm-client.ts         # LLM 抽象层
    ├── tools/                # 工具框架
    └── versioned-store.ts    # 版本化存储原语
```

## 回路编排器 (Chat Orchestrator)

这是核心 —— 一次对话怎么走完整回路:

```typescript
// modules/chat/chat.orchestrator.ts

async function chatTurn(input: ChatInput, userId: string) {
  // Step 1: 记录用戶消息到不可变日志
  yield* ingest(input, userId)

  // Step 2: 投影相关记忆 (回路第6步 → 闭环)
  const context = await projectMemory(userId, input)

  // Step 3: LLM 回复 (流式 SSE)
  yield* llmStream(context, input)

  // Step 4: 异步后处理 (不阻塞回复)
  schedule(async () => {
    await extractInsights(userId, input)      // 回路第2步
    await updateGraph(userId, input)          // 回路第3步
    await distillFacts(userId)                // 回路第4步
    await maybeEvolve(userId)                 // 回路第5步
  })
}
```

## 关键不变式 (来自原始架构)

1. **EventLog 是唯一真相源** — 所有数据通过事件溯源写入，投影表是派生视图
2. **进化必须可证伪** — 每次编辑发 proposal → 观察窗口 → 裁决 → 可回滚
3. **5 个命名空间独立版本化** — 各进化器不互相阻塞
4. **每件事是可追加的** — 没有静默删除，没有覆盖
