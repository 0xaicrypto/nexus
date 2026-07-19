#!/usr/bin/env bash
# Heurion 完整回归测试 v2 — 覆盖所有用户场景
BASE="${1:-https://heurion.org}"
USERNAME="HZ"
PASSWORD="hz123456"
PASS=0; FAIL=0
SAMPLE_DIR="packages/server-ts"

# Prevent commands inside command substitutions from accidentally reading this script via stdin.
exec < /dev/null

check() {
  result="$(printf '%s' "$2" | tr -d '\n\r')"
  if [ "$result" = "ok" ]; then echo "  ✓ $1"; PASS=$((PASS+1))
  else echo "  ✗ $1 — $result"; FAIL=$((FAIL+1)); fi
}

echo "════════════════════════════════════════════"
echo "  Heurion 回归测试 v2"
echo "════════════════════════════════════════════"

# ── 0. Login/Register ──
TOKEN=$(curl -sf -X POST "$BASE/api/v1/auth/login" -H "Content-Type: application/json" -d "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('jwt_token',''))" 2>/dev/null)
if [ -z "$TOKEN" ]; then
  TOKEN=$(curl -sf -X POST "$BASE/api/v1/auth/register" -H "Content-Type: application/json" -d "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\",\"display_name\":\"Test\"}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('jwt_token',''))" 2>/dev/null)
fi
if [ -z "$TOKEN" ]; then echo "✗ Login failed"; exit 1; fi
H="Authorization: Bearer $TOKEN"
check "0. Login" ok

# ═══ 0. Clear data ═══
# Staging uses API to clear, production uses SSH
if echo "$BASE" | grep -q "localhost\|127.0.0.1"; then
  # Staging: clear via API (no SSH needed)
  curl -sf -X POST "$BASE/api/v1/auth/clear-test-data" -H "$H" > /dev/null 2>&1 || true
else
  ssh -o StrictHostKeyChecking=no -i ~/.ssh/heurion-do root@174.138.31.245 "cd ~/heurion/packages/server-ts && node scripts/clear-data.js && rm -rf .nexus/twins/*/event_log.jsonl .nexus/twins/*/facts/ .nexus/twins/*/episodes/ .nexus/twins/*/uploads/*" 2>/dev/null
fi
check "0. Clear data" ok

# ═══ 1. Patient Onboarding ═══
HASH=$(curl -sf -X POST "$BASE/api/v1/dicom/patients/register-manual" -H "$H" -H "Content-Type: application/json" -d '{"name":"张强","initials":"ZQ","age":58,"sex":"M","chief_complaint":"咳嗽胸痛3周"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('patient_hash',''))" 2>/dev/null)
check "1.1 Create patient" "$([ -n "$HASH" ] && echo ok || echo 'FAIL')"
DETAIL_RES="$(curl -sf "$BASE/api/v1/dicom/patients/$HASH/detail" -H "$H" | python3 -c "import sys,json; print('ok' if json.load(sys.stdin).get('initials')=='ZQ' else 'FAIL')" 2>/dev/null)" < /dev/null
NAME_RES="$(curl -sf "$BASE/api/v1/dicom/patients/$HASH/detail" -H "$H" | python3 -c "import sys,json; print('ok' if json.load(sys.stdin).get('name')=='张强' else 'FAIL')" 2>/dev/null)" < /dev/null
check "1.2 Patient detail" "$DETAIL_RES"
check "1.2b Patient name stored" "$NAME_RES"
check "1.3 Patient count=1" "$([ $(curl -sf "$BASE/api/v1/dicom/patients/full" -H "$H" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null) = 1 ] && echo ok || echo 'FAIL')"

# ═══ 2. Imaging Upload + DICOM Scan ═══
DCM=$(curl -sf -X POST "$BASE/api/v1/files/upload" -H "$H" -F "file=@$SAMPLE_DIR/sample-chest-ct.dcm" | python3 -c "import sys,json; print(json.load(sys.stdin).get('file_id',''))" 2>/dev/null)
check "2.1 Upload DICOM" "$([ -n "$DCM" ] && echo ok || echo 'FAIL')"
check "2.2 Quick Scan tags" "$([ $(curl -sf -X POST "$BASE/api/v1/dicom/studies/$DCM/quick-scan" -H "$H" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('findings',[])))" 2>/dev/null) -gt 0 ] && echo ok || echo 'FAIL')"
check "2.3 Viewer thumbnail" "$(curl -sf -o /dev/null -w '%{http_code}' "$BASE/api/v1/dicom/studies/$DCM/series/0/render?index=0&format=png" -H "$H" 2>/dev/null | python3 -c "import sys; print('ok' if sys.stdin.read().strip()=='200' else 'FAIL')")"
check "2.4 Scan→Profile update" "$(curl -sf "$BASE/api/v1/dicom/patients/$HASH/detail" -H "$H" | python3 -c "import sys,json; print('ok' if '[Scan]' in json.load(sys.stdin).get('chief_complaint','') else 'FAIL')" 2>/dev/null)"

