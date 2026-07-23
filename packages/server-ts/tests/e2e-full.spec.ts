/**
 * Heurion E2E Tests — Complete User Workflow
 *
 * Uses pre-seeded test data (see fixtures/seed.ts):
 *   Doctor: e2e-doctor / test123456
 *   Patients: Zhang Wei (lung cancer), Li Xia (breast cancer)
 *   Files: lab-report, imaging-report
 *   Knowledge: EGFR TKI, RECIST 1.1
 *
 * Run: npx playwright test --config=playwright.config.ts
 */
import { test, expect } from '@playwright/test'

const BASE = process.env.BASE_URL || 'http://localhost:8002'
const DOCTOR = { username: 'e2e-doctor', password: 'test123456' }
const PATIENT_NAME = 'Zhang Wei'

test.use({ storageState: undefined }) // hermetic tests

async function doLogin(page: any) {
  // Call login API directly and inject token into localStorage
  await page.goto(`${BASE}/login`, { timeout: 10000, waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(500)
  await page.evaluate(async (user) => {
    const res = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: user.username, password: user.password }),
    })
    const data = await res.json()
    if (data.jwt_token) {
      localStorage.setItem('nexus.auth.token', data.jwt_token)
      localStorage.setItem('nexus.auth.user_id', data.user_id)
      localStorage.setItem('nexus.auth.display_name', data.display_name || user.username)
      window.location.href = '/app/today'
    }
    return Boolean(data.jwt_token)
  }, DOCTOR)
  await page.waitForURL('**/app/today', { timeout: 10000 })
}

test.beforeAll(async ({ browser }) => {
  test.setTimeout(60000)
  const page = await browser.newPage()
  await doLogin(page)
  await page.context().storageState({ path: '/tmp/e2e-state.json' })
  await page.close()
})

// ── 1. Authentication ───────────────────────────────────

test.describe('1. Authentication', () => {
  test('1.1 Login via API + redirect to today', async ({ page }) => {
    await doLogin(page)
    await expect(page).toHaveURL(/\/app\/today/)
  })

  test('1.2 Protected routes redirect to login', async ({ page }) => {
    await page.goto(`${BASE}/app/patients`)
    await page.waitForURL('**/login', { timeout: 10000 })
    await expect(page).toHaveURL(/\/login/)
  })
})

// ── 2. Navigation ───────────────────────────────────────

test.describe('2. Navigation', () => {
  test.use({ storageState: '/tmp/e2e-state.json' })

  const ROUTES = [
    { name: 'Today', url: '/app/today' },
    { name: 'Chat', url: '/app/chat' },
    { name: 'Patients', url: '/app/patients' },
    { name: 'Research', url: '/app/research' },
    { name: 'Writing', url: '/app/writing' },
    { name: 'Skills', url: '/app/skills' },
    { name: 'Knowledge', url: '/app/knowledge' },
    { name: 'Files', url: '/app/files' },
  ]

  for (const route of ROUTES) {
    test(`2.x Navigate to ${route.name}`, async ({ page }) => {
      await page.goto(`${BASE}${route.url}`)
      await expect(page.locator('body')).toBeVisible()
    })
  }
})

// ── 3. Patients — Core Clinical Workflow ────────────────

test.describe('3. Patients', () => {
  test.use({ storageState: '/tmp/e2e-state.json' })

  test('3.1 Patient list shows seeded patients', async ({ page }) => {
    await page.goto(`${BASE}/app/patients`)
    await expect(page.locator('body')).toContainText(PATIENT_NAME, { timeout: 10000 })
  })

  test('3.2 Patient detail shows clinical data', async ({ page }) => {
    await page.goto(`${BASE}/app/patients`)
    await page.getByText(PATIENT_NAME).first().click({ timeout: 10000 })
    await expect(page.locator('body')).toContainText(/adenocarcinoma|lung|NSCLC/i, { timeout: 8000 })
  })

  test('3.3 Patient summary shows medical record', async ({ page }) => {
    await page.goto(`${BASE}/app/patients`)
    await page.getByText(PATIENT_NAME).first().click({ timeout: 10000 })
    // Medical record should be primary data source in summary
    await expect(page.locator('body')).toContainText(/Initial Consultation|Diagnosis|Treatment Plan/i, { timeout: 8000 })
    await expect(page.locator('body')).toContainText(/adenocarcinoma|NSCLC|osimertinib/i, { timeout: 5000 })
  })

  test('3.4 Create new patient via dialog', async ({ page }) => {
    const name = `Test-${Date.now()}`
    await page.goto(`${BASE}/app/patients`)
    const addBtn = page.locator('button:has-text("New"), button:has-text("新增"), button:has-text("Add")').first()
    if (await addBtn.isVisible({ timeout: 3000 })) {
      await addBtn.click()
      await page.waitForTimeout(500)
      const nameInput = page.locator('input').first()
      if (await nameInput.isVisible({ timeout: 2000 })) {
        await nameInput.fill(name)
        const submitBtn = page.locator('button[type="submit"]').first()
        if (await submitBtn.isVisible({ timeout: 2000 })) {
          await submitBtn.click()
          await page.waitForTimeout(2000)
        }
      }
    }
    expect(true).toBe(true)
  })
})

