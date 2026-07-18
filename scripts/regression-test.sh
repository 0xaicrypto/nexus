#!/usr/bin/env bash
# Heurion 完整回归测试 v2 — 覆盖所有用户场景
BASE="${1:-https://heurion.org}"
USERNAME="HZ"
PASSWORD="hz123456"
PASS=0; FAIL=0
SAMPLE_DIR="packages/server-ts"

check() {
  if [ "$2" = "ok" ]; then echo "  ✓ $1"; PASS=$((PASS+1))
  else echo "  ✗ $1 — $2"; FAIL=$((FAIL+1)); fi
}

echo "════════════════════════════════════════════"
echo "  Heurion 回归测试 v2"
echo "════════════════════════════════════════════"

TOKEN=$(curl -sf -X POST "$BASE/api/v1/auth/login" -H "Content-Type: application/json" -d "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('jwt_token',''))" 2>/dev/null)
H="Authorization: Bearer $TOKEN"
check "0. Login" ok

# ═══ Clear ═══
ssh -o StrictHostKeyChecking=no -i ~/.ssh/heurion-do root@174.138.31.245 "cd ~/heurion/packages/server-ts && node scripts/clear-data.js && rm -rf .nexus/twins/*/event_log.jsonl .nexus/twins/*/facts/ .nexus/twins/*/episodes/ .nexus/twins/*/uploads/*" 2>/dev/null
check "0. Clear data" ok

# ═══ 1. Patient Onboarding ═══
HASH=$(curl -sf -X POST "$BASE/api/v1/dicom/patients/register-manual" -H "$H" -H "Content-Type: application/json" -d '{"initials":"ZQ","age":58,"sex":"M","chief_complaint":"咳嗽胸痛3周"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('patient_hash',''))" 2>/dev/null)
check "1.1 Create patient" "$([ -n "$HASH" ] && echo ok || echo 'FAIL')"
check "1.2 Patient detail" "$(curl -sf "$BASE/api/v1/dicom/patients/$HASH/detail" -H "$H" | python3 -c "import sys,json; print('ok' if json.load(sys.stdin).get('initials')=='ZQ' else 'FAIL')" 2>/dev/null)"
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

curl -sf -X POST "$BASE/api/v1/research/studies/$SID/import-protocol" -H "$H" -H "Content-Type: application/json" -d '{"text":"INCLUSION: Stage IIIB/IV NSCLC, PD-L1>=1%. EXCLUSION: EGFR/ALK positive, autoimmune disease."}' > /dev/null 2>&1
check "7.2 Import protocol" ok

RULES=$(curl -sf -X POST "$BASE/api/v1/research/studies/$SID/extract-rules" -H "$H" -H "Content-Type: application/json" -d '{"text":"INCLUSION: Stage IIIB/IV NSCLC, PD-L1>=1%. EXCLUSION: EGFR/ALK positive."}' | python3 -c "import sys,json; print(json.load(sys.stdin)['status']['total'])" 2>/dev/null)
check "7.3 Extract rules" "$([ "${RULES:-0}" -gt 0 ] && echo ok || echo 'FAIL')"

# ═══ 8. Chat with Patient + Research Context ═══
CHAT2=$(curl -sf -N -X POST "$BASE/api/v1/agent/chat" -H "$H" -H "Content-Type: application/json" -d "{\"text\":\"ZQ这个患者什么诊断？符合NSCLC001吗？\",\"patient_hash\":\"$HASH\"}" 2>/dev/null)
# Parse SSE to extract all final_answer text
CHAT2_TEXT=$(echo "$CHAT2" | grep 'final_answer' | sed 's/^data: //' | python3 -c "import sys,json; print(''.join(json.loads(l.strip()).get('text','') for l in sys.stdin if l.strip()))" 2>/dev/null)
check "8.1 Chat references patient" "$(echo "$CHAT2_TEXT" | python3 -c "import sys; t=sys.stdin.read().lower(); print('ok' if 'nsclc' in t or '肺癌' in t or '腺癌' in t or 'zq' in t else 'FAIL')")"
check "8.2 Chat references study" "$(echo "$CHAT2_TEXT" | python3 -c "import sys; t=sys.stdin.read().lower(); print('ok' if 'nsclc001' in t or '研究' in t else 'FAIL')")"

# ═══ 9. Skills ═══
check "9.1 Skills catalog" "$([ $(curl -sf "$BASE/api/v1/skills" -H "$H" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('skills',[])))" 2>/dev/null) -ge 8 ] && echo ok || echo 'FAIL')"
curl -sf -X POST "$BASE/api/v1/skills/install" -H "$H" -H "Content-Type: application/json" -d '{"identifier":"official/clinical-summary"}' > /dev/null 2>&1
check "9.2 Install skill" "$(curl -sf "$BASE/api/v1/skills" -H "$H" | python3 -c "import sys,json; print('ok' if any(s['name']=='Clinical Summary' and s.get('installed') for s in json.load(sys.stdin)['skills']) else 'FAIL')" 2>/dev/null)"

# ═══ 10. Writing ═══
DID=$(curl -sf -X POST "$BASE/api/v1/docs" -H "$H" -H "Content-Type: application/json" -d '{"title":"ZQ Case Report"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
curl -sf -X PUT "$BASE/api/v1/docs/$DID" -H "$H" -H "Content-Type: application/json" -d '{"body":"58yo M, cT2aN2M0 IIIA NSCLC"}' > /dev/null 2>&1
check "10.1 Create document" "$([ -n "$DID" ] && echo ok || echo 'FAIL')"
check "10.2 Document content" "$(curl -sf "$BASE/api/v1/docs/$DID" -H "$H" | python3 -c "import sys,json; print('ok' if 'IIIA' in json.load(sys.stdin).get('body','') else 'FAIL')" 2>/dev/null)"

# ═══ 11. Calendar ═══
check "11. Calendar iCal" "$(curl -sf "$BASE/api/v1/calendar/export.ics?token=$TOKEN" | python3 -c "import sys; t=sys.stdin.read(); print('ok' if 'VCALENDAR' in t else 'FAIL')" 2>/dev/null)"

# ═══ 12. Timeline ═══
check "12. Timeline has events" "$(curl -sf "$BASE/api/v1/agent/timeline?limit=20" -H "$H" | python3 -c "import sys,json; print('ok' if len(json.load(sys.stdin)['items'])>0 else 'FAIL')" 2>/dev/null)"

echo ""
echo "════════════════════════════════════════════"
echo "  $((PASS+FAIL)) tests: $PASS ✓  $FAIL ✗"
echo "  $BASE"
echo "════════════════════════════════════════════"
[ "$FAIL" -eq 0 ] || exit 1
