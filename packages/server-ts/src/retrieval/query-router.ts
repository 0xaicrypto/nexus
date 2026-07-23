/**
 * P3 — Query Router
 *
 * Two-layer classifier (rule layer for now; LLM layer added in P4).
 *   1. Rules (keyword + pattern, <1ms): ~70% of queries
 *   2. Intent type: sql | vector | file | mixed
 *
 * routeQuery() maps an intent to ordered retrieval routes.
 */

export type QueryIntent = 'sql' | 'vector' | 'file' | 'mixed'
export type RouteKind = 'sql' | 'vector' | 'file'

/**
 * Rule-based classifier. Handles ~70% of queries without LLM call.
 */
export function classifyQuery(query: string): QueryIntent {
  const q = query.toLowerCase().trim()
  if (!q) return 'mixed'

  // File references
  if (q.startsWith('#文件') || q.startsWith('#file') ||
      q.includes('上传') && (q.includes('文件') || q.includes('CT') || q.includes('报告')) ||
      q.includes('uploaded') && q.includes('file')) {
    return 'file'
  }

  // SQL — patient demographic queries
  const sqlPatterns = [
    /(患者|patient).*(年龄|性别|名字|姓名|主诉|多大|叫什么)/,
    /(年龄|性别|名字|姓名|主诉|多大|叫什么).*(是|为)/,
    /what is.*(age|sex|name|gender)/i,
    /(patient|患者).*(list|列表|有几个|count|how many)/i,
    /(我|my).*(patient|患者).*(list|count|number)/i,
    /list.*(patient|患者)/i,
    /(which|what).*(patient|患者)/i,
  ]
  if (sqlPatterns.some(p => p.test(q))) return 'sql'

  // Semantic/clinical — these need vector search of Knowledge/Facts
  const vectorPatterns = [
    /(进展|治疗|管理|指南|综述|研究|最新|literature|review|management|treatment|guideline|immunotherapy|targeted|cancer|carcinoma|tumor)/,
    /what (is|are) the (latest|new|current|recommended)/,
    /how (to treat|to manage|to diagnose|does.*work)/,
  ]
  if (vectorPatterns.some(p => p.test(q))) return 'vector'

  // Default — mixed: try SQL first, then vector
  return 'mixed'
}

/**
 * Maps a query intent to ordered retrieval routes.
 * For 'mixed', returns all applicable routes.
 */
export function routeQuery(_query: string, intent: QueryIntent): RouteKind[] {
  switch (intent) {
    case 'sql':    return ['sql']
    case 'vector': return ['vector']
    case 'file':   return ['file']
    case 'mixed':  return ['sql', 'vector']
    default:       return ['sql', 'vector']
  }
}

/**
 * Full router: classify + route. Used by chat.router.ts.
 */
export function router(query: string): { intent: QueryIntent; routes: RouteKind[] } {
  const intent = classifyQuery(query)
  return { intent, routes: routeQuery(query, intent) }
}
