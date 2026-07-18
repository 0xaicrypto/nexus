# Heurion 完整测试用例

## 测试环境
- 后端: http://localhost:8001
- 前端: http://localhost:5173
- 用户: HZ / hz123456 (admin)

---

## 1. 认证模块 (Auth)

### 1.1 注册新用户
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 打开 `/login?mode=register` | 显示注册表单 |
| 2 | 输入 username: `testuser`, password: `pass123`, displayName: `Test` | |
| 3 | 点击 Register | 跳转到 `/app/today`，sidebar 显示用户名 |
| 4 | 检查 role | 第一个注册的是 admin，后续是 user |

### 1.2 登录
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 访问 `/app/chat`（未登录） | 重定向到 `/login` |
| 2 | 输入 HZ / hz123456 / Login | 进入 `/app/today` |
| 3 | 刷新页面 | 保持登录状态，不需要重新登录 |
| 4 | 等待 24 小时 | token 过期，自动跳转 login |

### 1.3 登出
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 点击 sidebar 底部用户头像 → Logout | 跳转 login，清空 token |

---

## 2. 首页 (Today)

| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 登录后进入 `/app/today` | 显示问候语、统计数字 |
| 2 | 查看状态指标 | 显示 Patients、Reports、Conflicts 等计数 |
| 3 | 查看 Timeline | 显示最近的对话活动 |

---

## 3. Chat 对话

### 3.1 基础对话
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 进入 `/app/chat` | 显示 chat 界面，顶部显示 `deepseek/deepseek-chat` |
| 2 | 输入 "Hello, what can you do?" / Enter | DeepSeek 流式回复，每个 token 逐字出现 |
| 3 | 观察 SSE 流 | 应看到 reasoning_chunk → final_answer_chunk → turn_complete |
| 4 | 刷新页面 | 对话历史保留（从 `/api/v1/agent/messages` 加载） |

### 3.2 多轮对话
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 发送 "My name is John" | DeepSeek 回复 |
| 2 | 发送 "What's my name?" | 回复包含 "John"（记忆上下文生效）|

### 3.3 剪贴板附件
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 复制一张图片到剪贴板 | |
| 2 | 在 chat input 中 Ctrl+V / Cmd+V | 图片上传，显示附件 chip |
| 3 | 发送带附件的消息 | 附件 file_id 随消息发送 |
| 4 | 复制文件到剪贴板并粘贴 | 同上 |

### 3.4 Skills 切换
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 先在 Skills 页面安装 "Clinical Summary" | |
| 2 | 回到 Chat，点击 SkillsBar 中的 skill chip | 高亮/取消高亮 |
| 3 | 发送消息 | activeSkills 随请求发送 |

---

## 4. Patients 患者管理

### 4.1 患者列表
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 进入 `/app/patients` | 显示患者列表（可能为空） |
| 2 | 点击 "New Patient" 按钮 | 弹出创建对话框 |
| 3 | 填写 initials, age, sex, chief complaint | |
| 4 | 点击 Create | 新患者出现在列表中 |
| 5 | 点击患者名 | 跳转到 `/app/patients/:hash` |

### 4.2 患者详情
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 进入 `/app/patients/:hash` | 显示 Summary tab，包含 Findings、Medications、Timeline |
| 2 | 切换到 Chat tab | 显示患者专属对话 |
| 3 | 发送 "What findings do we have?" | DeepSeek 回复（含患者上下文） |
| 4 | 切换到 Imaging tab | 显示 DICOM 检查列表 |
| 5 | 切换到 Labs tab | 显示上传的文档 |
| 6 | 切换到 Memory tab | 显示 Findings/Medications/Timeline 详情 |
| 7 | 切换到 Report tab | 显示报告生成表单 |

---

## 5. Research 研究工作台

### 5.1 创建研究
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 进入 `/app/research` | 显示研究列表 |
| 2 | 输入 name: "Lung Cancer Phase II", short_code: "LC002" | |
| 3 | 点击 Create | 新研究出现在列表，显示 study_id 和 short_code |

### 5.2 研究详情 — Overview
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 点击研究 | 进入 `/app/research/:studyId` |
| 2 | 查看 Overview tab | 显示 study_id, display_name, status, created_at |

### 5.3 研究详情 — Roster
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 切换到 Roster tab | 显示入组患者列表 |
| 2 | 输入 patient_hash + 选择 arm / Enroll | 患者添加到 roster |
| 3 | 点击 Unenroll | 患者移出 roster |

