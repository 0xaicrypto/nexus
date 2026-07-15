# End-to-End 测试用例 · Research-first 临床工作流

> 假设环境:刚 build 完的 `.dmg`,数据库**全新空**(本轮清干净了 `nexus_server.db` 和 `~/.nexus_server/`)。
> 测试时间约 30–45 分钟。每个步骤后面有"✅ 预期"和"❌ 如果发现"两栏。

---

## Phase 0 · 启动 + 登录(5 分钟)

| # | 操作 | ✅ 预期 | ❌ 如果发现 |
|---|---|---|---|
| 0.1 | 双击 `/Applications/Nexus.app` | 应用打开,先出 splash → LoginView | 卡 splash > 30s → 看 `~/Library/Logs/Nexus/server.log` 是否 sidecar 启动失败 |
| 0.2 | 输入显示名(如"测试医生"),提交 | 进入主界面;**默认应该落在"研究" workspace**(顶部 segmented control 右侧高亮) | 落到"今日固定"页 → store.ts:251 的 fresh-install default 没生效 |
| 0.3 | 看顶部 `[ 患者 ｜ 研究 ]` 段控 + 左侧 RESEARCH sidebar | 段控两侧字体/颜色一致;sidebar 显示 "+ 新建研究" 按钮和"MY STUDIES (空)" | 段控样式不一致 → B3 fix 没生效 |

---

## Phase 1 · 创建第一个研究项目(导入 .docx)— 10 分钟

| # | 操作 | ✅ 预期 | ❌ 如果发现 |
|---|---|---|---|
| 1.1 | 点 "+ 新建研究" | 弹窗"新建研究",顶部 toggle "手动填写 / 导入 .docx",默认在导入 | 没弹窗 → 看 DevTools Console |
| 1.2 | 切到"导入 .docx" tab(应该已默认),拖一个**真实的 .docx 协议文件**进虚线框,或点击选 | 显示"解析中…",几秒后跳到 Review 页;基础信息(显示名/简称/期次)自动填好;入选/排除标准/访视计划应有若干条 | "docx open failed: Package not found" → 看 server 日志:很可能是 **(1)** 文件不是真 docx(是 .doc OLE 或 PDF 改后缀)→ 应该报"legacy .doc / 请另存为 .docx"; **(2)** 中文文件名 NFC/NFD 不一致 → 应该自动找到 NFD twin |
| 1.3 | Review 页:逐条看入选/排除标准,有需要可改 kind(auto-rule / auto-llm / manual)、规则文本 | 每条可编辑;增加 / 删除按钮可用 | 编辑无效 → 表单 state 没 bind |
| 1.4 | 拉到底,点"建立研究 · 写入 X 入选 / Y 排除 / Z 访视" | 弹窗关闭,sidebar MY STUDIES 出现这条研究,自动选中,主区域跳到该研究的"概览" tab | 失败 → server 日志看 POST /studies / PATCH 是哪一步挂 |
| 1.5 | 在概览 tab 看 KPI 4 卡片(入组进度 0、候选总数 0、待医生 0、中位随访 —) | 全部为 0,因为还没患者 | 显示 N/A 或假数字 → 数据没拉到 |

**Stretch test**: 重复 1.1–1.4 导入第二个不同协议(比如 IV 期 Hybrid RT),验证两个研究并存,sidebar 有两条。

---

## Phase 2 · 录入病例(支持手动 + DICOM)— 8 分钟

| # | 操作 | ✅ 预期 |
|---|---|---|
| 2.1 | 切回顶部"患者"workspace(段控左侧) | 进入患者 workspace,左 sidebar 显示"今日固定"和"全部"两组(都空) |
| 2.2 | 点右上角"+ 新增病人"(若没有,Cmd+K 唤起 command palette 输 "新增") | 弹窗,字段:姓名首字母 / MRN / 性别 / 年龄段;**MRN 或 姓名首字母至少填一项** |
| 2.3 | 填:姓名首字母 `Z.S`(张三)、性别 M、年龄段 40s,提交 | 列表出现"张三 · #1" patient card;自动选中并跳到该患者的"病人"tab |
| 2.4 | 验证 patient card 风格 | 选中态用 `rw-accent-bg` 青色边框 + chip(MR 等模态)在右侧 |
| 2.5 | 上传 DICOM:Cmd+K → "Upload DICOM",拖一个 .zip(整套 study)或一个 .dcm 文件 | 进入上传中状态,顶部出现 async-task 进度条;几秒后"近期影像"区出现 thumbnail(MR / CT / PET-CT 等 modality chip) |
| 2.6 | 点 thumbnail 上的"open viewer →" | 系统外部浏览器或 sidecar webview 打开 OHIF 查看器 |

**Stretch**: 创建第 2、第 3 个患者(李四 / 王五),分别上传不同 modality 的 DICOM。

