# Heurion SDK — 设计文档

## 目标

前后端解耦，支持多种消费端:

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│  Web UI  │   │  CLI     │   │  VS Code │   │  Mobile  │
└────┬─────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘
     │              │              │              │
     └──────────────┼──────────────┼──────────────┘
                    │
              ┌─────▼─────┐
              │ @heurion/ │   ← SDK (npm package)
              │    sdk    │
              └─────┬─────┘
                    │ HTTP + SSE
              ┌─────▼─────┐
              │  Backend  │
              └───────────┘
```

## 包结构

```
packages/sdk-client/              npm: @heurion/sdk
├── package.json
├── tsconfig.json
│
├── src/
│   ├── index.ts                   # 统一导出
│   ├── client.ts                  # HeurionClient 主类
│   ├── types.ts                   # ⚡ 与 backend/generated/types.ts 共享
│   │
│   ├── core/
│   │   ├── http-client.ts         # fetch 封装 (token, error, retry)
│   │   ├── token-store.ts         # 可插拔 token 管理
│   │   ├── stream-parser.ts       # SSE → AsyncGenerator
│   │   └── errors.ts              # 类型化错误
│   │
│   └── modules/                   # 10 个业务模块 (每个 < 100 行)
│       ├── auth.ts                # login, register, getProfile, updateProfile
│       ├── chat.ts                # sendMessage (SSE generator), sessions CRUD
│       ├── patients.ts            # list, get, create, delete, archive
│       ├── research.ts            # studies, roster, eligibility, safety
│       ├── documents.ts           # docs CRUD, polish SSE, docChat SSE, PHI scan
│       ├── skills.ts              # search, install, toggle, uninstall
│       ├── settings.ts            # LLM status, test, update
│       ├── files.ts               # upload (FormData), list, delete
│       ├── admin.ts               # listUsers, disable, enable, resetPassword
│       └── memory.ts              # projection, findings, timeline, medications
│
└── __tests__/                     # 各模块单元测试
```

## HeurionClient — 核心类设计

```typescript
import { HeurionClient } from '@heurion/sdk'

// ── 浏览器使用 ──
const heurion = new HeurionClient({
  baseUrl: 'http://localhost:8001',
  tokenStore: 'localStorage',       // 浏览器: localStorage | sessionStorage | custom
})

// ── CLI / Node.js 使用 ──
const heurion = new HeurionClient({
  baseUrl: 'http://localhost:8001',
  tokenStore: {
    get: () => fs.readFileSync('.heurion-token', 'utf-8'),
    set: (t) => fs.writeFileSync('.heurion-token', t),
    clear: () => fs.unlinkSync('.heurion-token'),
  },
})

// ── 用法 ──
const session = await heurion.auth.login('HZ', 'password')
console.log(session.role)  // 'admin'

// SSE 流式对话
for await (const chunk of heurion.chat.sendMessage('analyze this CT scan')) {
  if (chunk.type === 'final_answer_chunk') process.stdout.write(chunk.text)
}

// CRUD
const study = await heurion.research.createStudy('Lung Trial', 'LC001')
const docs = await heurion.documents.list()
```

## 模块方法签名 (10 个模块)

```typescript
// ── auth ──
heurion.auth.register(input: RegisterInput) => AuthSession
heurion.auth.login(username, password)     => AuthSession
heurion.auth.getProfile()                  => UserProfile
heurion.auth.updateProfile(data)           => UserProfile

// ── chat ──
heurion.chat.sendMessage(opts: SendChatOptions) => AsyncGenerator<ChatStreamChunk>  // SSE
heurion.chat.listSessions(archived?)       => Session[]
heurion.chat.createSession(title)          => Session
heurion.chat.deleteSession(id)             => void

// ── patients ──
heurion.patients.list()                    => Patient[]
heurion.patients.get(hash)                 => PatientDetail
heurion.patients.register(data)            => Patient
heurion.patients.delete(hash)              => void

// ── research ──
heurion.research.listStudies()             => Study[]
heurion.research.createStudy(name, code)   => Study
heurion.research.getStudy(id)              => StudyDetail
heurion.research.getRoster(studyId)        => RosterEntry[]
heurion.research.enroll(studyId, hash, arm)
heurion.research.unenroll(studyId, hash)
heurion.research.scanEligibility(studyId)  => { scanned: number }
heurion.research.getSafety(studyId)        => SafetyStatus

// ── documents ──
heurion.documents.list()                   => Doc[]
heurion.documents.create(title)            => Doc
heurion.documents.get(id)                  => Doc
heurion.documents.update(id, data)         => Doc
heurion.documents.polish(id, sel, inst)    => AsyncGenerator<Poli shChunk>  // SSE
heurion.documents.docChat(id, message)     => AsyncGenerator<ChatChunk>     // SSE
heurion.documents.phiScan(id)              => PhiFinding[]
heurion.documents.getSnapshots(id)         => SnapshotEntry[]