### 5.4 研究详情 — Eligibility
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 切换到 Eligibility tab | 显示筛选列表 |
| 2 | 点击 Re-scan Eligibility | 对 roster 中患者重新筛选 |

### 5.5 研究详情 — Safety
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 切换到 Safety tab | 显示 observations 和 stop rules |
| 2 | 查看 Stop Rule Status | 显示 DLT rate、Grade 4/5 AE 状态 |
| 3 | 确认一个 observation | 设置 confirmed、grade、DLT |

---

## 6. Writing 写作工作室

### 6.1 文档管理
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 进入 `/app/writing` | 显示文档列表 |
| 2 | 输入 title / Create | 新文档出现在列表 |
| 3 | 点击文档 | 进入编辑器 |

### 6.2 文档编辑器
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 进入 `/app/writing/:docId` | 显示标题、正文 textarea |
| 2 | 编辑 title/body | 内容可编辑 |
| 3 | 点击 Save | 保存成功，创建 snapshot |
| 4 | 查看 Snapshots 历史 | 显示版本列表 |
| 5 | 点击 Restore 一个 snapshot | 正文恢复到该版本 |

### 6.3 AI Polish
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 选中一段正文文字 | |
| 2 | 点击 "AI Polish" + 输入指令 | SSE 流式返回润色后的文字 |

### 6.4 Doc Chat
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 打开 Doc Chat 面板 | |
| 2 | 输入问题 / 发送 | SSE 流式回复 |

### 6.5 PHI Scanner
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 在正文中写入 "John Smith, SSN 123-45-6789" | |
| 2 | 点击 PHI Scan | 高亮显示 Name 和 SSN 发现 |

---

## 7. Skills 技能市场

| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 进入 `/app/skills` | 显示已安装技能列表 |
| 2 | 搜索 "clinical" | 显示匹配的技能 |
| 3 | 点击 Install 一个技能 | 技能出现在已安装列表 |
| 4 | 切换 enabled toggle | 技能启用/禁用 |
| 5 | 点击 Uninstall | 技能从列表移除 |

---

## 8. Plugins 插件市场

| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 进入 `/app/plugins` | 显示三栏：Anthropic / GitHub / All |
| 2 | 切换到 All 源 | 显示所有来源的插件 |
| 3 | 搜索关键词 | 结果实时过滤 |
| 4 | Install / Uninstall | 与 Skills 共享已安装状态 |

---

## 9. Settings 设置

### 9.1 Profile
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 进入 `/app/settings` | 显示 Profile tab（默认） |
| 2 | 修改 Display Name / Organization / Intended Use | |
| 3 | 点击 Save | 保存成功，sidebar 名称更新 |

### 9.2 LLM 配置
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 切换到 LLM tab | 显示当前 provider/model/key status |
| 2 | 修改 provider 为 deepseek | |
| 3 | 输入 API Key / Save | 保存成功 |
| 4 | 点击 Test Connection | 返回 ok + latency |

---

## 10. Admin 管理 (仅 admin)

| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 以 HZ (admin) 登录 | sidebar 显示 Admin 入口 |
| 2 | 进入 `/app/admin/users` | 显示所有用户列表 |
| 3 | 点击 Disable 一个用户 | 用户状态变为 disabled |
| 4 | 点击 Enable | 恢复 |
| 5 | 点击 Reset Password / 输入新密码 | 密码重置成功 |
| 6 | 以非 admin 账号登录 | sidebar 不显示 Admin 入口 |

---

## 11. 边界情况

### 11.1 Token 过期
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 手动清除 localStorage `nexus-auth` | |
| 2 | 访问 `/app/chat` | 重定向 login |

### 11.2 404 页面
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 访问 `/app/nonexistent` | 显示 404 或重定向首页 |

### 11.3 并发请求
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 快速连续发送 3 条消息 | 每条独立处理，不冲突 |

### 11.4 长文本
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 发送 1000+ 字的消息 | DeepSeek 正常回复 |
| 2 | 发送超长消息 (>4096 tokens) | 后端截断或返回错误提示 |

---

## 12. Chat 跨 Tab 持久 (#1)

> SSE 流式对话在切换路由时不应中断

### 12.1 切换 Tab 不中断
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 在 `/app/chat` 发送一条长消息 | DeepSeek 开始流式回复 |
| 2 | Stream 中途切换到 `/app/research` | 页面正常切换，无报错 |
| 3 | 等待 10 秒后再切回 `/app/chat` | 之前的流式回复完整显示，最后一条消息 `isStreaming: true` 已变为 `false` |
| 4 | 发送新消息 | 正常回复，历史上下文保留 |