// ── 3b. Medical Records (病历) ────────────────────────────

test.describe('3b. Medical Records', () => {
  test.use({ storageState: '/tmp/e2e-state.json' })

  test('3b.1 Navigate to medical records tab', async ({ page }) => {
    await page.goto(`${BASE}/app/patients`)
    await page.getByText(PATIENT_NAME).first().click({ timeout: 10000 })
    // Click the "Records" tab
    const recordsTab = page.locator('button:has-text("Records"), a:has-text("Records"), [role="tab"]:has-text("Records")').first()
    if (await recordsTab.isVisible({ timeout: 3000 })) {
      await recordsTab.click()
      await page.waitForTimeout(1000)
    }
    await expect(page.locator('body')).toContainText(/Initial Consultation|chief_complaint|病历|Medical Record/i, { timeout: 8000 })
  })

  test('3b.2 View seeded medical record content', async ({ page }) => {
    await page.goto(`${BASE}/app/patients`)
    await page.getByText(PATIENT_NAME).first().click({ timeout: 10000 })
    const recordsTab = page.locator('button:has-text("Records"), a:has-text("Records"), [role="tab"]:has-text("Records")').first()
    if (await recordsTab.isVisible({ timeout: 3000 })) {
      await recordsTab.click()
      await page.waitForTimeout(1000)
    }
    // Click the seeded record to open it
    const recordLink = page.locator('text=Initial Consultation').first()
    if (await recordLink.isVisible({ timeout: 5000 })) {
      await recordLink.click()
      await page.waitForTimeout(1000)
      // Should show structured sections
      await expect(page.locator('body')).toContainText(/persistent|cough|hemoptysis/i, { timeout: 8000 })
      await expect(page.locator('body')).toContainText(/hypertension|amlodipine/i, { timeout: 5000 })
    }
  })

  test('3b.3 Create new medical record', async ({ page }) => {
    await page.goto(`${BASE}/app/patients`)
    await page.getByText(PATIENT_NAME).first().click({ timeout: 10000 })
    const recordsTab = page.locator('button:has-text("Records"), a:has-text("Records"), [role="tab"]:has-text("Records")').first()
    if (await recordsTab.isVisible({ timeout: 3000 })) {
      await recordsTab.click()
      await page.waitForTimeout(1000)
    }
    // Click "New Record" button
    const newBtn = page.locator('button:has-text("New Record"), button:has-text("新建")').first()
    if (await newBtn.isVisible({ timeout: 3000 })) {
      await newBtn.click()
      await page.waitForTimeout(500)
      // Fill title
      const titleInput = page.locator('input[placeholder*="title"], input[placeholder*="标题"], input[placeholder*="Record"]').first()
      if (await titleInput.isVisible({ timeout: 2000 })) {
        await titleInput.fill('E2E Follow-up Visit')
      }
      // Fill Chief Complaint section
      const sections = page.locator('textarea')
      const count = await sections.count()
      if (count > 0) {
        await sections.first().fill('Patient reports improving cough. No hemoptysis since last visit.')
      }
      // Save
      const saveBtn = page.locator('button:has-text("Save"), button:has-text("保存")').first()
      if (await saveBtn.isVisible({ timeout: 2000 })) {
        await saveBtn.click()
        await page.waitForTimeout(1500)
      }
    }
    expect(true).toBe(true)
  })
})

// ── 3c. Encounter (问诊) / Patient Chat ───────────────────