// ── skills ──
heurion.skills.list()                      => InstalledSkill[]
heurion.skills.search(query, source)       => SearchResult[]
heurion.skills.install(identifier)         => void
heurion.skills.toggle(name, enabled)       => void
heurion.skills.uninstall(name)             => void

// ── settings ──
heurion.settings.getLlmStatus()            => LlmStatus
heurion.settings.updateLlm(input)          => void
heurion.settings.testLlm()                 => { ok; provider; model; latency }

// ── files ──
heurion.files.upload(file: File|Buffer, patientHash?) => FileItem
heurion.files.list(patientHash?)           => FileItem[]
heurion.files.delete(fileId)               => void

// ── admin ──
heurion.admin.listUsers()                  => AdminUser[]
heurion.admin.disableUser(id)              => void
heurion.admin.enableUser(id)               => void
heurion.admin.resetPassword(id, pw)        => void

// ── memory ──
heurion.memory.getProjection(hash)         => MemoryProjection
heurion.memory.getFindings(hash)           => Finding[]
heurion.memory.getTimeline(hash)           => TimelineEvent[]
heurion.memory.getMedications(hash)        => Medication[]
```

## SSE 流式设计

不暴露原始 fetch/ReadableStream，而是返回 TypeScript AsyncGenerator:

```typescript
// SDK 内部实现
async *sendMessage(opts: SendChatOptions): AsyncGenerator<ChatStreamChunk> {
  const response = await this.http.post('/api/v1/agent/chat', opts.body, {
    headers: { 'Accept': 'text/event-stream' }
  })
  const reader = response.body!.getReader()
  const parser = new SSEDecoder()

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    for (const chunk of parser.decode(value)) {
      yield JSON.parse(chunk.data) as ChatStreamChunk
    }
  }
}

// 调用方 (web)
for await (const chunk of heurion.chat.sendMessage({ text })) {
  if (chunk.type === 'final_answer_chunk') setMessages(prev => [...prev, chunk.text])
}

// 调用方 (CLI)
for await (const chunk of heurion.chat.sendMessage({ text: 'analyze CT' })) {
  if (chunk.type === 'final_answer_chunk') console.log(chunk.text)
}
```

## Token 管理 — 可插拔

```typescript
interface TokenStore {
  get(): string | null
  set(token: string): void
  clear(): void
}

// 内置实现
const browserStore: TokenStore = {
  get: () => localStorage.getItem('nexus-token'),
  set: (t) => localStorage.setItem('nexus-token', t),
  clear: () => localStorage.removeItem('nexus-token'),
}

const fileStore = (path: string): TokenStore => ({
  get: () => { try { return readFileSync(path, 'utf-8') } catch { return null } },
  set: (t) => writeFileSync(path, t),
  clear: () => { try { unlinkSync(path) } catch {} },
})

const memoryStore: TokenStore = (() => { let t: string | null = null; return { get: () => t, set: (v) => { t = v }, clear: () => { t = null } } })()
```

## CLI 集成方案

```bash
npm install -g @heurion/cli
heurion login
heurion chat "analyze patient CT scan"
heurion patients list
heurion research create-study "Lung Trial" LC001
```

CLI 直接用 SDK:

```typescript
// packages/cli/src/commands/chat.ts
import { HeurionClient } from '@heurion/sdk'
import { createFileTokenStore } from '@heurion/sdk/token-stores'

const heurion = new HeurionClient({
  baseUrl: process.env.HEURION_API || 'http://localhost:8001',
  tokenStore: createFileTokenStore('.heurion-token'),
})

export async function chatCommand(message: string) {
  for await (const chunk of heurion.chat.sendMessage({ text: message })) {
    if (chunk.type === 'final_answer_chunk') process.stdout.write(chunk.text)
    if (chunk.type === 'error') console.error(chunk.message)
  }
}
```

## Web 前端迁移

当前 `api-client.ts` (719 行) → 被 SDK 替换:

```typescript
// 之前: api-client.ts
import { api } from '@/lib/api-client'
const session = await api.login(username, password)

// 之后: 直接用 SDK
import { heurion } from '@/lib/heimion'  // 创建好的 singleton
const session = await heurion.auth.login(username, password)
```

## 类型共享

SDK 和 Backend 共享 `types.ts`。三种方案:

| 方案 | 优点 | 缺点 |
|------|------|------|
| A: SDK copy backend types | 简单,独立发布 | 需手动同步 |
| B: Monorepo shared package | 自动同步 | 耦合 repo 结构 |
| C: SDK 是 types 的 source of truth | SDK 先于 backend | backend 依赖 SDK |

推荐 **方案A**: SDK `types.ts` 从 backend 的 `generated/types.ts` 复制，保持独立可分发的 npm 包。CI 中加 lint 检查是否同步。

具体执行: backend `generated/types.ts` 重命名为 `shared-types.ts` 或直接作为 SDK 的一部分。因为 SDK 是消费者最关心的类型定义来源。
