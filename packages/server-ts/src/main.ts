import { createApp } from './app'
import { config } from './config'

async function main() {
  const app = await createApp()
  await app.listen({ port: config.port, host: config.host })
  console.log(`Heurion TS backend listening on ${config.host}:${config.port}`)
}

main().catch(console.error)