### 12.2 多 Tab 同时对话
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | Chat 发送消息 A，流式中 | |
| 2 | Patient detail → Chat tab 发送消息 B | 两个独立的 session，互不干扰 |
| 3 | 回到 Chat | 消息 A 完整，消息 B 在其他 session |

### 12.3 页面刷新恢复
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 对话 3 轮后刷新页面 | 3 轮对话从 `/api/v1/agent/messages` 恢复显示 |
| 2 | 发送第 4 轮 | 前端将 3 轮历史 + 新消息一起发给后端 |

---

## 13. 注意力权重与自动压缩 (#2)

> 长期对话中，系统自动提取 Facts，并按重要性×衰减率投影上下文

### 13.1 自动事实提取
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 连续发送 5 轮对话（讨论患者、诊断、治疗方案等） | |
| 2 | 第 5 轮后检查后端日志 | 出现 `[EVOLVE] Extracted N facts` |
| 3 | 查看 `GET /api/v1/chat/projection` | budget 中 `layer3_facts` > 0 items |

### 13.2 上下文投影预算
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 对话 10+ 轮后发送消息 | SSE 中包含 `context_info`，kind=`projection` |
| 2 | 观察 budget 格式 | `persona: Nt | patient: Nt | layer1_recent: Nt | layer2_episodes: Nt | layer3_facts: Nt | reserve: Nt` |
| 3 | 验证 Layer 1 | 最近 3 轮对话完整保留 |
| 4 | 验证 Layer 2 | 7 天内 Episodes 摘要出现 |
| 5 | 验证 Layer 3 | Facts 按 `importance × e^(-0.3 × days_ago)` 排序 |

### 13.3 记忆跨会话继承
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | Session A 中讨论 "患者对青霉素过敏，importance=5" | 5 轮后自动提取 fact |
| 2 | 创建新 Session B | |
| 3 | Session B 中问 "这个患者能用阿莫西林吗？" | 投影上下文包含 penicillin 过敏 fact |

---

## 14. 技能/插件市场 (#3)

> 分页 + 30+ 技能 + 多源搜索

### 14.1 分页
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 进入 `/app/skills` | 显示已安装技能 |
| 2 | 搜索 `clinical` | 返回匹配结果 + 分页信息 `total, page, page_size, total_pages` |
| 3 | 请求 page=2 | 第二页结果 |

### 14.2 多源搜索
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 进入 `/app/plugins` | 三列：Anthropic / GitHub / All |
| 2 | 切换到 Anthropic | 显示 4 个 Anthropic 技能 |
| 3 | 切换到 GitHub | 显示 20+ GitHub 技能 |
| 4 | 切换到 All | 显示所有 28 个技能 |

### 14.3 安装后状态同步
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | Skills 页安装 "Clinical Summary" | 已安装列表增加 1 项 |
| 2 | 切换到 Plugins 页 | "Clinical Summary" 显示 `installed: true` |
| 3 | 回到 Skills 页 Uninstall | Plugins 页刷新后显示 `installed: false` |

### 14.4 Enable/Disable 不删除
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 安装 "Safety Monitor" | |
| 2 | Toggle disable | 技能仍在列表，enabled: false |
| 3 | Toggle enable | enabled: true |
| 4 | Uninstall | 从列表移除 |

---

## 15. 对话上下文管理 (#5)

> Session CRUD + 历史加载 + 上下文窗口管理

### 15.1 Session 列表
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | Chat 页面对话 3 轮 | |
| 2 | 查看 session 列表（sidebar） | 显示 session，title 为第一条消息前 50 字 |
| 3 | 创建新 session | 新 session 出现在列表，自动切换 |
| 4 | 回到旧 session | 历史消息完整加载 |

### 15.2 上下文窗口溢出
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 单 session 内对话 50+ 轮 | 历史上下文超出 token 预算 |
| 2 | 发送第 51 条消息 | projection budget 显示最近 3 轮全保留，旧消息压缩为 episode 和 fact |
| 3 | 问 "我们最早讨论了什么？" | DeepSeek 从 episode/fact 中回答（不是完整历史） |

### 15.3 Patient 对话隔离
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | Patient A Chat tab 发送消息 | session_id = `patient-{hashA}` |
| 2 | Patient B Chat tab 发送消息 | session_id = `patient-{hashB}` |
| 3 | 查看 Agent Messages | 两个 patient 的历史独立，不混合 |

