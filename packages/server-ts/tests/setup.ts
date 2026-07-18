import { createApp } from '../src/app.js'
import type { FastifyInstance } from 'fastify'

let app: FastifyInstance | null = null
let token: string | null = null

export async function getApp(): Promise<FastifyInstance> {
  if (!app) {
    process.env.NODE_ENV = 'test'
    app = await createApp()
    await app.ready()
  }
  return app
}

export async function getToken(): Promise<string> {
  if (!token) {
    const a = await getApp()
    const username = 'testadmin_' + Date.now()
    const res = await a.inject({
      method: 'POST', url: '/api/v1/auth/register',
      headers: { 'content-type': 'application/json' },
      payload: JSON.stringify({ username, password: 'test123456', display_name: 'Test Admin' }),
    })
    token = JSON.parse(res.payload).jwt_token
  }
  return token
}

export async function authHeader() {
  return { authorization: `Bearer ${await getToken()}` }
}
