// Quick smoke test for @heurion/sdk
// Run: npx tsx test-sdk.ts

import { HeurionClient, memoryStore } from './dist/index.js'

async function main() {
  // Node.js doesn't have localStorage → use memoryStore
  const h = new HeurionClient({ baseUrl: 'http://localhost:8001', tokenStore: memoryStore })

  // 1. Login
  const session = await h.auth.login('HZ', 'hz123456')
  console.log('✅ login:', session.role, session.user_id.slice(0, 16))

  // 2. Profile
  const profile = await h.auth.getProfile()
  console.log('✅ profile:', profile.display_name)

  // 3. Sessions
  const { sessions } = await h.chat.listSessions()
  console.log('✅ sessions:', sessions.length)

  // 4. Research CRUD
  const study = await h.research.createStudy('SDK Test Study', 'SDK001')
  console.log('✅ createStudy:', study.name, study.shortCode)
  const studies = await h.research.listStudies()
  console.log('✅ listStudies:', studies.length, 'studies')

  // 5. Skills search
  const { results } = await h.skills.search('clinical')
  console.log('✅ skills search:', results.length, 'results')

  // 6. Settings
  const llm = await h.settings.getLlmStatus()
  console.log('✅ llm status:', llm.provider)

  // 7. Patients
  const patients = await h.patients.list()
  console.log('✅ patients:', patients.length)

  // 8. Documents
  const { docs } = await h.documents.list()
  console.log('✅ documents:', docs.length)

  // 9. Memory (stub)
  const proj = await h.memory.getProjection('test')
  console.log('✅ memory projection: ok')

  // 10. Admin
  const { users } = await h.admin.listUsers()
  console.log('✅ admin users:', users.length)

  // 11. Chat SSE
  console.log('✅ chat SSE:')
  for await (const chunk of h.chat.sendMessage({ text: 'Hello SDK!' })) {
    if (chunk.type === 'final_answer_chunk') console.log('   ', chunk.text.slice(0, 50) + '...')
    if (chunk.type === 'turn_complete') console.log('   turn complete')
  }

  console.log('\n🎉 All tests passed!')
}

main().catch(err => {
  console.error('❌', err.message)
  process.exit(1)
})
