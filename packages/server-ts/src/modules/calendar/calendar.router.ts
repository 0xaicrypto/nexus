import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard'
import prisma from '../../common/prisma'

/**
 * Calendar export — iCal format so users can subscribe from Google/Apple Calendar.
 * Includes:
 *   - Study assessment due dates
 *   - Safety follow-up reminders
 *   - Patient follow-up events (from assessments)
 *
 * User subscribes once, calendar auto-updates on each refresh.
 */
export async function calendarRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  app.get('/api/v1/calendar/export.ics', async (request, reply) => {
    const userId = request.user!.userId
    const now = new Date()
    const lines: string[] = [
      'BEGIN:VCALENDAR',
      'VERSION:2.0',
      'PRODID:-//Heurion//Clinical Calendar//EN',
      'X-WR-CALNAME:Heurion Clinical Schedule',
    ]

    // 1. Research study assessments
    const assessments = await (prisma as any).researchAssessment.findMany({
      where: { study: { userId } },
      include: { study: true },
    })
    for (const a of assessments) {
      if (a.completedAt) continue
      const due = new Date(a.dueAt)
      lines.push(
        'BEGIN:VEVENT',
        `UID:heurion-assessment-${a.id}`,
        `DTSTART:${toICSDate(due)}`,
        `SUMMARY:📋 ${a.title || a.visit} — ${a.study?.shortCode || ''}`,
        `DESCRIPTION:Study: ${a.study?.name || ''}\\nVisit: ${a.visit}\\nPatient: ${a.patientHash}`,
        'CATEGORIES:Research',
        'END:VEVENT',
      )
    }

    // 2. Safety follow-up reminders (30 days after last observation)
    const studies = await (prisma as any).researchStudy.findMany({ where: { userId } })
    for (const study of studies) {
      const safetyDate = new Date(study.updatedAt)
      safetyDate.setDate(safetyDate.getDate() + 30)
      if (safetyDate > now) {
        lines.push(
          'BEGIN:VEVENT',
          `UID:heurion-safety-${study.id}`,
          `DTSTART:${toICSDate(safetyDate)}`,
          `SUMMARY:🔬 Safety Review — ${study.shortCode}`,
          `DESCRIPTION:30-day safety follow-up for ${study.name}`,
          'CATEGORIES:Safety',
          'END:VEVENT',
        )
      }
    }

    // 3. Scheduled tasks
    const tasks = await (prisma as any).session.findMany({
      where: { userId, archived: 0 },
      orderBy: { lastMessageAt: 'desc' },
      take: 5,
    })
    for (const t of tasks) {
      if (!t.lastMessageAt) continue
      const remind = new Date(t.lastMessageAt)
      remind.setDate(remind.getDate() + 7)
      if (remind > now) {
        lines.push(
          'BEGIN:VEVENT',
          `UID:heurion-followup-${t.id}`,
          `DTSTART:${toICSDate(remind)}`,
          `SUMMARY:📝 Follow-up — ${t.title || 'Patient chat'}`,
          `DESCRIPTION:Last message: ${t.lastMessageAt}. Session: ${t.id}`,
          'CATEGORIES:Follow-up',
          'END:VEVENT',
        )
      }
    }

    lines.push('END:VCALENDAR')

    reply.header('Content-Type', 'text/calendar; charset=utf-8')
    reply.header('Content-Disposition', 'inline; filename=heurion.ics')
    return lines.join('\r\n')
  })
}

function toICSDate(d: Date): string {
  return d.toISOString().replace(/[-:]/g, '').replace(/\.\d+/, '') + 'Z'
}