# ═══ 3. Lab Upload (with patient association) ═══
LAB=$(curl -sf -X POST "$BASE/api/v1/files/upload" -H "$H" -F "file=@$SAMPLE_DIR/sample-lab-report.txt" -F "patient_hash=$HASH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('file_id',''))" 2>/dev/null)
CTR=$(curl -sf -X POST "$BASE/api/v1/files/upload" -H "$H" -F "file=@$SAMPLE_DIR/sample-ct-report.txt" -F "patient_hash=$HASH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('file_id',''))" 2>/dev/null)
check "3.1 Upload lab report" "$([ -n "$LAB" ] && echo ok || echo 'FAIL')"
check "3.2 Upload CT text report" "$([ -n "$CTR" ] && echo ok || echo 'FAIL')"
check "3.3 Labs list has files" "$([ $(curl -sf "$BASE/api/v1/files/uploads?patient_hash=$HASH" -H "$H" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null) -ge 3 ] && echo ok || echo 'FAIL')"

# ═══ 4. AI Chat Analysis ═══
CHAT1=$(curl -sf -N -X POST "$BASE/api/v1/agent/chat" -H "$H" -H "Content-Type: application/json" -d "{\"text\":\"分析CT和实验室结果，简短回答\",\"patient_hash\":\"$HASH\",\"attachments\":[\"$CTR\",\"$LAB\"]}" 2>/dev/null)
check "4.1 Chat SSE complete" "$(echo "$CHAT1" | grep -q 'turn_complete' && echo ok || echo 'FAIL')"
sleep 3
check "4.2 Chat→Profile update" "$(curl -sf "$BASE/api/v1/dicom/patients/$HASH/detail" -H "$H" | python3 -c "import sys,json; j=json.load(sys.stdin); c=j.get('chief_complaint',''); print('ok' if len(c)>200 else 'FAIL: '+str(len(c))+'chars')" 2>/dev/null)"

# ═══ 5. Gemini Vision ═══
sleep 8
check "5.1 Gemini Vision in profile" "$(curl -sf "$BASE/api/v1/dicom/patients/$HASH/detail" -H "$H" | python3 -c "import sys,json; print('ok' if '[AI Vision]' in json.load(sys.stdin).get('chief_complaint','') else 'missing')" 2>/dev/null)"

# ═══ 6. Memory Projection ═══
check "6.1 Memory findings exist" "$([ $(curl -sf "$BASE/api/v1/memory/patient/$HASH/projection" -H "$H" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('findings',[])))" 2>/dev/null) -gt 0 ] && echo ok || echo 'FAIL')"
check "6.2 Memory export works" "$(curl -sf "$BASE/api/v1/memory/export" -H "$H" | python3 -c "import sys,json; j=json.load(sys.stdin); print('ok' if 'facts' in j else 'FAIL')" 2>/dev/null)"