---

## Phase 3 · 自动入排扫描(核心场景)— 6 分钟

> **关键体验**:你新录的病例,系统**自动**判断他/她符合哪些已有研究的入排标准,并在 Eligibility Inbox 推送候选。

| # | 操作 | ✅ 预期 |
|---|---|---|
| 3.1 | 切到顶部"研究"workspace,选第一个研究(8Gy 那个) | 进入该研究的概览 tab |
| 3.2 | 切到"入排清单"tab | 看到"候选患者 (N)"卡片,N = 系统刚才自动扫出来命中此研究入排的患者数 |
| 3.3 | 看右上"自动扫描已开启 / 重新扫描"按钮 | toggle 应该默认 ON;"重新扫描"会立即跑 |
| 3.4 | 如果 N=0,点"重新扫描" | 几秒后 N 更新;若你录的张三符合(IV 期 NSCLC 等条件命中),会出现一条 CandidateCard:显示患者 hash、入选命中条目、排除未命中条目、整体置信度 |
| 3.5 | 点 CandidateCard 上的"邀请入组" | 弹 InviteModal,确认 arm + 入组日期 + 备注 |
| 3.6 | 提交 InviteModal | 候选条消失,该患者出现在"入组名单"tab,enrollment_seq=#001 |

**❌ 常见踩坑**:
- 候选始终 0 → 检查患者的临床图谱里是否真有命中条目的实体(可在患者"记忆"tab 看)。Eligibility engine 需要至少:疾病分期 + 病理类型。
- 命中条目对但置信度很低 → 入排规则的 `rule_dsl` 可能没生成好(Phase 2 导入时的 LLM 解析输出),手动编辑该研究 → 入排清单的规则。

---

## Phase 4 · 入组后的访视 / 评估提醒 — 6 分钟

| # | 操作 | ✅ 预期 |
|---|---|---|
| 4.1 | 该研究 → "进度计划" tab | 看到入组的张三横向 Gantt 条,标注访视点(基线 / cCRT week 3 / 12 周 CT 等),按协议时间偏移自动生成 |
| 4.2 | 切到"今日"workspace 或左下角 Inbox | 应该有"今日待办 X 项"提示,内含张三的下一次访视(如距今 7 天内) |
| 4.3 | 回到该研究 → 进度计划 → 点某个即将到来的访视点 | 弹出 VisitChecklistModal,列出该次访视协议要做的所有动作(查血常规、CT、PRO 问卷…) |

**⚠️ 当前缺口**:VisitChecklistModal **还没实现**(visual-mock README 标 ✗)。点了应该没反应或报错 —— 留作下一轮。

---

## Phase 5 · 安全性 / AE 流(本轮新加 + Stop-rule)— 8 分钟

| # | 操作 | ✅ 预期 |
|---|---|---|
| 5.1 | 该研究 → "安全性" tab | **不再是假数据**:顶部 Stop-rule 条空白(无配置或 0 / N DLT),下方"暂无安全性事件",指引去手动录或等 Patient SOAP 镜像 |
| 5.2 | 右上点 "+ 记录 AE" | 弹 RecordObservationDialog;患者下拉自动列出入组的张三;输:类别 `肺部毒性`,grade `G2`,**不勾 DLT**,摘录 `门诊主诉气紧 + 干咳第 3 周` |
| 5.3 | 保存 | dialog 关,列表出现一条事件:虚框 G2 + "← 待医生确认" |
| 5.4 | 点 G2 按钮(同一颜色,实框) | 该条变实框 G2,显示"✓ {timestamp}",Stop-rule 条**不动**(G2 不计入 DLT) |
| 5.5 | 再 + 记录 AE:类别 `肺部毒性`,grade `G3`,**勾 DLT** | 列表新增一条,虚框 G3 + DLT pill |
| 5.6 | 点 G3 实心 | DLT 计数器从 0 → 1。Stop-rule 条出现 1 段橙色填充 + 文案"距 stop-rule 还有 1 例 DLT 余量" |
| 5.7 | 再造一条 G4 DLT → 确认 | 计数器 1 → 2 → **达到阈值**:条变**红**,文案变"已达 stop-rule 阈值 — 2/2 例 DLT,按协议应暂停入组并提交安全审查" |
| 5.8 | 选某条 G4,点"解除关联(误判)",输理由 | 该条从列表消失,DLT 计数 2 → 1,条变回橙色 |

✅ **过这一关说明 SafetyTab 整条 read+write+aggregate 闭环都通了。**

---

## Phase 6 · 研究内对话 / 跨患者聊天 — 5 分钟

