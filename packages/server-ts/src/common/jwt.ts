import jwt from 'jsonwebtoken'
import { config } from '../config'

export interface JwtPayload {
  userId: string
  role: string
  displayName: string
}

export function signToken(payload: JwtPayload): string {
  return jwt.sign(payload, config.secret, {
    algorithm: config.jwtAlgorithm,
    expiresIn: `${config.jwtExpirationHours}h`,
  })
}

export function verifyToken(token: string): JwtPayload {
  return jwt.verify(token, config.secret, {
    algorithms: [config.jwtAlgorithm],
  }) as JwtPayload
}

export function decodeToken(token: string): JwtPayload | null {
  try {
    return jwt.decode(token) as JwtPayload
  } catch {
    return null
  }
}
