import dotenv from 'dotenv'
dotenv.config()

export const config = {
  port: parseInt(process.env.SERVER_PORT || '8001'),
  host: process.env.SERVER_HOST || '0.0.0.0',
  secret: process.env.SERVER_SECRET || 'dev-secret-key',
  environment: process.env.ENVIRONMENT || 'development',
  jwtAlgorithm: 'HS256' as const,
  jwtExpirationHours: parseInt(process.env.JWT_EXPIRATION_HOURS || '24'),
  databaseUrl: process.env.DATABASE_URL || 'file:./nexus_server.db',
  corsAllowOrigins: (process.env.CORS_ALLOW_ORIGINS || 'http://localhost:3000,http://localhost:5173').split(','),
}