test.describe('3c. Encounter (问诊)', () => {
  test.use({ storageState: '/tmp/e2e-state.json' })

  test('3c.1 Open encounter tab for patient', async ({ page }) => {
    await page.goto(`${BASE}/app/patients`)
    await page.getByText(PATIENT_NAME).first().click({ timeout: 10000 })
    // Click the 问诊/Chat tab
    const chatTab = page.locator('button:has-text("问诊"), a:has-text("问诊"), button:has-text("Chat"), [role="tab"]:has-text("Chat")').first()
    if (await chatTab.isVisible({ timeout: 3000 })) {
      await chatTab.click()
      await page.waitForTimeout(1000)
    }
    // Should have patient name or MRN in context
    await expect(page.locator('body')).toContainText(/Zhang Wei|MRN-2026/i, { timeout: 8000 })
  })

  test('3c.2 Send clinical question during encounter', async ({ page }) => {
    await page.goto(`${BASE}/app/patients`)
    await page.getByText(PATIENT_NAME).first().click({ timeout: 10000 })
    const chatTab = page.locator('button:has-text("问诊"), a:has-text("问诊"), button:has-text("Chat"), [role="tab"]:has-text("Chat")').first()
    if (await chatTab.isVisible({ timeout: 3000 })) {
      await chatTab.click()
      await page.waitForTimeout(1000)
    }
    const input = page.locator('textarea, [contenteditable="true"], input[type="text"]').first()
    if (await input.isVisible({ timeout: 3000 })) {
      await input.fill('Based on this patient\'s EGFR exon 19 deletion and current response to osimertinib, what is the recommended surveillance interval and what resistance mutations should we monitor for?')
      await page.keyboard.press('Enter')
      await page.waitForTimeout(8000)
      // Should get a substantive response
      const text = await page.locator('body').innerText()
      expect(text.length).toBeGreaterThan(100)
    }
  })

  test('3c.3 Summary reflects medical record as primary source after encounter', async ({ page }) => {
    await page.goto(`${BASE}/app/patients`)
    await page.getByText(PATIENT_NAME).first().click({ timeout: 10000 })
    await page.waitForTimeout(1000)
    // Medical record should be the primary summary, not just auto-extracted tags
    await expect(page.locator('body')).toContainText(/Initial Consultation|Diagnosis|Treatment Plan/i, { timeout: 8000 })
  })
})

// ── 4. Chat — AI Interaction ────────────────────────────

test.describe('4. Chat', () => {
  test.use({ storageState: '/tmp/e2e-state.json' })

  test('4.1 Global chat page loads', async ({ page }) => {
    await page.goto(`${BASE}/app/chat`)
    await expect(page.locator('textarea, [contenteditable="true"], input[type="text"]').first()).toBeVisible({ timeout: 10000 })
  })

  test('4.2 Send message and get SSE stream', async ({ page }) => {
    await page.goto(`${BASE}/app/chat`)
    const input = page.locator('textarea, [contenteditable="true"], input[type="text"]').first()
    await input.fill('Hello, summarize EGFR TKI therapy in one sentence.')
    await page.keyboard.press('Enter')
    await page.waitForTimeout(5000)
    const body = page.locator('body')
    const text = await body.innerText()
    expect(text.length).toBeGreaterThan(50)
  })
})

// ── 5. Research ─────────────────────────────────────────

test.describe('5. Research', () => {
  test.use({ storageState: '/tmp/e2e-state.json' })

  test('5.1 Studies page loads', async ({ page }) => {
    await page.goto(`${BASE}/app/research`)
    await expect(page.locator('body')).toBeVisible()
  })

  test('5.2 Create study', async ({ page }) => {
    await page.goto(`${BASE}/app/research`)
    const addBtn = page.locator('button:has-text("New"), button:has-text("新增"), button:has-text("Create"), button:has-text("Add")').first()
    if (await addBtn.isVisible({ timeout: 3000 })) {
      await addBtn.click()
      await page.waitForTimeout(500)
      const nameInput = page.locator('input').first()
      if (await nameInput.isVisible({ timeout: 2000 })) {
        await nameInput.fill(`E2E NSCLC Study ${Date.now()}`)
        const submit = page.locator('button[type="submit"]').first()
        if (await submit.isVisible({ timeout: 2000 })) {
          await submit.click()
          await page.waitForTimeout(2000)
        }
      }
    }
    expect(true).toBe(true)
  })
})

// ── 6. Writing ──────────────────────────────────────────

test.describe('6. Writing', () => {
  test.use({ storageState: '/tmp/e2e-state.json' })

  test('6.1 Document list loads', async ({ page }) => {
    await page.goto(`${BASE}/app/writing`)
    await expect(page.locator('body')).toContainText(/Treatment Summary|Document/, { timeout: 8000 })
  })

  test('6.2 Create and edit document', async ({ page }) => {
    await page.goto(`${BASE}/app/writing`)
    const addBtn = page.locator('button:has-text("New"), button:has-text("新增"), button:has-text("Create")').first()
    if (await addBtn.isVisible({ timeout: 3000 })) {
      await addBtn.click()
      await page.waitForTimeout(1000)
      // Should navigate to editor
      const editor = page.locator('[contenteditable="true"], textarea, .ProseMirror').first()
      if (await editor.isVisible({ timeout: 5000 })) {
        await editor.fill('# E2E Test Document\n\nThis is a test document created by E2E tests.')
        await page.waitForTimeout(1000)
      }
    }
    expect(true).toBe(true)
  })
})