| # | 操作 | ✅ 预期 |
|---|---|---|
| 6.1 | 该研究 → "研究对话" tab | 顶部 scope chip 显示"X 入组 + Y 候选",左侧聚焦选择器(`— 不聚焦（cohort 模式）—`) |
| 6.2 | 问 `入组 ≥1 月的患者中谁出现 G2 肺部毒性?` | AI 流式回应,引用具体患者(应该出张三),scope_info 显示 `cohort 1 例` |
| 6.3 | **粘贴图片测试**:截一张张图复制,直接 Cmd+V 到输入框 | 输入框上方出现一个 chip:"图片名 · 大小K · ⟳ 上传中" → 几秒后变 ✓ → 发送 |
| 6.4 | **拖文件测试**:从 Finder 拖一个 .pdf 进输入框区域 | 同上,出现 chip + 上传完成 |

✅ **过这一关说明 B2 Research Chat 附件支持生效。**

---

## Phase 7 · 患者 Chat(问诊)隔离测试 — 5 分钟

> **专门测刚修的 B5:每个患者独立 session,不再混。**

| # | 操作 | ✅ 预期 |
|---|---|---|
| 7.1 | 切顶部到"患者"workspace,选张三 → "问诊"tab | **空对话**(因为新 sessionId 派生为 `patient-{hashOf张三}`,而这是第一次用) |
| 7.2 | 发 `你这位患者的初诊主诉是什么?` | AI 流式回应,只引用张三的数据 |
| 7.3 | 左 sidebar 点李四 → 问诊 tab | **再次是空对话**(派生成 `patient-{hashOf李四}`),不应该看到张三那条消息 |
| 7.4 | 在李四这里发 `给我列一下你的化验异常` | AI 只引用李四 |
| 7.5 | 切回张三 → 问诊 | 看到张三之前那条 + AI 回复,**不应该有李四的内容** |

✅ **过这关说明 B5 session 隔离生效。** ❌ 如果切到张三还能看到李四 → 前端 effectiveSessionId 没生效或派生错。

---

## Phase 8 · 删除 / 归档(本轮新加)— 3 分钟

| # | 操作 | ✅ 预期 |
|---|---|---|
| 8.1 | "研究" sidebar 上某条研究 hover | 卡片右上角出现淡灰色垃圾桶 icon(SVG),hover 上去变红 |
| 8.2 | 点垃圾桶 | 弹 DeleteStudyDialog,告知"研究从侧栏隐藏,但所有事件、入组、筛查记录都保留(GCP 合规)" |
| 8.3 | 点"归档" | 该研究从 MY STUDIES 消失;若它是当前选中的,主区域 fallback 到 EmptyState;若有入组,弹窗里之前会显示橙色 "已有 X 例入组" 警告 |
| 8.4 | 验证点垃圾桶**不会**误触发选中卡片(stopPropagation) | 卡片不被选中 / activeStudyId 不变 |

---

## Phase 9 · 一键报告导出 — 3 分钟

| # | 操作 | ✅ 预期 |
|---|---|---|
| 9.1 | 研究 → "报告导出" tab | 看到 "中期报告" / "CONSORT 流图" / "全数据 .xlsx" 三个按钮 |
| 9.2 | 点 "中期报告" | 应该触发 async-task,生成 .pdf 后弹下载 |

**注意**:Phase 4 在 ROADMAP 里,可能 PDF 生成是 stub。能跑就 OK,不能跑也属正常。

---

## 必死必查(测完之后再做一遍 sanity check)

- [ ] **DevTools Console**:打开任意一个 tab 切一遍 5-7 → Console 应该**没有红色 error**。绿/橙 warning 可接受
- [ ] **Network 面板**:观察任意请求 URL 都是 `http://127.0.0.1:8001/api/v1/...`,**绝不应该出现 `tauri://localhost/api/...`**(出现就是 baseUrl 没拼)
- [ ] **Authorization header**:任意请求都应该有 `Bearer ey...`(不是 `Bearer null` 或缺失)
- [ ] **检查 sidecar 日志**:`~/Library/Logs/Nexus/server.log` 末尾应该没有 traceback;`docx open failed` / `Package not found` 这种字眼**不应该有**

---

## 截图收集清单(测完发给我)

完成后请给我以下截图,我用来确认:

1. **Phase 0**:登录后首屏(应该是 Research workspace)
2. **Phase 1.4**:刚建好的研究的 overview tab
3. **Phase 3.4**:Eligibility 候选清单出现一条 CandidateCard
4. **Phase 5.6**:Stop-rule 条变橙后的样子
5. **Phase 5.7**:Stop-rule 红色触发态
6. **Phase 7.5**:切回张三后看到的对话(应该不含李四)

---

## 如果某一步挂了

把以下三样发我,我能定位到根因:

1. **截图**:UI 上看到的错误文案
2. **DevTools Console 截图**:那一刻的 Console 红色行
3. **服务端日志末 40 行**:`tail -n 40 ~/Library/Logs/Nexus/server.log`

---

_祝测试顺利。_