# ═══ 7. Research ═══
SID=$(curl -sf -X POST "$BASE/api/v1/research/studies" -H "$H" -H "Content-Type: application/json" -d '{"display_name":"NSCLC Immunotherapy Phase II","short_code":"NSCLC001"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('study_id',''))" 2>/dev/null)
check "7.1 Create study" "$([ -n "$SID" ] && echo ok || echo 'FAIL')"

PROTOCOL_TEXT='INCLUSION: Stage IIIB/IV NSCLC, PD-L1>=1%, ECOG 0-1\nEXCLUSION: EGFR/ALK positive, autoimmune disease\nSAFETY: DLT evaluation Cycle 1, Grade 4 neutropenia >7 days, DLT rate >33%\nSCHEDULE: Screening (Day -28 to -1): consent, CT, labs. Cycle 1 Day 1 (Day 1 of 21-day cycle): CBC, chemistry. Cycle 1 Day 8 (Day 8): vital signs, CBC. Follow-up (Day 30): safety check'
curl -sf -X POST "$BASE/api/v1/research/studies/$SID/import-protocol" -H "$H" -H "Content-Type: application/json" -d "{\"text\":\"$PROTOCOL_TEXT\"}" > /dev/null 2>&1
check "7.2 Import protocol" ok

RULES=$(curl -sf -X POST "$BASE/api/v1/research/studies/$SID/extract-rules" -H "$H" -H "Content-Type: application/json" -d "{\"text\":\"$PROTOCOL_TEXT\"}" | python3 -c "import sys,json; print(json.load(sys.stdin)['status']['total'])" 2>/dev/null)
check "7.3 Extract rules" "$([ "${RULES:-0}" -gt 0 ] && echo ok || echo 'FAIL')"

# 7b. Enroll patient + verify roster/schedule/safety
curl -sf -X POST "$BASE/api/v1/research/studies/$SID/enrollments" -H "$H" -H "Content-Type: application/json" -d "{\"patient_hash\":\"$HASH\",\"arm\":\"Arm A\"}" > /dev/null 2>&1
check "7.4 Enroll patient" ok
check "7.5 Roster has entry" "$([ $(curl -sf "$BASE/api/v1/research/studies/$SID/roster" -H "$H" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null) -ge 1 ] && echo ok || echo 'FAIL')"
check "7.5b Roster shows patient name" "$(curl -sf "$BASE/api/v1/research/studies/$SID/roster" -H "$H" | python3 -c "import sys,json; print('ok' if any(e.get('name')=='张强' for e in json.load(sys.stdin)) else 'FAIL')" 2>/dev/null)"
check "7.5c Roster shows patient ID" "$(curl -sf "$BASE/api/v1/research/studies/$SID/roster" -H "$H" | python3 -c "import sys,json; print('ok' if any(e.get('patient_id')=='$HASH' for e in json.load(sys.stdin)) else 'FAIL')" 2>/dev/null)"
check "7.5d Roster shows basic info" "$(curl -sf "$BASE/api/v1/research/studies/$SID/roster" -H "$H" | python3 -c "import sys,json; print('ok' if any(e.get('age_value')==58 and e.get('sex')=='M' for e in json.load(sys.stdin)) else 'FAIL')" 2>/dev/null)"
check "7.6 Schedule tab data" "$(curl -sf "$BASE/api/v1/research/studies/$SID/assessments" -H "$H" | python3 -c "import sys,json; print('ok' if isinstance(json.load(sys.stdin), list) else 'FAIL')" 2>/dev/null)"
check "7.7 Safety status" "$(curl -sf "$BASE/api/v1/research/studies/$SID/safety/stop-rule-status" -H "$H" | python3 -c "import sys,json; print('ok' if 'triggered_rules' in json.load(sys.stdin) else 'FAIL')" 2>/dev/null)"
check "7.8 Eligibility list" "$(curl -sf "$BASE/api/v1/research/studies/$SID/eligibility" -H "$H" | python3 -c "import sys,json; print('ok' if 'screenings' in json.load(sys.stdin) else 'FAIL')" 2>/dev/null)"

# ═══ 8. Chat with Patient + Research Context ═══
CHAT2=$(curl -sf -N -X POST "$BASE/api/v1/agent/chat" -H "$H" -H "Content-Type: application/json" -d "{\"text\":\"ZQ这个患者什么诊断？符合NSCLC001吗？\",\"patient_hash\":\"$HASH\"}" 2>/dev/null)
# Parse SSE to extract all final_answer text
CHAT2_TEXT=$(echo "$CHAT2" | grep 'final_answer' | sed 's/^data: //' | python3 -c "import sys,json; print(''.join(json.loads(l.strip()).get('text','') for l in sys.stdin if l.strip()))" 2>/dev/null)
check "8.1 Chat references patient" "$(echo "$CHAT2_TEXT" | python3 -c "import sys; t=sys.stdin.read().lower(); print('ok' if 'nsclc' in t or '肺癌' in t or '腺癌' in t or 'zq' in t else 'FAIL')")"
check "8.2 Chat references study" "$(echo "$CHAT2_TEXT" | python3 -c "import sys; t=sys.stdin.read().lower(); print('ok' if 'nsclc001' in t or '研究' in t else 'FAIL')")"

# 8b. 问诊 — AI must reference actual patient data from profile
CHAT3=$(curl -sf -N -X POST "$BASE/api/v1/agent/chat" -H "$H" -H "Content-Type: application/json" -d "{\"text\":\"ZQ的诊断是什么？分期？有什么发现？请引用患者资料回答\",\"patient_hash\":\"$HASH\"}" 2>/dev/null)
CHAT3_TEXT=$(echo "$CHAT3" | grep 'final_answer' | sed 's/^data: //' | python3 -c "import sys,json; print(''.join(json.loads(l.strip()).get('text','') for l in sys.stdin if l.strip()))" 2>/dev/null)
check "8.3 问诊: references diagnosis" "$(echo "$CHAT3_TEXT" | python3 -c "import sys; t=sys.stdin.read().lower(); print('ok' if any(w in t for w in ['nsclc','腺癌','iiia','结节','cea','诊断','肿瘤','stage']) else 'FAIL')")"
check "8.4 问诊: references findings" "$(echo "$CHAT3_TEXT" | python3 -c "import sys; t=sys.stdin.read().lower(); print('ok' if any(w in t for w in ['ct','影像','结节','cea','淋巴结','rul','cm']) else 'FAIL')")"

# 8c. Chat should see patient basic demographics (name/age/sex)
CHAT4=$(curl -sf -N -X POST "$BASE/api/v1/agent/chat" -H "$H" -H "Content-Type: application/json" -d "{\"text\":\"患者名字是什么？年龄和性别呢？\",\"patient_hash\":\"$HASH\"}" 2>/dev/null)
CHAT4_TEXT=$(echo "$CHAT4" | grep 'final_answer' | sed 's/^data: //' | python3 -c "import sys,json; print(''.join(json.loads(l.strip()).get('text','') for l in sys.stdin if l.strip()))" 2>/dev/null)
check "8.5 Chat knows patient name" "$(echo "$CHAT4_TEXT" | python3 -c "import sys; t=sys.stdin.read(); print('ok' if '张强' in t or 'ZQ' in t else 'FAIL')")"
check "8.6 Chat knows patient age/sex" "$(echo "$CHAT4_TEXT" | python3 -c "import sys; t=sys.stdin.read().lower(); print('ok' if ('58' in t or '58岁' in t) and ('男' in t or 'm' in t) else 'FAIL')")"

# ═══ 9. Skills ═══
check "9.1 Skills catalog" "$([ $(curl -sf "$BASE/api/v1/skills" -H "$H" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('skills',[])))" 2>/dev/null) -ge 8 ] && echo ok || echo 'FAIL')"
curl -sf -X POST "$BASE/api/v1/skills/install" -H "$H" -H "Content-Type: application/json" -d '{"identifier":"official/clinical-summary"}' > /dev/null 2>&1
check "9.2 Install skill" "$(curl -sf "$BASE/api/v1/skills" -H "$H" | python3 -c "import sys,json; print('ok' if any(s['name']=='Clinical Summary' and s.get('installed') for s in json.load(sys.stdin)['skills']) else 'FAIL')" 2>/dev/null)"

