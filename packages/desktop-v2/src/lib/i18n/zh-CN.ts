/**
 * 简体中文 (zh-CN) — full translation of en-US.ts.
 *
 * Type-checked against ``Dict`` from en-US.ts. Adding a key in
 * English without translating it here = TS compile error. That keeps
 * the two files in lockstep.
 *
 * Translation guidelines used here:
 *
 *   * Medical terminology: use the term doctors actually use, not the
 *     literal back-translation. e.g. "Quick scan" → "快速扫描" (literal
 *     scan), not "快速浏览" (literal: quick browse).
 *
 *   * Action labels: keep ≤4 characters where possible so buttons
 *     don't stretch (Send → 发送, Cancel → 取消, Confirm → 确认).
 *
 *   * Sentences: end with a Chinese full stop "。" rather than ".".
 *     This is a subtle but consistent convention across the file.
 *
 *   * Placeholders: same ``{name}`` syntax as English. Don't translate
 *     the placeholder body; the formatter substitutes at runtime.
 *
 *   * UX text that references shell affordances (⌘. / ⌘K / esc) stays
 *     in roman characters — those are physical keys, not words.
 *
 *   * Tech terms with no clean Chinese equivalent (DICOM, SMTP, JWT,
 *     API, MRN, PHI) stay in English — local doctors recognise them
 *     in this form, and translating leaves the meaning ambiguous.
 *
 *   * "Nexus" stays as Nexus (product name).
 */
import type { Dict } from './en-US';