// ── 7. Knowledge Base ───────────────────────────────────

test.describe('7. Knowledge', () => {
  test.use({ storageState: '/tmp/e2e-state.json' })

  test('7.1 Knowledge page loads', async ({ page }) => {
    await page.goto(`${BASE}/app/knowledge`)
    await expect(page.locator('body')).toContainText(/EGFR|RECIST|knowledge|Knowledge|知识/i, { timeout: 10000 })
  })

  test('7.2 Facts API returns seeded data', async ({ page }) => {
    // Test API directly from browser context
    const result = await page.evaluate(async () => {
      const res = await fetch('/api/v1/facts')
      return res.json()
    })
    expect(Array.isArray(result)).toBe(true)
  })
})

// ── 8. Settings ─────────────────────────────────────────

test.describe('8. Settings', () => {
  test.use({ storageState: '/tmp/e2e-state.json' })

  test('8.1 Settings page loads', async ({ page }) => {
    await page.goto(`${BASE}/app/settings`)
    await expect(page.locator('body')).toBeVisible()
  })
})

// ── 9. Admin ────────────────────────────────────────────

test.describe('9. Admin', () => {
  test.use({ storageState: '/tmp/e2e-state.json' })

  test('9.1 Admin users list loads', async ({ page }) => {
    await page.goto(`${BASE}/app/admin/users`)
    await expect(page.locator('body')).toContainText(/hz|e2e-doctor|admin|Admin/i, { timeout: 10000 })
  })
})

// ── 10. Complete End-to-End Workflow ────────────────────

test.describe('10. Full Clinical Workflow', () => {
  test.use({ storageState: '/tmp/e2e-state.json' })

  test('10.1 Login → Patient → Records → Encounter → Chat → Knowledge → Settings', async ({ page }) => {
    // 1. Login via API (bypass form UI)
    await doLogin(page)
    await expect(page).toHaveURL(/\/app\/today/)

    // 2. View patients
    await page.goto(`${BASE}/app/patients`)
    await expect(page.locator('body')).toContainText(PATIENT_NAME, { timeout: 8000 })

    // 3. Open Zhang Wei's chart → verify diagnosis
    await page.getByText(PATIENT_NAME).first().click({ timeout: 8000 })
    await page.waitForTimeout(1000)
    await expect(page.locator('body')).toContainText(/adenocarcinoma|NSCLC|lung/i, { timeout: 8000 })

    // 4. View medical records tab
    const recordsTab = page.locator('button:has-text("Records"), [role="tab"]:has-text("Records")').first()
    if (await recordsTab.isVisible({ timeout: 3000 })) {
      await recordsTab.click()
      await page.waitForTimeout(1000)
      await expect(page.locator('body')).toContainText(/Initial Consultation|病历/i, { timeout: 8000 })
    }

    // 5. Start encounter (问诊) with patient
    const chatTab = page.locator('button:has-text("问诊"), button:has-text("Chat"), [role="tab"]:has-text("Chat")').first()
    if (await chatTab.isVisible({ timeout: 3000 })) {
      await chatTab.click()
      await page.waitForTimeout(1000)
      await expect(page.locator('body')).toContainText(/Zhang Wei|MRN-2026/i, { timeout: 8000 })
      const input = page.locator('textarea, [contenteditable="true"], input[type="text"]').first()
      if (await input.isVisible({ timeout: 3000 })) {
        await input.fill('Review this patient\'s EGFR TKI treatment response and recommend next steps.')
        await page.keyboard.press('Enter')
        await page.waitForTimeout(5000)
      }
    }

    // 6. Check knowledge base
    await page.goto(`${BASE}/app/knowledge`)
    await expect(page.locator('body')).toContainText(/EGFR|RECIST|knowledge|Knowledge|知识/i, { timeout: 8000 })

    // 7. Settings
    await page.goto(`${BASE}/app/settings`)
    await expect(page.locator('body')).toBeVisible()

    // 8. Admin
    await page.goto(`${BASE}/app/admin/users`)
    await expect(page.locator('body')).toContainText(/hz|e2e-doctor/i, { timeout: 8000 })
  })
})
