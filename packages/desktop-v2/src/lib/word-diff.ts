/**
 * word-diff — dependency-free word-level diff for the Writing Studio
 * polish flow (P1).
 *
 * Tokenization is CJK-aware: each CJK ideograph is its own token, while
 * latin/digit runs (incl. '.' and '%' so "13.8%" stays one token) and
 * whitespace runs stay whole. Diff is a plain O(n·m) LCS — polish
 * selections are short paragraphs, so quadratic is fine; a MAX_TOKENS
 * guard degrades to a single whole-selection hunk for pathological
 * inputs instead of freezing the UI.
 */

export type DiffSegment =
  | { kind: 'same'; text: string }
  | { kind: 'change'; del: string; add: string };

// Order matters: latin/number runs first, then whitespace runs, then
// single CJK ideographs, then any other single char.
const TOKEN_RE = /[a-zA-Z0-9.%]+|\s+|[㐀-䶿一-鿿豈-﫿]|[\s\S]/g;

export function tokenize(s: string): string[] {
  return s.match(TOKEN_RE) ?? [];
}

/** Above this token count the O(n·m) table gets expensive — fall back
 *  to one whole-text change hunk (still correct, just coarse). */
const MAX_TOKENS = 4000;

/**
 * Word-level diff of ``a`` → ``b``. Returns an ordered list of
 * segments: unchanged runs plus change hunks (paired del/add).
 * Whitespace-only unchanged runs sandwiched between two change hunks
 * are folded into the hunk so the UI doesn't render confetti.
 */
export function diffWords(a: string, b: string): DiffSegment[] {
  if (a === b) return a ? [{ kind: 'same', text: a }] : [];
  const ta = tokenize(a);
  const tb = tokenize(b);
  const n = ta.length;
  const m = tb.length;
  if (n > MAX_TOKENS || m > MAX_TOKENS) {
    return [{ kind: 'change', del: a, add: b }];
  }

  // LCS length table — dp[i][j] = LCS of ta[i..] vs tb[j..].
  const dp: Int32Array[] = [];
  for (let i = 0; i <= n; i++) dp.push(new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    const row = dp[i];
    const next = dp[i + 1];
    for (let j = m - 1; j >= 0; j--) {
      row[j] = ta[i] === tb[j]
        ? next[j + 1] + 1
        : Math.max(next[j], row[j + 1]);
    }
  }

  // Backtrack into a flat op list.
  type Op = { t: 'same' | 'del' | 'add'; text: string };
  const ops: Op[] = [];
  const push = (t: Op['t'], text: string) => {
    const last = ops[ops.length - 1];
    if (last && last.t === t) last.text += text;
    else ops.push({ t, text });
  };
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (ta[i] === tb[j]) {
      push('same', ta[i]);
      i++; j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      push('del', ta[i]);
      i++;
    } else {
      push('add', tb[j]);
      j++;
    }
  }
  while (i < n) { push('del', ta[i]); i++; }
  while (j < m) { push('add', tb[j]); j++; }

  // Group ops into segments, folding whitespace-only 'same' runs that
  // sit BETWEEN two change ops into the surrounding hunk.
  const segments: DiffSegment[] = [];
  let pendDel = '';
  let pendAdd = '';
  const flushChange = () => {
    if (pendDel || pendAdd) {
      segments.push({ kind: 'change', del: pendDel, add: pendAdd });
      pendDel = '';
      pendAdd = '';
    }
  };
  for (let k = 0; k < ops.length; k++) {
    const op = ops[k];
    if (op.t === 'same') {
      const isWs = /^\s+$/.test(op.text);
      const next = ops[k + 1];
      const changeOpen = pendDel !== '' || pendAdd !== '';
      if (isWs && changeOpen && next && next.t !== 'same') {
        // Fold into the open hunk on both sides.
        pendDel += op.text;
        pendAdd += op.text;
      } else {
        flushChange();
        const last = segments[segments.length - 1];
        if (last && last.kind === 'same') last.text += op.text;
        else segments.push({ kind: 'same', text: op.text });
      }
    } else if (op.t === 'del') {
      pendDel += op.text;
    } else {
      pendAdd += op.text;
    }
  }
  flushChange();
  return segments;
}

/**
 * Merge a diff back into text given per-change-hunk accept flags
 * (indexed in change-hunk order): accepted → the new text, rejected →
 * the original text.
 */
export function applyDiff(segments: DiffSegment[], accepted: boolean[]): string {
  let out = '';
  let c = 0;
  for (const seg of segments) {
    if (seg.kind === 'same') out += seg.text;
    else {
      out += accepted[c] !== false ? seg.add : seg.del;
      c++;
    }
  }
  return out;
}

/** Count of change hunks in a segment list (drives the accept[] size). */
export function changeCount(segments: DiffSegment[]): number {
  return segments.reduce((acc, s) => acc + (s.kind === 'change' ? 1 : 0), 0);
}