export const zh: Dict = {
  /* ────────────── Mode tabs ────────────── */
  'mode.today':     '今日',
  'mode.patient':   '病人',
  'mode.encounter': '问诊',
  'mode.imaging':   '影像',
  'mode.labs':      '化验',
  'mode.memory':    '记忆',
  'mode.report':    '报告',

  /* ────────────── Global header / sidebar ────────────── */
  'header.search':         '搜索…',
  'header.searchAria':     '搜索',
  'header.newPatient':     '新增病人',
  'header.back':           '后退',
  'header.forward':        '前进',
  'header.account':        '账户',
  'header.contextRail':    '上下文 (⌘.)',

  'sidebar.filter':        '筛选病人…',
  'sidebar.pinned':        '今日固定',
  'sidebar.all':           '全部',
  'sidebar.empty':         '还没有病人。点击 + 新增病人 开始。',

  /* ────────────── Login ────────────── */
  'login.title':           '临床工作流助手',
  'login.namePlaceholder': '张医生',
  'login.signIn':          '登录',
  'login.signingIn':       '登录中…',
  'login.devMock':         '不连后端继续（开发 / 模拟模式）',
  'login.help':            'M0：无密码登录 — 每次登录都会铸造新的 user_id。Passkey 支持稍后上线。',
  'login.diag.title':      '后端诊断',
  'login.diag.sidecarUp':  'Sidecar 进程运行中。',
  'login.diag.sidecarDown':'Sidecar 进程未运行。',
  'login.diag.serverUp':   '/healthz 可达。',
  'login.diag.serverDown': '/healthz 不可达 — sidecar 可能仍在启动。',
  'login.passkey.signIn':  '使用 Passkey 登录',
  'login.passkey.signUp':  '使用 Passkey 注册',
  'login.passkey.signingIn': '正在打开 Passkey 窗口…',
  'login.passkey.signupHint': '创建新账户并绑定到本机的一个 Passkey。',
  'login.passkey.signinHint': '使用本机已注册的 Passkey 登录。',
  'login.passkey.divider': '或',
  'login.passkey.error':   'Passkey 登录失败：{error}',
  'login.passkey.cancelled': 'Passkey 登录已取消。',

  /* ────────────── Boot gate ────────────── */
  'boot.starting':         '正在启动 Nexus…',
  'boot.migrating':        '应用数据库迁移中…',
  'boot.timeoutSoft':      '后端启动比预期慢 — 仍在重试。',
  'boot.failed':           '后端启动失败。',
  'boot.viewLogs':         '查看诊断',
  'boot.retry':            '重试',

  /* ────────────── Account menu ────────────── */
  'account.signedIn':      '已登录',
  'account.signedInHint':  '已登录',
  'account.settingsData':  '设置 · 数据',
  'account.composeEmail':  '撰写邮件…',
  'account.hasLearned':    'Nexus 已学到',
  'account.lightMode':     '浅色模式',
  'account.darkMode':      '深色模式',
  'account.signOut':       '退出登录',
  'account.language':      '语言',
  'account.languageEn':    'English',
  'account.languageZh':    '中文 (简体)',

  /* ────────────── Command palette ────────────── */
  'palette.placeholder':   '搜索病人、跳转模式、执行操作…',
  'palette.noMatches':     '没有匹配项',
  'palette.actionEmail':   '撰写邮件',
  'palette.actionEmailHint':'通过 relay / SMTP 发送',
  'palette.actionNewPatient': '新增病人',
  'palette.actionToday':   '返回今日',
  'palette.openMode':      '打开 {mode}',
  'palette.forPatient':    '· {patient}',
  'palette.esc':           'esc',

  /* ────────────── New patient dialog ────────────── */
  'newPatient.title':      '新增病人',
  'newPatient.intro':      '服务器会生成 PHI 安全的 hash。请填写姓名缩写（如 张三）或 MRN — 两者至少需要一项作为 hash 输入。',
  'newPatient.initials':   '姓名缩写',
  'newPatient.initialsPlaceholder': '张S.',
  'newPatient.mrn':        'MRN',
  'newPatient.mrnHint':    '（或姓名缩写）',
  'newPatient.mrnPlaceholder': 'MRN-12345',
  'newPatient.sex':        '性别',
  'newPatient.female':     '女',
  'newPatient.male':       '男',
  'newPatient.age':        '年龄',
  'newPatient.ageHint':    '（岁 — 服务器自动归入年龄段）',
  'newPatient.agePlaceholder': '65',
  'newPatient.reason':     '就诊原因',
  'newPatient.reasonHint': '（可选）',
  'newPatient.reasonPlaceholder': '胸痛、CT 复查 …',
  'newPatient.cancel':     '取消',
  'newPatient.create':     '创建病人',
  'newPatient.creating':   '创建中…',

  /* ────────────── Email composer ────────────── */
  'email.title':           '撰写邮件',
  'email.tagline':         '通过已配置的传输方式发送 — 优先 relay，否则使用直连 SMTP。',
  'email.probing':         '检查传输配置中…',
  'email.notConfigured':   '邮件传输未配置。请在 $RUNE_HOME/.env 中配置 NEXUS_RELAY_URL + NEXUS_RELAY_API_KEY（推荐）或 NEXUS_SMTP_* 后重新打开。',
  'email.viaRelay':        '通过 relay（{host}）',
  'email.viaSmtp':         '通过直连 SMTP',
  'email.sendingVia':      '发送方式：',
  'email.allowList':       '白名单已启用 · 共 {count} 个收件人',
  'email.to':              '收件人',
  'email.toHint':          '（用逗号分隔）',
  'email.toPlaceholder':   'colleague@hospital.org',
  'email.cc':              '抄送',
  'email.ccHint':          '（可选）',
  'email.subject':         '主题',
  'email.subjectPlaceholder': 'CT 影像所见 · 帕特尔先生',
  'email.body':            '正文',
  'email.invalidAddr':     '无效邮箱地址：{addrs}',
  'email.noRecipient':     '至少填写一个收件人',
  'email.recipients':      '{toCount} 个收件人{extra}',
  'email.ccExtra':         ' + 抄送 {ccCount}',
  'email.cancel':          '取消',
  'email.send':            '发送',
  'email.sending':         '发送中…',
  'email.sentToast':       '邮件已发送 · {to}',

  /* ────────────── Today mode ────────────── */
  'today.welcome':         '欢迎回来',
  'today.welcomeNamed':    '欢迎回来，{name}',
  'today.subline':         '从左侧选一位病人，或者跨所有病人向 Nexus 提问。',
  'today.pinned':          '今日固定',
  'today.pinnedEmpty':     '暂无固定项。当 Agent 有未读笔记时，相关病人会自动出现在此。',
  'today.ask':             '关于任一病人向 Nexus 提问',
  'today.askPlaceholder':  '输入问题或粘贴 MRN…',
  'today.llmAdvisoryTitle':'LLM 未配置',
  'today.llmAdvisoryCta':  '打开 设置 · LLM →',
  'today.allPatients':     '全部病人',

  /* ────────────── Patient mode ────────────── */
  'patient.noSelection':   '尚未选择病人',
  'patient.studies':       '{count} 项检查',
  'patient.unknown':       '—',
  'patient.loading':       '加载中…',
  'patient.loadFailed':    '加载失败：{error}',
  'patient.activeFindings':'当前发现',
  'patient.findingsEmpty': '暂无活跃发现。',
  'patient.unlabeled':     '（未命名）',
  'patient.emailFindings': '把发现发邮件给同事',
  'patient.emailHint':     '会打开撰写窗口并预填发现列表。PHI 注意：仅使用假名化标识，不含 MRN、出生日期。',
  'patient.medications':   '用药',
  'patient.medsEmpty':     '暂无用药记录。',
  'patient.recentImaging': '近期影像',
  'patient.imagingEmpty':  '暂无影像检查。',
  'patient.imagingLoading':'加载检查中…',
  'patient.deleteBtn':     '删除病人',
  'patient.deleting':      '删除中…',
  'patient.conflictBanner':'有 {count} 个未解决冲突 — 在记忆模式中处理',
  'patient.resolveCta':    '处理',

  /* ────────────── Encounter mode ────────────── */
  'encounter.noSelection':       '尚未选择病人',
  'encounter.session.label':     '{patient} · #{seq}',
  'encounter.session.default':   '默认会话',
  'encounter.session.new':       '新会话',
  'encounter.session.rename':    '重命名',
  'encounter.session.archive':   '归档',
  'encounter.session.confirm':   '确认',
  'encounter.session.cancel':    '取消',
  'encounter.session.switch':    '切换会话',
  'encounter.messages':          '{count} 条消息',
  'encounter.history.loading':   '加载历史中…',
  'encounter.history.empty':     '暂无消息，从下方开始对话。',
  'encounter.composer.placeholder': '关于这位病人的任何问题…',
  'encounter.composer.attach':   '附件',
  'encounter.composer.send':     '发送',
  'encounter.composer.sending':  '发送中…',
  'encounter.attachment.uploading': '上传 {name} 中…',
  'encounter.attachment.failed': '{name} · 上传失败',
  'encounter.attachment.ready':  '{name} · 就绪',
  'encounter.attachment.remove': '移除',
  'encounter.toast.newSession':  '已开启新的会话',
  'encounter.toast.sessionFailed':'无法创建会话：{error}',
  'encounter.toast.uploadFailed':'上传失败：{error}',
  'encounter.label.you':         '我',
  'encounter.label.nexus':       'Nexus',
  'encounter.label.system':      '系统',
  'encounter.reasoning.show':    '展开推理过程',
  'encounter.reasoning.hide':    '收起推理过程',

  /* ────────────── Imaging mode ────────────── */
  'imaging.title':         '影像',
  'imaging.noSelection':   '尚未选择病人',
  'imaging.uploadBtn':     '上传 DICOM…',
  'imaging.uploading':     '上传 {name} 中…',
  'imaging.history.title': '上传历史',
  'imaging.history.empty': '暂无上传。',
  'imaging.history.label': '{name} · {when}',
  'imaging.memoryStatus.pending':  '记忆：摄取中…',
  'imaging.memoryStatus.ok':       '记忆：已摄取',
  'imaging.memoryStatus.error':    '记忆：错误',
  'imaging.qsStatus.pending':      '快速扫描：等待中…',
  'imaging.qsStatus.running':      '快速扫描：运行中…',
  'imaging.qsStatus.ok':           '快速扫描：{summary}',
  'imaging.qsStatus.error':        '快速扫描：错误 · {summary}',
  'imaging.retry':         '重试',
  'imaging.retryQs':       '重跑快速扫描',
  'imaging.retryQsHint':   '对该检查重新跑一遍 Gemini Flash 分诊',
  'imaging.qs.scanning':   '正在分诊 {triaged}/{total} 网格 · {preset}',
  'imaging.qs.rendering':  '正在生成网格（{rendered}/{total}）',
  'imaging.qs.recentFindings': '近期发现',
  'imaging.modality.label':    '{modality} · {bodyPart}',

  /* ────────────── Labs mode ────────────── */
  'labs.title':            '化验',
  'labs.stub':             '化验模式为占位 — 将在 U3+ 阶段上线。',

  /* ────────────── Memory mode ────────────── */
  'memory.title':          '记忆',
  'memory.noSelection':    '尚未选择病人',
  'memory.layer1.title':   'L1 · 病人',
  'memory.layer1.tag':     '该病人 · 来自你的对话与导入',
  'memory.layer1.empty':   '暂无节点。当你讨论发现、用药、随访时，病人图谱会自动增长。',
  'memory.layer1.studies': '检查（{count}）',
  'memory.layer1.findings':'发现（{count}）',
  'memory.layer1.medications': '用药（{count}）',
  'memory.layer1.timeline':'时间线（{count}）',
  'memory.layer2.title':   'L2 · 你（医生）',
  'memory.layer2.tag':     '每位医生 · 跨病人 · 已剥离 PHI',
  'memory.layer2.intro':   'Nexus 学到的关于你"如何读片"的模式 — 表达习惯、工作流、阈值、建议校准。需在 ≥N 位病人上累积，且仅在你确认后激活。',
  'memory.layer2.empty':   '暂无内容。Nexus 会从你的提问方式和反复使用的工作流中提取模式，当在足够多位病人上观测到同一模式时浮出为候选（表达 ≥3 例、工作流 / 实践 ≥5 例、校准 ≥8 例）。任何模式开始影响 Agent 之前都会先请你确认。',
  'memory.layer2.confirm': '确认',
  'memory.layer2.reject':  '拒绝',
  'memory.layer2.cases':   '{count} 例 · {patients} 位病人',
  'memory.layer2.confidence': '置信度 {pct}%',
  'memory.layer3.title':   'L3 · 参考',
  'memory.layer3.tag':     '指南与文献',
  'memory.layer3.empty':   '暂无参考知识。',
  'memory.meta.title':     '元数据',
  'memory.meta.tag':       '关于记忆本身',
  'memory.tier.t1':        'T1 · 缓存',
  'memory.tier.t2':        'T2 · 模板',
  'memory.tier.t3':        'T3 · LLM',

  /* ────────────── Report mode ────────────── */
  'report.title':          '报告',
  'report.noSelection':    '尚未选择病人',
  'report.indication':     '检查指征',
  'report.indicationPlaceholder': '检查指征、既往治疗、对比检查 …',
  'report.findings':       '影像所见',
  'report.findingsPlaceholder': '从对话中提取的发现 — 可自由编辑。',
  'report.impression':     '诊断意见',
  'report.impressionPlaceholder': '综合判读 — 各项发现合起来说明什么。',
  'report.recommendation': '建议',
  'report.recommendationPlaceholder': '下一步、随访间隔 …',
  'report.exportPdf':      '导出 PDF',
  'report.exporting':      '导出中…',
  'report.exportFhir':     '导出 FHIR DiagnosticReport',
  'report.exportSr':       '导出 DICOM SR',
  'report.lastExport':     '上次导出 · {size} · {when}',
  'report.openFolder':     '在文件夹中查看',
  'report.exportFailed':   'PDF 导出失败：{error}',
  'report.exportedToast':  'PDF 已导出 · {size}',

  /* ────────────── Settings · Data ────────────── */
  'settings.title':        '设置',
  'settings.tab.llm':      'LLM',
  'settings.tab.data':     '数据',
  'settings.data.tagline': '你的数据属于你。导出格式开放、有完整文档。即使 Nexus 不再维护，记录也不会丢失。',
  'settings.data.backups.title':    '自动备份 · 本地 · 始终开启',
  'settings.data.backups.schedule': '计划',
  'settings.data.backups.scheduleValue': '每日凌晨 03:00 (本地时间)',
  'settings.data.backups.retention':'保留策略',
  'settings.data.backups.retentionValue': '每日 30 份 · 每周 12 份 · 每月 24 份',
  'settings.data.backups.location': '位置',
  'settings.data.openArchive':      '打开归档文件夹',
  'settings.data.export.title':     '导出全部数据',
  'settings.data.export.intro':     '生成自包含 zip，含 twin EventLog + 清单。EventLog 是规范的只追加源 — 所有投影可通过回放重建。FHIR R5 与 SQL dump 在 M3.3 finalize 中上线。',
  'settings.data.export.now':       '立即导出…',
  'settings.data.export.exporting': '导出中…',
  'settings.data.export.scheduleOn': '每月 · 已开启',
  'settings.data.export.scheduleOff':'设置每月导出…',
  'settings.data.export.last':      '上次导出 · {size} · {when}',
  'settings.data.export.reveal':    '在文件夹中查看',
  'settings.data.restore.title':    '从历史导出恢复',
  'settings.data.restore.local':    '从本地归档恢复',
  'settings.data.restore.import':   '导入归档…',
  'settings.data.toast.nothing':    '暂无可导出的数据 — 先聊天或导入一个检查。',
  'settings.data.toast.failed':     '导出失败：{error}',
  'settings.data.toast.scheduleOn': '已设置每月自动导出（本地设置）',
  'settings.data.toast.scheduleOff':'已关闭每月导出',
  'settings.data.toast.exported':   '已导出 {size} · {events} 条事件、{nodes} 个节点',

  /* ────────────── Settings · LLM ────────────── */
  'settings.llm.provider':    '提供方',
  'settings.llm.model':       '模型',
  'settings.llm.geminiKey':   'Gemini API key',
  'settings.llm.openaiKey':   'OpenAI API key',
  'settings.llm.anthropicKey':'Anthropic API key',
  'settings.llm.placeholderKey': 'AIza… / sk-… / sk-ant-…',
  'settings.llm.envPath':     'Key 写入位置：{path}',
  'settings.llm.save':        '保存',
  'settings.llm.saving':      '保存中…',
  'settings.llm.restart':     '重启 sidecar',
  'settings.llm.restartHint': '终止 sidecar 进程并重新拉起',
  'settings.llm.advisory':    '{provider} 还未配置 API key。配置之前，对话与推理将无法工作。',
  'settings.llm.savedToast':  'LLM 设置已保存。',

  /* ────────────── Practitioner overlay ────────────── */
  'practitioner.title':       'Nexus 已学到',
  'practitioner.intro':       'Nexus 在跨病人观察中发现的模式。符合你真实读片习惯的请确认，其余请拒绝。',
  'practitioner.empty':       '暂无候选。Layer 2 需要 ≥3 位（表达）到 ≥8 位（校准）不同病人才会开始浮出候选。',
  'practitioner.activeNote':  '已激活模式：{count} 项',
  'practitioner.askLater':    '稍后再问',
  'practitioner.seeCases':    '查看案例',
  'practitioner.confirm':     '确认',
  'practitioner.reject':      '拒绝',

  /* ────────────── Toast / banner generic ────────────── */
  'toast.dismiss':         '关闭',
  'banner.llmTitle':       'LLM 未配置',
  'banner.llmCta':         '现在配置',

  /* ────────────── Scheduled tasks ────────────── */
  'sched.proposalTitle':   '计划任务',
  'sched.proposalIntro':   'Nexus 检测到一个未来动作意图。确认即调度，可编辑后再确认，也可取消。',
  'sched.fireAt':          '时间',
  'sched.kind':            '内容',
  'sched.kind.sendEmail':  '发送邮件',
  'sched.to':              '收件人',
  'sched.subject':         '主题',
  'sched.subjectPlaceholder': '可选 — 邮件主题',
  'sched.body':            '正文',
  'sched.bodyPlaceholder': '可选 — 邮件正文',
  'sched.recipient':       '收件人',
  'sched.recipientPlaceholder': 'colleague@hospital.org',
  'sched.confirm':         '确认并调度',
  'sched.scheduling':      '调度中…',
  'sched.cancel':          '取消',
  'sched.edit':            '编辑',
  'sched.scheduledToast':  '已调度 · {when}',
  'sched.scheduleFailed':  '调度失败：{error}',
  'sched.account':         '我的计划任务（{count}）',
  'sched.listTitle':       '计划任务',
  'sched.listEmpty':       '暂无计划任务。在聊天中说 "两小时后给 X 医生发邮件" 等即可创建。',
  'sched.status.pending':  '待执行',
  'sched.status.running':  '执行中',
  'sched.status.done':     '已完成',
  'sched.status.error':    '错误',
  'sched.status.cancelled':'已取消',
  'sched.cancelTask':      '取消任务',

  /* ────────────── Empty state ────────────── */
  'empty.noPatient':       '尚未选择病人',
  'empty.modeStub':        '{mode} 模式 — 敬请期待。',

  /* ────────────── Time ────────────── */
  'time.justNow':          '刚刚',
  'time.minsAgo':          '{n} 分钟前',
  'time.hoursAgo':         '{n} 小时前',
  'time.daysAgo':          '{n} 天前',
};