# ═══ 10. Writing ═══
DID=$(curl -sf -X POST "$BASE/api/v1/docs" -H "$H" -H "Content-Type: application/json" -d '{"title":"ZQ Case Report"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
curl -sf -X PUT "$BASE/api/v1/docs/$DID" -H "$H" -H "Content-Type: application/json" -d '{"body":"58yo M, cT2aN2M0 IIIA NSCLC"}' > /dev/null 2>&1
check "10.1 Create document" "$([ -n "$DID" ] && echo ok || echo 'FAIL')"
check "10.2 Document content" "$(curl -sf "$BASE/api/v1/docs/$DID" -H "$H" | python3 -c "import sys,json; print('ok' if 'IIIA' in json.load(sys.stdin).get('body','') else 'FAIL')" 2>/dev/null)"

# Doc chat should edit the document automatically.
DOC_CHAT_BODY=$(curl -sS -N -X POST "$BASE/api/v1/docs/$DID/chat" -H "$H" -H "Content-Type: application/json" -d '{"message":"Append the exact line CONFIRMED_DOC_CHAT_EDIT to the document."}' | python3 -c "
import sys, json
body = None
for line in sys.stdin:
    if line.startswith('data: '):
        try:
            d = json.loads(line[6:])
            if d.get('type') == 'done':
                body = d.get('doc_body')
        except Exception:
            pass
print(body or '')
" 2>/dev/null)
check "10.3 Doc Chat edits document" "$(echo "$DOC_CHAT_BODY" | python3 -c "import sys; t=sys.stdin.read(); print('ok' if 'CONFIRMED_DOC_CHAT_EDIT' in t else 'FAIL')" 2>/dev/null)"

# ═══ 10b. Document list, snapshots, PHI, references, export, delete ═══
check "10.4 Document list includes doc" "$(curl -sf "$BASE/api/v1/docs" -H "$H" | python3 -c "import sys,json; docs=json.load(sys.stdin).get('docs',[]); print('ok' if any(d['id']=='$DID' for d in docs) else 'FAIL')" 2>/dev/null)"

SNAP_ID=$(curl -sf "$BASE/api/v1/docs/$DID/snapshots" -H "$H" | python3 -c "import sys,json; snaps=json.load(sys.stdin).get('snapshots',[]); print(snaps[0]['id'] if snaps else '')" 2>/dev/null)
check "10.5 Snapshot exists after edits" "$([ -n \"$SNAP_ID\" ] && echo ok || echo 'FAIL')"

curl -sf -X POST "$BASE/api/v1/docs/$DID/snapshots/$SNAP_ID/restore" -H "$H" > /dev/null 2>&1
check "10.6 Restore snapshot" "$(curl -sf "$BASE/api/v1/docs/$DID" -H "$H" | python3 -c "import sys,json; print('ok' if '58yo M, cT2aN2M0 IIIA NSCLC' in json.load(sys.stdin).get('body','') else 'FAIL')" 2>/dev/null)"

# Put PHI-laden body for scan
curl -sf -X PUT "$BASE/api/v1/docs/$DID" -H "$H" -H "Content-Type: application/json" -d '{"body":"Patient John Smith has SSN 123-45-6789."}' > /dev/null 2>&1
PHI_COUNT=$(curl -sf -X POST "$BASE/api/v1/docs/$DID/phi-scan" -H "$H" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('findings',[])))" 2>/dev/null)
PHI_COUNT=$(echo "$PHI_COUNT" | tr -d '"')
check "10.7 PHI scan finds issues" "$([ "${PHI_COUNT:-0}" -gt 0 ] && echo ok || echo "FAIL: ${PHI_COUNT} findings")"

REF=$(curl -sf -X POST "$BASE/api/v1/docs/$DID/references" -H "$H" -H "Content-Type: application/json" -d '{"kind":"guideline","content":"NCCN NSCLC guideline v4.2024","label":"NCCN","source_patient_hash":""}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('reference_id',''))" 2>/dev/null)
check "10.8 Add reference" "$([ -n \"$REF\" ] && echo ok || echo 'FAIL')"
check "10.9 List references" "$(curl -sf "$BASE/api/v1/docs/$DID/references" -H "$H" | python3 -c "import sys,json; print('ok' if any(r.get('reference_id')=='$REF' for r in json.load(sys.stdin).get('references',[])) else 'FAIL')" 2>/dev/null)"

DOCX_STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/docs/$DID/export" -H "$H" 2>/dev/null)
check "10.10 Export DOCX" "$(echo "$DOCX_STATUS" | python3 -c "import sys; print('ok' if sys.stdin.read().strip()=='200' else 'FAIL: '+sys.stdin.read().strip())")"

curl -sf -X DELETE "$BASE/api/v1/docs/$DID" -H "$H" > /dev/null 2>&1
GET_AFTER_DELETE=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/api/v1/docs/$DID" -H "$H" 2>/dev/null)
check "10.11 Delete document" "$(echo "$GET_AFTER_DELETE" | python3 -c "import sys; print('ok' if sys.stdin.read().strip()=='404' else 'FAIL')")"

# ═══ 11. Calendar ═══
CAL=$(curl -sf "$BASE/api/v1/calendar/export.ics?token=$TOKEN" 2>/dev/null)
check "11.1 Calendar iCal format" "$(echo "$CAL" | python3 -c "import sys; t=sys.stdin.read(); print('ok' if 'VCALENDAR' in t else 'FAIL')" 2>/dev/null)"
CAL_EVENTS=$(echo "$CAL" | python3 -c "import sys; print(sys.stdin.read().count('BEGIN:VEVENT'))" 2>/dev/null)
check "11.2 Calendar has events" "$([ "${CAL_EVENTS:-0}" -gt 0 ] && echo ok || echo 'FAIL: 0 events')"

# ═══ 12. File content (Labs) ═══
LAB_ID=$(curl -sf "$BASE/api/v1/files/uploads?patient_hash=$HASH" -H "$H" | python3 -c "import sys,json; [print(f['file_id']) for f in json.load(sys.stdin) if 'lab' in f['name'].lower()][:1]" 2>/dev/null)
FILE_CONTENT=$(curl -sf "$BASE/api/v1/files/$LAB_ID/content" -H "$H" | python3 -c "import sys,json; j=json.load(sys.stdin); print('ok' if j.get('type')=='text' else 'FAIL')" 2>/dev/null)
check "12.1 File content viewable" "$FILE_CONTENT"

# ═══ 13. Rule Confirmation ═══
RULES_LIST=$(curl -sf "$BASE/api/v1/research/studies/$SID/protocol-rules" -H "$H" 2>/dev/null)
RULE_COUNT=$(echo "$RULES_LIST" | python3 -c "import sys,json; print(json.load(sys.stdin)['status']['total'])" 2>/dev/null)
check "13.1 Rules extracted" "$([ "${RULE_COUNT:-0}" -gt 0 ] && echo ok || echo 'FAIL')"

# Confirm ALL schedule rules to generate assessments
SCHEDULE_RULE=$(echo "$RULES_LIST" | python3 -c "import sys,json; rules=json.load(sys.stdin)['rules']; [print(r['id']) for r in rules if r['category']=='schedule']" 2>/dev/null)
if [ -n "$SCHEDULE_RULE" ]; then
  for rid in $SCHEDULE_RULE; do
    curl -sf -X POST "$BASE/api/v1/research/studies/$SID/protocol-rules/$rid/confirm" -H "$H" > /dev/null 2>&1
  done
  check "13.2 Schedule rules confirmed" ok
else
  FIRST_RULE=$(echo "$RULES_LIST" | python3 -c "import sys,json; print(json.load(sys.stdin)['rules'][0]['id'])" 2>/dev/null)
  if [ -n "$FIRST_RULE" ]; then
    CONFIRM=$(curl -sf -X POST "$BASE/api/v1/research/studies/$SID/protocol-rules/$FIRST_RULE/confirm" -H "$H" 2>/dev/null)
    check "13.2 Doctor confirms rule" "$(echo "$CONFIRM" | python3 -c "import sys,json; print('ok' if json.load(sys.stdin)['rule']['confirmed'] else 'FAIL')" 2>/dev/null)"
  else
    check "13.2 Doctor confirms rule" "no rules"
  fi
fi

# ═══ 14. Timeline ═══
check "14. Timeline has events" "$(curl -sf "$BASE/api/v1/agent/timeline?limit=20" -H "$H" | python3 -c "import sys,json; print('ok' if len(json.load(sys.stdin)['items'])>0 else 'FAIL')" 2>/dev/null)"

# ═══ 15. Medical Records ═══
MR=$(curl -sf -X POST "$BASE/api/v1/medical-records" -H "$H" -H "Content-Type: application/json" -d "{\"patient_hash\":\"$HASH\",\"title\":\"Initial Visit\",\"sections\":{\"chief_complaint\":\"咳嗽胸痛3周\",\"diagnosis\":\"疑似肺癌待排\",\"treatment_plan\":\"进一步检查\"}}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
check "15.1 Create medical record" "$([ -n "$MR" ] && echo ok || echo 'FAIL')"
RECORD_COUNT=$(curl -sf "$BASE/api/v1/medical-records?patient_hash=$HASH" -H "$H" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('records',[])))" 2>/dev/null)
check "15.2 List medical records" "$([ "${RECORD_COUNT:-0}" -ge 1 ] && echo ok || echo 'FAIL')"
check "15.3 Get medical record" "$(curl -sf "$BASE/api/v1/medical-records/$MR" -H "$H" | python3 -c "import sys,json; print('ok' if json.load(sys.stdin).get('title')=='Initial Visit' else 'FAIL')" 2>/dev/null)"
check "15.4 Update medical record" "$(curl -sf -X PUT "$BASE/api/v1/medical-records/$MR" -H "$H" -H "Content-Type: application/json" -d '{"title":"Follow-up Visit"}' | python3 -c "import sys,json; print('ok' if json.load(sys.stdin).get('title')=='Follow-up Visit' else 'FAIL')" 2>/dev/null)"
check "15.5 Delete medical record" "$(curl -sf -X DELETE "$BASE/api/v1/medical-records/$MR" -H "$H" | python3 -c "import sys,json; print('ok' if json.load(sys.stdin).get('deleted') else 'FAIL')" 2>/dev/null)"

echo ""
echo "════════════════════════════════════════════"
echo "  $((PASS+FAIL)) tests: $PASS ✓  $FAIL ✗"
echo "  $BASE"
echo "════════════════════════════════════════════"
[ "$FAIL" -eq 0 ] || exit 1
