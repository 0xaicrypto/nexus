# 医生工作流测试覆盖分析

## Step 1: 接诊 — 创建患者 + 上传影像/实验室

已经覆盖:
- POST /api/v1/dicom/patients/register-manual ✓
- GET /api/v1/dicom/patients/:hash/detail ✓  
- GET /api/v1/dicom/patients/full ✓
- POST /api/v1/files/upload ✓

缺失:
- [ ] 上传文件后自动分析内容并更新患者信息
- [ ] 影像/实验室文件内容提取到患者 findings

## Step 2: 接诊 — 患者 Chat + 记录更新

已经覆盖:
- POST /api/v1/agent/chat (with patient_hash) ✓
- postTurn 提取 facts ✓

缺失:
- [ ] 对话中提取的临床发现自动关联到患者
- [ ] 患者基本信息被对话内容更新

## Step 3: 研究 — 导入 DOCX

缺失:
- [ ] DOCX 文件解析 (需要 Python worker)
- [ ] 规则提取 + AI 确认
- [ ] 创建研究

## Step 4: 研究 — 确认并创建

已经覆盖:
- POST /api/v1/research/studies ✓
- GET /api/v1/research/studies ✓

缺失:
- [ ] 导入规则后的确认流程
- [ ] 规则存入 protocol 字段

## Step 5: 跨研究 Chat

缺失:
- [ ] 研究上下文注入 Chat
- [ ] 对话中摘要更新研究
- [ ] 研究进展追踪

## Step 6: 写作 — 引用研究/患者

已覆盖:
- POST /api/v1/docs/:id/chat ✓
- POST /api/v1/docs/:id/polish ✓

缺失:
- [ ] 引用患者数据到文档
- [ ] 引用研究结论到文档
- [ ] 文档中的引用链接
