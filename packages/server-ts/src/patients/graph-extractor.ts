/**
 * P5 — Dual-Track Graph Extractor
 *
 * Track 1 (rule-based): regex patterns for ~80% of clinical text. Free, fast.
 * Track 2 (LLM): complex sentences with confidence < 0.7. 15% of text.
 * Track 3 (human): edge cases with low confidence. 5% of text.
 */

export interface ClinicalEntity {
  type: 'diagnosis' | 'lab_result' | 'imaging' | 'medication' | 'finding'
  content: string
  confidence: number
}

export interface ClinicalRelation {
  from: string
  to: string
  relation: 'hasFinding' | 'measuredIn' | 'comparedTo' | 'takesMedication' | 'relatedTo'
  confidence: number
}

/** Track 1: rule-based entity extraction */
export function extractEntities(text: string): ClinicalEntity[] {
  if (!text) return []
  const entities: ClinicalEntity[] = []

  // Diagnosis patterns
  const diagPatterns = [
    /(?:diagnosed with|诊断[：:]?\s*|确诊[：:]?\s*)([A-Za-z\u4e00-\u9fa5\s\-/]+?)(?:[,.;，。；]|$)/gi,
    /(NSCLC|SCLC|adenocarcinoma|squamous|melanoma|lymphoma|leukemia)\s*(?:stage|期)?\s*([IVXA-D0-9]+)?/gi,
  ]
  for (const p of diagPatterns) {
    for (const m of text.matchAll(p)) {
      entities.push({ type: 'diagnosis', content: m[0].trim(), confidence: 0.85 })
    }
  }

  // Lab values
  const labPattern = /([A-Za-z\u4e00-\u9fa5]+)\s*[：:]\s*(\d+\.?\d*)\s*([a-zA-Z/%]+)?/g
  for (const m of text.matchAll(labPattern)) {
    entities.push({ type: 'lab_result', content: `${m[1]} ${m[2]}${m[3] || ''}`, confidence: 0.9 })
  }

  // Imaging
  if (/(CT|MRI|PET|X-ray|超声|CT扫描|核磁|PET-CT)/i.test(text)) {
    const nodMatch = text.match(/(\d+\.?\d*\s*(?:mm|cm|毫米|厘米)?\s*(?:nodule|结节|mass|肿块|lesion|病灶))/i)
    if (nodMatch) entities.push({ type: 'imaging', content: nodMatch[0], confidence: 0.85 })

    const effMatch = text.match(/(no|without|未见|无)\s*(pleural|胸膜|心包)?\s*(effusion|积液|渗出)/i)
    if (effMatch) entities.push({ type: 'imaging', content: effMatch[0], confidence: 0.8 })
  }

  // EGFR/mutation
  for (const m of text.matchAll(/(EGFR|ALK|ROS1|BRAF|KRAS|HER2|MSI|PD-L1)\s*[：:]?\s*(exon\s*\d+)?\s*(\w+)?\s*(mutation|deletion|fusion|阳性|突变|野生型)?/gi)) {
    entities.push({ type: 'finding', content: m[0].trim(), confidence: 0.88 })
  }

  // Medications
  const medPattern = /([A-Za-z]+(?:mab|nib|mide|cin|zole|pril|sartan|statin|sone|olone|mycin|cycline))\s*(\d+\.?\d*\s*(?:mg|g|mcg|μg))?/gi
  for (const m of (text.matchAll(medPattern) || [])) {
    if (m[0].length > 4) {
      entities.push({ type: 'medication', content: m[0].trim(), confidence: 0.8 })
    }
  }

  // Dedup by content
  const seen = new Set<string>()
  return entities.filter(e => {
    const key = `${e.type}:${e.content.slice(0, 40)}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

/** Track 1: simple relations from entity co-occurrence */
export function extractRelations(
  entities: ClinicalEntity[],
  _context: string,
): ClinicalRelation[] {
  if (entities.length < 2) return []
  const relations: ClinicalRelation[] = []

  // Finding → measuredIn → Imaging
  const findings = entities.filter(e => e.type === 'finding')
  const imaging = entities.filter(e => e.type === 'imaging')
  for (const f of findings) {
    for (const i of imaging) {
      relations.push({
        from: f.content,
        to: i.content,
        relation: 'measuredIn',
        confidence: Math.min(f.confidence, i.confidence) * 0.75,
      })
    }
  }

  // Diagnosis → hasFinding → Finding
  const diagnoses = entities.filter(e => e.type === 'diagnosis')
  for (const d of diagnoses) {
    for (const f of findings) {
      relations.push({
        from: d.content,
        to: f.content,
        relation: 'hasFinding',
        confidence: Math.min(d.confidence, f.confidence) * 0.7,
      })
    }
  }

  // Patient → takesMedication → Medication
  const meds = entities.filter(e => e.type === 'medication')
  for (const m of meds) {
    relations.push({
      from: 'patient',
      to: m.content,
      relation: 'takesMedication',
      confidence: m.confidence * 0.7,
    })
  }

  return relations
}
