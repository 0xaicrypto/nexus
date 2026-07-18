import { PrismaClient } from '@prisma/client'
import bcrypt from 'bcryptjs'

async function main() {
  const prisma = new PrismaClient()
  const now = new Date().toISOString()
  const hash = await bcrypt.hash('hz123456', 10)

  // Just update password — don't delete (foreign keys)
  const existing = await prisma.user.findFirst({ where: { displayName: 'HZ' } })
  if (existing) {
    await prisma.user.update({
      where: { id: existing.id },
      data: { passwordHash: hash, role: 'admin', isAdmin: 1 }
    })
    console.log('HZ updated:', existing.id)
  } else {
    await prisma.user.create({
      data: {
        id: 'user_hz_admin',
        displayName: 'HZ',
        passwordHash: hash,
        role: 'admin',
        isAdmin: 1,
        createdAt: now,
        updatedAt: now,
      }
    })
    console.log('HZ created')
  }
  console.log('Password: hz123456')
  await prisma.$disconnect()
}

main()
