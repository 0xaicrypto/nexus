import { z } from 'zod'

export const createStudySchema = z.object({
  name: z.string().min(1).max(200),
  shortCode: z.string().min(1).max(20).regex(/^[A-Z0-9_-]+$/, 'Short code must be uppercase alphanumeric'),
})

export const enrollPatientSchema = z.object({
  patientHash: z.string().min(1),
  arm: z.string().optional(),
})

export const confirmObservationSchema = z.object({
  confirmed: z.boolean(),
  grade: z.number().min(0).max(5).optional(),
  dlt: z.boolean().optional(),
  note: z.string().optional(),
})

export const completeAssessmentSchema = z.object({
  completedAt: z.string().optional(),
  note: z.string().optional(),
})

export type CreateStudyInput = z.infer<typeof createStudySchema>
export type EnrollPatientInput = z.infer<typeof enrollPatientSchema>
export type ConfirmObservationInput = z.infer<typeof confirmObservationSchema>
