import { z } from 'zod'

export const loginSchema = z.object({
  username: z.string().min(1),
  password: z.string().min(1),
})

export const registerSchema = z.object({
  username: z.string().min(1).max(64),
  password: z.string().min(6).max(128),
  displayName: z.string().min(1).max(128).optional(),
})

export const claimSchema = z.object({
  username: z.string().min(1),
  password: z.string().min(6).max(128),
})

export type LoginInput = z.infer<typeof loginSchema>
export type RegisterInput = z.infer<typeof registerSchema>
export type ClaimInput = z.infer<typeof claimSchema>