---

## 16. 记忆数据导出/导入 (#6)

> 用户可以将进化记忆（Facts/Episodes）导出为 JSON 并导入到其他环境

### 16.1 导出
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 对话 10+ 轮，积累 facts | `GET /api/v1/memory/export` |
| 2 | 查看响应 | JSON 包含 `facts[]`, `episodes[]`, `skills[]`, `event_log_count` |
| 3 | 验证 fact 结构 | 每个 fact 有 `id, category, importance, content, createdAt` |

### 16.2 导入
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | `POST /api/v1/memory/import` 发送导出的 JSON | |
| 2 | 查看响应 | `imported: N, facts_count: N` |
| 3 | 再次导出 | 新导入的 facts 出现在列表中 |

### 16.3 幂等导入
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 重复导入同一份数据 | facts 不会重复（基于 id 去重） |

---

## 17. 研究导入和分析 (#4)

### 17.1 协议导入
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | `POST /api/v1/research/studies` 创建研究 | |
| 2 | `POST /api/v1/research/studies/:id/import-protocol` 发送 protocol JSON | 返回 `{ imported: true, sections: N }` |

### 17.2 入排标准分析
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 创建研究 + 添加 protocol（含 inclusion/exclusion JSON） | |
| 2 | 注册患者 + Enroll 入研究 | |
| 3 | 点击 Re-scan Eligibility | 对每个 enrolled 患者运行规则匹配 + LLM 分析 |
| 4 | 查看 eligibility 结果 | `screenings[]` 包含 `verdict: eligible|ineligible|pending` + `criteria_results` |

### 17.3 安全分析
| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 添加 3 个 observations（含 1 个 DLT + 1 个 Grade 4） | |
| 2 | 确认 observation | `confirmed: true` |
| 3 | 查看 Stop Rule Status | DLT rate 计算正确，Grade 4/5 AE 触发正确 |

---

## 测试结果汇总

| # | 模块 | 测试项 | 状态 |
|---|------|--------|------|
| 1.1 | Auth | 注册 | |
| 1.2 | Auth | 登录 | |
| 1.3 | Auth | 登出 | |
| 2 | Today | 首页 | |
| 3.1 | Chat | 基础对话 | |
| 3.2 | Chat | 多轮对话 | |
| 3.3 | Chat | 剪贴板附件 | |
| 3.4 | Chat | Skills 切换 | |
| 4.1 | Patients | 患者列表 | |
| 4.2 | Patients | 患者详情 | |
| 5.1 | Research | 创建研究 | |
| 5.2 | Research | Overview | |
| 5.3 | Research | Roster | |
| 5.4 | Research | Eligibility | |
| 5.5 | Research | Safety | |
| 6.1 | Writing | 文档管理 | |
| 6.2 | Writing | 编辑器 | |
| 6.3 | Writing | AI Polish | |
| 6.4 | Writing | Doc Chat | |
| 6.5 | Writing | PHI Scanner | |
| 7 | Skills | 技能市场 | |
| 8 | Plugins | 插件市场 | |
| 9.1 | Settings | Profile | |
| 9.2 | Settings | LLM 配置 | |
| 10 | Admin | 用户管理 | |
| 11.1 | Edge | Token 过期 | |
| 11.2 | Edge | 404 页面 | |
| 11.3 | Edge | 并发请求 | |
| 11.4 | Edge | 长文本 | |
| 12.1 | Chat | 切换Tab不中断 | |
| 12.2 | Chat | 多Tab同时对话 | |
| 12.3 | Chat | 页面刷新恢复 | |
| 13.1 | Memory | 自动事实提取 | |
| 13.2 | Memory | 上下文投影预算 | |
| 13.3 | Memory | 记忆跨会话继承 | |
| 14.1 | Skills | 分页 | |
| 14.2 | Skills | 多源搜索 | |
| 14.3 | Skills | 安装后状态同步 | |
| 14.4 | Skills | Enable/Disable | |
| 15.1 | Session | Session列表 | |
| 15.2 | Session | 上下文窗口溢出 | |
| 15.3 | Session | Patient对话隔离 | |
| 16.1 | Memory | 导出 | |
| 16.2 | Memory | 导入 | |
| 16.3 | Memory | 幂等导入 | |
| 17.1 | Research | 协议导入 | |
| 17.2 | Research | 入排标准分析 | |
| 17.3 | Research | 安全分析 | |
