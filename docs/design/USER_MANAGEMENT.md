# 用户管理设计 — Nexus 桌面工作站

**状态**: Draft v1.0
**作者**: JZ + Claude
**日期**: 2026-06-27
**适用范围**: Nexus.app desktop (`packages/desktop-v2`) + 本地 sidecar (`packages/server`)

本文档定义 Nexus 桌面端的"用户是谁、怎么登陆、如何切换"完整方案。
它要回答几个具体问题：

1. 首次打开 app，看到什么？
2. 关掉再打开，是不是要重新登陆？
3. 一台 Mac 多个医生轮班用，怎么办？
4. 重装 app 之后，数据还在吗？
5. 别人偷了我的笔记本，能看到我的数据吗？
6. 这是临床工具，PHI 合规要求怎么满足？

它取代前面三次失败的尝试（display-name 表单、WebAuthn passkey、OS Keychain），
作为长期方案落地。

---

## 1. 目标与反目标

### 目标 (要做的)

- **零摩擦默认体验**：常规使用者打开 app 就用，不弹任何系统权限、不需要记密码、不需要点 TouchID。
- **数据隔离**：每个身份的数据（患者、记忆、设置）严格分开；一个身份看不到另一个的内容。
- **跨重装持久**：用户在 Settings 里配的 API key、积累的患者档案，重装 dmg 后还在。
- **跨 Mac 用户隔离**：同一台机器上不同 macOS 账户互不影响（已经由文件系统层保证）。
- **简单切换**：偶尔两个医生共用，能 1 秒内切换。
- **诚实标注**：UI 不假装"安全登陆"——身份是本地的，没有强认证，文档明说。

### 反目标 (本设计 v1.0 范围之外，但已有路线图衔接)

- **生物识别锁屏**：TouchID 解锁 app 本身。可作为后续 opt-in 增强（§7），不是默认。
- **密码 / OAuth / SSO**：临床工作站没人想再多记一个密码。Managed Mode 例外（见 §13），用一次性激活码而非密码。
- **机构间数据共享**：跨医院 federation 是另一个项目。
- **HIPAA 完整合规**：本设计满足"per-user filesystem isolation"和"audit log"的基线，但不承诺 BAA、SOC2、加密静态存储等企业级特性。这些通过部署到企业服务器版本提供。

### 已纳入路线图的扩展目标 (本设计 v1.0 设计完，分阶段实施)

- **Managed Mode（商业化、机构版）**：一名超级管理员能远程激活/吊销/审计医生账户，但**临床数据 (PHI) 永远不离开医生本机**。控制面只管账户元数据 + license + 使用计量。详见 §10。
- **跨设备同步（同一医生的多台 Mac 一致）**：办公室一台、家里一台、移动一台都看到同样的患者库 + 记忆 + 设置。**端到端加密**——服务器只见密文，不见 PHI。详见 §11。
- **多机构同医生**：一个医生可同时归属多个机构（例如：私立医院 + 研究中心），每个机构是独立的身份 + 独立的数据空间。
- **离线降级**：管理面不可达时，已激活的医生仍能正常工作；网络恢复后增量同步审计事件。

---

## 2. 用户画像与威胁模型

### 人物画像

| 代号 | 描述 | 比例估计 | 部署模式 | 默认体验目标 |
|---|---|---|---|---|
| **P1** | 单独使用自己 Mac 的肿瘤科研究员（独立用户）| ~60% | Local | 零摩擦，零弹窗 |
| **P2** | 两名 PI 共用实验室工作站（独立用户）| ~5% | Local | 切换账号 ≤2 次点击 |
| **P3** | 住院医 / 主治医轮班，同一台机器（独立用户）| ~3% | Local | 同 P2 |
| **P4** | 机构买单、超级管理员配发账号给 5-50 名医生 | ~25% | Managed | 凭管理员发的激活码一次激活 |
| **P5** | 大型医院 IT 部署、需 SSO/audit/license seat 管理 | ~7% | Managed Enterprise | 凭 SSO 登陆，admin 批量配置 |
| **P6** | 未来云版本远程协作（跨机器同身份） | 0%（不在本设计范围）| Cloud | 用云版本 |

P1-P3 走 §1-§12 的 Local Mode；P4-P5 走 §13 的 Managed Mode；它们**共用同一份 dmg 二进制**，只是首次启动时根据用户选择走不同分支。

### 威胁模型

| 威胁 | 防御层 | 本设计承诺 |
|---|---|---|
| 同机器其他 macOS 用户读到数据 | macOS 用户文件系统隔离 (`~/Library/Application Support/...`) | ✓ 自动 |
| 笔记本被偷 | FileVault 全盘加密（macOS 默认开启）| 用户责任，文档建议开 |
| 攻击者拿到 dmg 解包 | 不在 dmg 里塞 secret | ✓ F17 已修 |
| 同一个 macOS 账户下的恶意进程读 identity.json | 文件 0600 + Mac 用户密码 | 部分（不是强威胁模型）|
| 一个身份越权读另一个身份的数据 | 后端 `WHERE user_id = ?` 强制过滤 | ✓ 所有 router 已有 |
| 医生 A 离开后医生 B 拿同一身份看 A 的患者 | 切换身份机制 | 需要 P2 设计 |
| LLM API key 被偷 | DB 不暴露 + Settings 里 mask preview | ✓ F12/F16 已做 |
| PHI 跨身份污染 (Layer 2/2b takeaway 越界) | per-user PK + 删除时级联清理 | ✓ F13 已做 |
| **Managed Mode**: 管理员越权读医生的 PHI | 控制面**没有** PHI 通道 — 它只见账号元数据 | §13.4 |
| **Managed Mode**: 离职医生数据被前同事拿走 | 管理员可远程吊销 → 下次启动 wipe 本机 user data | §13.5 |
| **Managed Mode**: License token 被复制到非授权机器 | token 绑 machine_fingerprint（CPU + 主板 UUID 哈希）| §13.6 |
| **Managed Mode**: 控制面被攻陷 → 大规模 PHI 泄露 | 控制面不存 PHI；攻陷面只是吊销凭据/审计日志 | §13.4 (架构层防御) |

### 不打算防的

- macOS 物理访问 + 已登陆账户：app 内数据没有二次加密。如果医生离开未锁屏，旁人能看见全部内容。这不是产品能在桌面端解决的——用 macOS 锁屏（自动息屏锁屏）防御。

---

## 3. 核心原则

1. **最小权限**：默认不申请任何系统级权限（Keychain、TouchID、文件 sandbox 例外、相机、定位、…）。
2. **本地优先**：身份完全在本机，不在云上注册。
3. **诚实标注**：不把"身份"叫做"账号"，UI 上明说这是本地 profile。
4. **可选增强**：高安全场景的医生可在 Settings 里 opt-in 锁屏（TouchID / PIN），但默认关闭。
5. **可回滚**：每一步行为都要可撤销 —— 误删身份能恢复，切错账户能切回。

---

## 4. 身份模型与数据 schema

### 4.1 身份是什么

一个 **identity** = 一个本地数字孪生，由以下原子构成：

- `user_id`: UUID v4，全机唯一，对外的稳定 ID。
- `display_name`: 医生可读名（默认 "Doctor"，可改）。
- `avatar_emoji`: 可选 emoji，单字符（默认 🩺）。
- `created_at`: ISO 8601。
- `last_active_at`: ISO 8601，每次成功登陆时更新（用于 picker 排序）。

存储于两处，分工明确：

```
~/Library/Application Support/RuneProtocol/identity.json
  └─ 本机所有身份的目录 + 当前活跃身份指针
     {
       "schema_version": 2,
       "active_user_id": "uuid-...",
       "identities": [
         {
           "user_id": "uuid-A",
           "display_name": "Dr. Wang",
           "avatar_emoji": "🩺",
           "created_at": "2026-06-27T08:00:00Z",
           "last_active_at": "2026-06-27T14:23:00Z"
         },
         {
           "user_id": "uuid-B",
           ...
         }
       ]
     }

~/Library/Application Support/Nexus/rune_server.db
  └─ users 表(权威源)
     id, display_name, jwt_secret, created_at, updated_at, avatar_emoji
```

### 4.2 两个文件谁是权威？

- **`users` 表是权威**。jwt_secret、签发凭据、所有跨表 FK 都在 DB 里。
- **identity.json 是 picker 索引**。它告诉桌面 app "这台机器上有谁、上次用的是谁"——本质是一份 UI 列表的本地缓存。
- 二者不一致时（DB 没了 / 文件损坏 / 用户手改文件）：
  - 文件指向 DB 不存在的 user_id → 静默清掉那条 identity，从 picker 移除
  - DB 有的 user 但文件没列出 → 不主动 import（避免与已删除身份混淆），但提供 Settings 里的"导入孤儿身份"按钮兜底

### 4.3 schema_version 升级路径

| 版本 | 改动 |
|---|---|
| 1 | 单身份 `{user_id, created_at}` — F24 落地的版本 |
| 2 | 多身份列表 + active 指针 — 本设计目标版本 |
| 3 | + `device_id` + 可选 `sync` 段 — §11 跨设备同步上线时 |

迁移：app 启动时检查 `schema_version`。如果是 1，把单 user_id 包成 identities 数组的第 0 项，写回。idempotent。

### 4.4 identity.json 损坏/丢失的恢复路径

**核心原则**：identity.json 是 picker 索引，`users` 表（在 `rune_server.db` 里）才是权威源。两者不一致时**永远以 DB 为准**，identity.json 可以**完全从 DB 重建**。这意味着删了 identity.json 或者文件 corrupt 都**不丢任何数据**。

#### 4.4.1 触发路径

启动时 `POST /auth/local-bootstrap` 检查 identity.json 状态：

```
读 identity.json
    │
    ├─ 读成功 + JSON 合法 + schema_version 已知
    │     → §6.1 正常路径
    │
    ├─ 文件不存在
    │     ↓
    ├─ 文件存在但 JSON parse 失败 (truncated / encoding / 0 bytes)
    │     ↓
    ├─ schema_version 未知（未来回滚场景）
    │     ↓
    └─ active_user_id 不在 identities[] 里
          ↓
       → 全部走 4.4.2 重建路径
```

#### 4.4.2 重建算法

```python
# 伪代码 —— packages/server/nexus_server/auth/routes.py 实施
def rebuild_identity_from_db(rune_home: Path) -> IdentityFile:
    # 1. 把原文件备份，方便 forensics
    if (rune_home / "identity.json").exists():
        rotate_backup(rune_home / "identity.json")  # → identity.json.bak.{ts}

    # 2. 扫 users 表，把所有"未软删"的行拉出来
    rows = db.execute(
        "SELECT id, display_name, avatar_emoji, created_at, updated_at "
        "FROM users WHERE deleted_at IS NULL "
        "ORDER BY updated_at DESC"
    ).fetchall()

    if not rows:
        # 4.4.3 路径 —— DB 真空,只能真创建一个新身份
        return fresh_bootstrap_new_identity()

    identities = [Identity(
        user_id=r.id,
        display_name=r.display_name or "Doctor",
        avatar_emoji=r.avatar_emoji or "🩺",
        created_at=r.created_at,
        last_active_at=r.updated_at,
    ) for r in rows]

    # 3. 默认活跃 = 上次 updated_at 最晚的那个
    active = identities[0].user_id

    # 4. 原子写回
    write_atomic(rune_home / "identity.json", {
        "schema_version": CURRENT_SCHEMA,
        "active_user_id": active,
        "identities": [asdict(i) for i in identities],
        "_recovered_from_users_table_at": now_iso(),
    })

    log.warning(
        "identity.json was corrupt/missing — rebuilt %d identities "
        "from users table; active=%s",
        len(identities), active[:8],
    )
    return read_identity_file(rune_home)
```

#### 4.4.3 唯一不可恢复路径

**只有** identity.json 损坏 **AND** `users` 表为空（全删了 / DB 重置 / 全新装机）时才会真创建新身份。这是真正"两边都没数据"的情况，没有可恢复源。

#### 4.4.4 防御性写入策略

每次写 identity.json 时：

1. **轮转备份**：写之前把当前文件 rename 成 `identity.json.bak.<unix_ts>`，保留最近 3 份（更老的 GC）。
2. **原子写**：`write(tempfile) → fsync → rename(tempfile, target)`。崩在中间永远不会留下半张文件。
3. **权限收紧**：`chmod 0600`，避免别的进程读写。
4. **schema_version 校验**：写之前 assert 自己的 schema 是已知的，避免代码 bug 写出未来版本。

#### 4.4.5 UI 表达

如果触发了 4.4.2 重建，下一帧 workspace 顶部弹一条非阻塞 banner：

```
ℹ Nexus 检测到 identity.json 异常（已从备份重建）。
  恢复了 N 个身份。如果发现有遗漏，请联系支持并附上：
  ~/Library/Application Support/RuneProtocol/identity.json.bak.<ts>
```

医生该看到的是"我的数据全部回来了"，而不是"我得重新开始"。

---

## 5. API 设计

### 5.1 新增/修改的端点

| Method | Path | 用途 | Auth |
|---|---|---|---|
| `POST` | `/api/v1/auth/local-bootstrap` | 静默登陆当前活跃身份；首次启动若无 identity.json 则自动创建一个 | 无 |
| `GET`  | `/api/v1/auth/identities` | 列出本机所有身份（picker 用） | 无 |
| `POST` | `/api/v1/auth/identities` | 新增一个身份（用于"添加账号"） | 无 |
| `POST` | `/api/v1/auth/identities/{user_id}/activate` | 切换活跃身份；返回新 JWT | 无 |
| `PATCH`| `/api/v1/auth/identities/{user_id}` | 改 display_name / avatar_emoji | JWT（仅本身份） |
| `DELETE`| `/api/v1/auth/identities/{user_id}` | 从 picker 移除身份（DB 数据保留为 orphan，可恢复） | JWT（仅本身份） |
| `POST` | `/api/v1/auth/wipe?user_id=...` | 真删 — DB 行 + 所有 user-scoped 投影表全清。**需二次确认 token** | JWT（仅本身份） |

### 5.2 为什么这些都不需要 auth

因为它们的攻击面就是"本机已经登陆 macOS 的进程"。要打这些端口，攻击者已经在你机器上了——他们能读 identity.json 直接拷走 user_id，端点防御没意义。所以这些 endpoint 只做**正确性校验**（user_id 格式、是否在 DB 里），不做**身份证明**。

唯一例外：PATCH / DELETE / WIPE 需要"操作的身份就是目前活跃身份"——通过 JWT 验证。这防的不是攻击，是 UI bug 把别人身份的数据误改。

### 5.3 sidecar 监听地址

继续绑 `localhost:8001`（F19）。loopback only，外部不可达。

---

## 6. UI 流程

### 6.1 启动决策树

```
App 启动
    │
    ▼
BootGate: 等 sidecar /healthz 200
    │
    ▼
POST /auth/local-bootstrap (10 s deadline + 12 s 兜底)
    │
    ├─ 返回 identities=[A], active=A     →  静默进入 workspace, MainShell(用户A)
    ├─ 返回 identities=[A,B,C], active=A →  静默进入 workspace, MainShell(用户A)
    │                                       右上角头像可点开 picker 切换
    ├─ 返回 is_new_account=true          →  workspace + 一次性 welcome toast
    └─ 后端不可达 / 超时                  →  LoginView (人工兜底)
```

**没有传统意义的"登陆页"。** 默认路径下医生连选身份都不需要——上次用谁，这次就是谁。

### 6.2 身份切换 picker（在头像/avatar 上）

```
┌──────────────────────────────┐
│  🩺 Dr. Wang   ✓ 当前活跃    │
│  🩺 Dr. Chen    最近 3 天前  │
│  🩺 Dr. Liu     最近 12 天前 │
│  ──────────────────────────  │
│  + 添加新身份                │
│  ⚙ 管理身份…                 │
└──────────────────────────────┘
```

点一个身份 → 调 `/identities/{user_id}/activate` → 拿到新 JWT → 整个 workspace 重新挂载（清空 zustand state、按新 user_id 重拉 patients/sessions）。

### 6.3 添加新身份

只需输入 `display_name`（必填）。立即生效，新身份的患者列表是空的。没有密码、没有验证码、没有 OAuth 跳转。

UI 上要明确告诉用户："这是本机的一个新工作 profile。它和你的医院账号、PubMed 账号都没有关系。"

### 6.4 管理身份（Settings 子页）

| 操作 | 影响 |
|---|---|
| 改名 / 改 emoji | 只改 display_name / avatar_emoji，patient 数据不动 |
| 删除身份 | 从 picker 隐藏；DB 数据保留 90 天，期间可"恢复"；90 天后 GC job 清除 |
| 立即清空数据 | 调用 `/wipe` — DB 行 + 所有 user-scoped 投影立即删；需要二次确认 |
| 导出 | 走现有 `/export/bundle` （只导本身份）|

### 6.5 LoginView 还在吗？

**保留，但默认隐藏**。出现条件：

1. autoLogin 失败（sidecar 不可达）—— 显示诊断面板 + retry
2. 用户主动选择"切换到别的身份"——picker 是入口
3. 高级用户在 Settings 里开了"启动时要求 passkey 解锁"（见下一节）

LoginView 里还保留 display-name + passkey 两条路径，作为 escape hatch。

---

## 7. 可选安全层（默认全部关闭）

这一节是**给在意 PHI 的医生准备的 opt-in 选项**。默认不启用，Settings → 安全 子页可以打开。

### 7.1 启动锁屏（"Require unlock on launch"）

打开后，每次启动 app 时强制走以下其一：

- **TouchID / Apple Watch** — 通过 `LocalAuthentication.framework`，Tauri 命令包装；只用于本地解锁，不向后端发任何东西，不是 WebAuthn。
- **本地 PIN**：4 位数，存 PBKDF2 哈希在 identity 记录里。
- **跳过（默认）**

打开锁屏不改变身份模型，只是在 BootGate 之后插入一个验证步骤。失败 5 次后强制清空当前身份的 JWT 缓存 + 提示从 LoginView 重来。

### 7.2 自动锁屏（"Lock after N minutes idle"）

打开后，没有 UI 操作超过 N 分钟（默认 15 分钟）就把工作区灰掉、要求重新解锁。沿用 7.1 的方式。

### 7.3 切换身份验证

打开后，picker 里点别的身份会要求验证（防止"医生 A 转身泡咖啡，医生 B 顺手切到 A 的 profile 看 A 的患者"）。

---

## 8. 多身份共存的隔离保证

### 8.1 后端层

所有 user-scoped 表已经 `WHERE user_id = ?`（已审计：clinical_graph_nodes、practitioner_*、chat_takeaways、patients、uploads、sessions 等）。本设计不改这些；只确保切换身份后 JWT 里的 user_id 不同，自然走到不同的数据集。

### 8.2 前端层

切换身份会触发：

1. zustand store 全 reset（`useAppState.reset()`）
2. JWT 替换
3. 当前打开的 patient/study/session 强制清空
4. WebSocket / SSE 连接强制重建（避免上一身份的 stream 把消息推到新身份的 UI 上）
5. Window title / dock badge 更新为新 display_name

### 8.3 事件日志层

`twin_event_log` 表本身按 user_id 分区（每行带 user_id 列）。所有 emit_and_apply 调用都带 user_id 参数。**不存在跨身份的事件**。

---

## 9. 从历史方案的迁移

| 历史方案 | 状态 | 迁移动作 |
|---|---|---|
| WebAuthn passkey (`passkey_credential` 列) | 移除 | 列在下一次 schema 迁移里 DROP；新身份不创建；旧值由迁移脚本清掉 |
| OS Keychain (F22 keyring crate) | 已删 | F24 已经 strip 干净 |
| localStorage user_id | 兼容兜底 | autoLogin 不依赖；LoginView 手动路径仍读 |
| identity.json schema v1 | 自动迁移 | 启动检查 → v2 |
| display_name "Doctor" 默认 | 保留 | 仍是首次 bootstrap 的默认 |

### 旧用户首次升级到本设计

```
启动 → /local-bootstrap
    ├─ 见 identity.json schema_version=1
    ├─ 读出单个 user_id
    ├─ 改写为 schema_version=2，identities=[那个user]，active=那个user
    └─ 正常返回 JWT
```

完全静默，老用户无感。

---

## 10. Managed Mode — 商业化路径

> **关键不变量**：PHI **永远不离开** 医生本机。Managed Mode 引入的是一个**控制面**（仅账号元数据 + license + 审计），不是数据面。

### 10.1 用例

| 客户类型 | 例子 | 规模 | 关键诉求 |
|---|---|---|---|
| 单 PI 实验室 | 一位研究员买个 license 给自己用 | 1 seat | 跟 Local Mode 一样体验 |
| 中型研究中心 | 主任给组里 10 位医生发账号 | 5-30 seats | 一处发邀请、一处吊销、看用了几次 |
| 医院科室 | 肿瘤科 IT 统一部署 | 30-200 seats | SSO（OIDC）、SCIM 自动配置、详细审计 |

我们在 v0.2 ship **单 PI + 中型研究中心**（不需要 SSO/SCIM）。v0.5+ 才考虑 enterprise SSO。

### 10.2 部署模式 (Deployment Modes)

**同一份 dmg**，首次启动在 BootGate 之后多一个分支：

```
首次启动 (无 identity.json)
    │
    ▼
"你是个人使用还是机构邀请？"  ←─ 唯一的一次性选择
    │
    ├─ 个人使用       → §6.1 流程，本地零摩擦
    │   (Local Mode)
    │
    └─ 我有激活码     → §10.4 流程，输码 → 联系控制面 → 拿身份
        (Managed Mode)
```

激活之后**模式写入 identity.json 的 `mode` 字段**，之后启动不再问。

```jsonc
{
  "schema_version": 3,
  "mode": "local" | "managed",        // ← v0.2 新增
  "active_user_id": "uuid-...",
  "managed": {                         // ← 仅 mode=managed 时存在
    "control_plane_url": "https://api.runeprotocol.ai",
    "org_id": "uuid-org-...",
    "org_name": "Beijing Cancer Hospital · 胸外科",
    "license_token": "<JWT signed by control plane>",
    "license_token_expires_at": "2027-06-27T00:00:00Z",
    "last_phone_home_at": "2026-06-27T14:00:00Z",
    "machine_fingerprint": "<sha256(...)>"
  },
  "identities": [...]
}
```

### 10.3 控制面 (Control Plane) 是个独立服务

完全独立于本地 sidecar，部署在我们自己运营的云上（建议 Fly.io 或 AWS，跟现有 `nexus-email-relay` 同一架构）。

```
                  ┌─────────────────────────────────────────┐
                  │  Control Plane @ api.runeprotocol.ai    │
                  │  (Fly.io · Postgres · Stripe webhook)   │
                  │                                          │
                  │   ╔═══════════════════════════╗         │
                  │   ║  Org table                ║         │
                  │   ║  Doctor invitations       ║         │
                  │   ║  License seats            ║         │
                  │   ║  Audit log               ║         │
                  │   ║  Usage counters           ║         │
                  │   ║  (NO PHI, NO chat text)   ║         │
                  │   ╚═══════════════════════════╝         │
                  │                                          │
                  │  HTTPS endpoints                         │
                  │  + Admin Web Dashboard                   │
                  └─────────────┬───────────────────────────┘
                                │
                  ┌─────────────┴───────────────────────────┐
                  │   POST /api/v1/license/activate         │
                  │   POST /api/v1/license/phone-home       │
                  │   POST /api/v1/audit/event              │
                  └─────────────┬───────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        │                       │                       │
        ▼                       ▼                       ▼
  ┌───────────┐           ┌───────────┐           ┌───────────┐
  │ Dr. Wang  │           │ Dr. Chen  │           │ Dr. Liu   │
  │ 's Mac    │           │ 's Mac    │           │ 's Mac    │
  │           │           │           │           │           │
  │ Nexus.app │           │ Nexus.app │           │ Nexus.app │
  │ + sidecar │           │ + sidecar │           │ + sidecar │
  │ + LOCAL   │           │ + LOCAL   │           │ + LOCAL   │
  │ DB (PHI)  │           │ DB (PHI)  │           │ DB (PHI)  │
  └───────────┘           └───────────┘           └───────────┘

       PHI 永远只在虚线下方 ────────────────────────────────
```

控制面**不接触**临床数据；它只知道：

- 这个机构有多少医生
- 每个医生上一次"打卡"是什么时候
- 总的 chat turns 数 / 患者数 / 研究数（计数器，纯数字，不含内容）
- 谁邀请了谁、谁吊销了谁

### 10.4 邀请 / 激活流程

```
[超级管理员浏览器]                   [医生 Mac]
       │
       │  ① 在 admin dashboard 输入医生邮箱
       │     dr.wang@hospital.com + 角色
       ▼
   生成 8 位激活码 (e.g. NXJC-9K3M)
   + 邮件: "Dr. Wang, 您的 Nexus 激活码: NXJC-9K3M
            一次性使用 · 14 天有效"
       │
       │                              ① 医生打开 Nexus.app (首次)
       │                              ② 选 "我有激活码"
       │                              ③ 输入 NXJC-9K3M
       │                              ④ Nexus 把 (code, machine_fp)
       │                                 POST 到控制面 /license/activate
       │  ⑤ 验证 code 有效 + 未过期 + 未用过
       │     生成 license_token (JWT, 1y 有效, 绑 machine_fp)
       │     在 Postgres 写一行 user
       │     标记 code 为 redeemed
       │     返回 {license_token, org_info, user_id}
       │                              ⑥ Nexus 写 identity.json
       │                                 + 创建本地 user 行
       │                                 + 进入 workspace ✓
       ▼                              ▼
   admin 看到 "Dr. Wang"               医生看到自己的工作区
   状态变成 "已激活"                    跟 Local Mode 体感一样
```

激活后控制面跟本机的交互只剩 §10.6 的"心跳"。

### 10.5 远程吊销

管理员在 dashboard 点 "撤销 Dr. Wang"：

1. 控制面把该医生的 license token 加入 revocation list（CRL-style）
2. 医生 Mac 下次 phone-home（默认每 24 小时；详见 §10.6 + D3 拍板）发现自己被吊销
3. 本机进入 "license suspended" 状态：
   - **默认**：read-only 模式（保留访问以便导出数据）+ 7 天后真删
   - **可配**：立即 wipe（管理员勾选 "Force immediate wipe"）
4. wipe 流程：删 `identity.json` 中该 user 条目 + 调本机 `/auth/wipe` 清 DB 行 + 所有 user-scoped 投影

医生离线时被吊销：phone-home 不通就维持上次状态。一旦联网立即生效。

最坏情况：医生离线 N 天 + 已被吊销 → 这 N 天他还能用。补丁：license_token 里设本地 TTL（默认 7 天），过期就强制 phone-home 才能继续；UX 上加一行 "上次同步：N 天前 · 工作中"。

**紧急即时吊销路径（不依赖心跳节奏）**：

当 admin 真的需要"现在就切断"（医生当场被发现 PHI 滥用 / 设备遗失等），走带外（out-of-band）路径，不等下一次 24h 心跳：

- Admin Dashboard 上"Force immediate wipe" 按钮
- 控制面下发 push notification（基于 Apple Push Notification Service for macOS / 长连接备份信道）
- 桌面端 SidecarState 收到 push → 立即调本机 `/auth/wipe` → 抹掉 user_id 所有数据 → 立刻跳到 LoginView
- 离线设备会在 push 通道恢复（或 24h 心跳）后兜底执行

也就是说：**心跳 24h 只决定"被动发现自己被吊销"的延迟**，主动紧急切断走 push 不受这个限制。

### 10.6 心跳 / phone-home

```
默认每 24 小时（admin 在 dashboard 可调成 1h-7d），sidecar 后台:
    POST https://api.runeprotocol.ai/api/v1/license/phone-home
    Authorization: Bearer <license_token>
    Body: {
        "version": "0.2.0",
        "user_count": 1,        // 这台机器的 active identities
        "chat_turns_24h": 47,   // 计数器
        "patients_total": 12,
        "studies_total": 2,
        "last_chat_ts": "..."
    }
    
返回:
    {
        "ok": true,
        "license_status": "active" | "revoked" | "expired",
        "force_refresh_token": null | "<new_token>",
        "policy_updates": {
            "max_offline_hours": 168,  // admin 配置的离线上限
            "lock_idle_minutes": null
        }
    }
```

载荷里**只有计数器，没有内容**。没有 patient_hash、没有 chat text、没有 SOAP、没有 user_id 之外的可识别信息。

`policy_updates` 允许管理员**远程下发策略**：要求开启锁屏、最大离线天数、是否允许 X 功能等。但 admin **不能** 远程读 PHI。

### 10.7 数据治理 (Privacy 不变量)

| 数据类别 | 存哪 | 控制面能看到吗 |
|---|---|---|
| 患者姓名 / MRN / 影像 | 本机 DB (Nexus user data dir) | ❌ 永不 |
| Chat 内容 / SOAP / LLM 回答 | 本机 DB | ❌ 永不 |
| Layer 1 临床图谱 / Layer 2 医生模式 / Layer 2b takeaway | 本机 DB | ❌ 永不 |
| 医生姓名 / 邮箱 | 控制面（admin 发邀请时输入） | ✓ |
| license 状态 | 控制面 + 本机 identity.json | ✓ 双向同步 |
| 使用计数器（chat turns、患者数）| 本机生成 → 心跳上报 | ✓ 仅数字 |
| 登陆 / 切换 / 吊销事件 | 本机 → 心跳上报 audit_event | ✓ 仅事件元数据 |
| 用药 / 化验 / 影像具体值 | 本机 DB | ❌ 永不 |

**承诺**：控制面 Postgres schema 里**没有任何字段允许容纳 PHI**。审计：CI 跑一个 grep + schema linter，看到 `patient_*` / `chat_*` / `clinical_*` / `note_*` 列名就 fail build。

### 10.8 控制面 schema 草案 (Postgres)

```sql
-- 机构表
CREATE TABLE organizations (
  id              UUID PRIMARY KEY,
  name            TEXT NOT NULL,
  billing_email   TEXT NOT NULL,
  stripe_cust_id  TEXT,
  plan            TEXT,                  -- 'starter' | 'team' | 'enterprise'
  seat_limit      INT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 角色枚举
CREATE TYPE role AS ENUM ('super_admin', 'admin', 'doctor');

-- 用户表 (仅元数据)
CREATE TABLE managed_users (
  id              UUID PRIMARY KEY,
  org_id          UUID NOT NULL REFERENCES organizations(id),
  email           TEXT NOT NULL,
  display_name    TEXT NOT NULL,
  role            role NOT NULL DEFAULT 'doctor',
  invited_at      TIMESTAMPTZ,
  invited_by      UUID REFERENCES managed_users(id),
  activated_at    TIMESTAMPTZ,
  revoked_at      TIMESTAMPTZ,
  last_phone_home_at TIMESTAMPTZ,
  UNIQUE (org_id, email)
);

-- 激活码（一次性）
CREATE TABLE activation_codes (
  code            TEXT PRIMARY KEY,     -- e.g. 'NXJC-9K3M'
  org_id          UUID NOT NULL REFERENCES organizations(id),
  user_id         UUID NOT NULL REFERENCES managed_users(id),
  expires_at      TIMESTAMPTZ NOT NULL,
  redeemed_at     TIMESTAMPTZ,
  redeemed_machine_fp TEXT
);

-- License token CRL
CREATE TABLE revoked_tokens (
  jti             UUID PRIMARY KEY,
  revoked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  reason          TEXT
);

-- 使用计数器（per-user 每日 1 行）
CREATE TABLE usage_daily (
  user_id         UUID NOT NULL REFERENCES managed_users(id),
  day             DATE NOT NULL,
  chat_turns      INT NOT NULL DEFAULT 0,
  patients_total  INT NOT NULL DEFAULT 0,
  studies_total   INT NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, day)
);

-- 审计事件 (login / activate / revoke / wipe)
CREATE TABLE audit_events (
  id              UUID PRIMARY KEY,
  org_id          UUID NOT NULL REFERENCES organizations(id),
  user_id         UUID REFERENCES managed_users(id),
  actor_user_id   UUID REFERENCES managed_users(id),     -- 谁触发的
  event_kind      TEXT NOT NULL,                         -- 'activate' | 'revoke' | ...
  metadata_json   JSONB NOT NULL DEFAULT '{}',           -- IP, machine_fp, etc.
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**注意没有任何 PHI 字段**。 `metadata_json` 也只允许下列 key：`ip_country` / `machine_fp_prefix` / `app_version` / `reason`。CI lint 检查 audit_event 写入处的 payload。

### 10.9 超级管理员的 UI（独立 web app）

```
admin.runeprotocol.ai  ←─ 一个完全独立的 React app, 不在 Nexus.app 里

  Sidebar:                       Main:
  ─────────────                  ─────────────────────────────
  · Members                       Members
  · Invitations                   ┌─────────────────────────────┐
  · License & Billing             │ 邮箱            状态  最后活跃  │
  · Audit Log                     │ wang@hosp.com  ✓ 活跃 2h 前    │
  · Settings                      │ chen@hosp.com  ✓ 活跃 12d 前   │
                                  │ liu@hosp.com   ⏸ 已暂停        │
                                  │ + 邀请新成员                   │
                                  └─────────────────────────────┘
  
  Audit log 视图:
  ─────────────────────────────────────────────────
  时间        操作者        事件      对象          
  14:23:01    wang@hosp.    activate  自己          
  09:15:02    super-admin   invite    new@hosp.com  
  ...
```

权限分级：

| 角色 | 邀请 | 吊销 | 看 audit | 改 billing | 看 PHI |
|---|---|---|---|---|---|
| super_admin | ✓ | ✓ | ✓ | ✓ | **永远不能** |
| admin | ✓ | ✓ | ✓ | ❌ | **永远不能** |
| doctor | ❌ | ❌ | 自己的事件 | ❌ | 只能在自己的 Mac 上看自己的 |

### 10.10 离线 vs 在线 (degradation matrix)

| 状态 | 本机表现 |
|---|---|
| 已激活 + 在线 + license 有效 | 完全正常 |
| 已激活 + 离线 + 上次心跳 < `max_offline_hours` | 完全正常 |
| 已激活 + 离线 + 上次心跳超时 | 进入只读模式 + 顶部红条 "需要联网激活" |
| 已激活 + 在线 + license 已吊销 | 7 天宽限只读 → wipe（按 admin 配置）|
| 已激活 + 在线 + license 已过期 | 显示 "license 过期，请联系管理员续费"，只读 |
| 未激活 | LoginView 显示，可选 "我有激活码" / "个人本地使用" |

### 10.11 个人使用与 Managed Mode 切换

允许个人用户后续被管理员邀请并"升级"为 managed 身份：

1. 个人本地用了 6 个月，攒了一堆 patient 数据
2. 加入某机构，管理员给他发激活码
3. 启动 Nexus → Settings → "加入机构" → 输码 → 联系控制面
4. **数据 stay where it is**：本机 DB 不动；只是给现有 identity 加上 `org_id` + license_token
5. 之后管理员可在 dashboard 看到这位医生，但**仍然看不到 PHI**

反向（managed → 个人）：licensing 到期后选择不续费，本机自动转为 Local Mode；patient 数据保留。

### 10.12 计费

走 Stripe（`.env` 里已有 `STRIPE_*` 占位）。仅控制面交互；本机不感知钱的事。

- Per-seat 月付 / 年付
- 超 seat 限自动锁邀请（"已用 30/30 seats，请升级套餐"）
- Stripe webhook → 控制面更新 `seat_limit`
- 免费试用：14 天，期满转月付或降级

### 10.13 风险 & 决策点

| 风险 | 缓解 |
|---|---|
| 控制面被攻陷 | 不存 PHI；最坏后果是吊销凭据被滥用 → 医生被踢出 → 数据仍在本机 → admin 可手动恢复 |
| 我们公司倒闭 / 控制面消失 | 本机检测心跳连续 N 天失败 → 提示用户 → 提供"转 Local Mode"路径，license token 当地缓存继续用到自然过期 |
| 国家级 / 区域级 license | 不绑国家；US/EU/CN 都按同一套合同走。但 PHI 留在本机回避了大部分跨境合规问题 |
| 医生希望"从公司退出仍保留 patient 数据" | 离职流程：admin 吊销 → 医生选 "导出 + 转 Local Mode" → DB 保留，license 失效 |

---

## 11. 跨设备同步 (Cross-device Sync)

> **关键不变量（不可妥协）**：同步服务器**只能见密文**。所有 PHI（患者数据、聊天文本、记忆节点）在离开设备前用医生掌握的密钥端到端加密。即使我们的服务器被攻陷，攻击者拿到的也只是无法解密的字节流。

### 11.1 用例

| 代号 | 场景 | 频率 |
|---|---|---|
| **SC-A** | 医生有 2 台 Mac（办公室 + 家），希望两台都能看到同一份患者库 | 高 |
| **SC-B** | 笔记本损坏 → 新机器 → 用同一身份恢复全部历史 | 中（灾难恢复刚需）|
| **SC-C** | 主 Mac + iPad 只读跟踪（未来 iOS 版本） | 未来 |
| **SC-D** | Managed Mode 下机构多个工位，医生随便坐哪一台 | 中（机构买单常见）|

SC-A + SC-B 决定**必须有同步**；SC-C/SC-D 决定**架构必须支持 N 设备**而不只是双机镜像。

### 11.2 设计约束

```
P0 (不可妥协):
  · 服务器不可读 PHI         → E2EE，密钥只在医生设备
  · 离线工作不受影响          → 同步是 background job，不在 critical path
  · 单设备用户零额外摩擦      → 不开同步 = 不需要联网账户
  · 冲突不丢数据              → 所有写入是事件,append-only

P1 (强烈倾向):
  · 共享同步基础设施和 Managed 控制面，节省运维
  · 同步过程能审计 / 可暂停 / 可清空云端副本
  · 用户能从云端"清账号"完整退出
```

### 11.3 架构选择 — Event Log 复制

我们的所有数据已经是 event-sourced 的。`twin_event_log` 是 append-only 的事件流，所有 projection（clinical_graph_nodes、patient 表、practitioner_facts 等）都从它派生。

**这是为跨设备同步量身定做的架构**：

```
设备 A 的 event_log:          设备 B 的 event_log:
  e1 USER_MESSAGE              e1 USER_MESSAGE              ← 同步过来
  e2 ASSISTANT_RESPONSE        e2 ASSISTANT_RESPONSE         
  e3 NODE_ADDED                e3 NODE_ADDED
  e4 STUDY_CREATED              e4 STUDY_CREATED
  e5 USER_MESSAGE  (本地新增)   e5* USER_MESSAGE (B 自己新增)
                          ↓
                     冲突合并
                     ↓
  e1, e2, e3, e4, e5 (from A), e5* (from B, renumbered e6)
```

**同步 = 把双方 event_log 互相增量灌过去 + replay projection**。

唯一新增的概念：每个 event 带 `device_id` 字段。冲突合并时 `(device_id, local_event_idx)` 联合排序，得到全局顺序。冲突归并后所有 projection 在两台机器上**逐字节相同**。这是 event-sourcing 的免费午餐。

### 11.4 端到端加密设计

**密钥派生**:

```
医生在首次开启同步时设置一个 "Cloud passphrase"
            │
            ▼
  argon2id(passphrase, salt) → 32-byte master_key (KEK)
            │
            ├──→ derive_key(KEK, "events.v1")   → AES-256-GCM key for event payload
            ├──→ derive_key(KEK, "files.v1")    → AES-256-GCM key for file blobs (uploads)
            └──→ derive_key(KEK, "metadata.v1") → HMAC key for searchable hashes
```

passphrase **永不离开设备**。服务器只见从 derived key 派生的 anonymous user identifier 和 ciphertext。

**事件加密格式**:

```
plaintext event JSON  →  zlib compress  →  AES-256-GCM(nonce, payload_key) → ciphertext + auth_tag

cloud blob = {
    user_id_hmac:  HMAC(master_key, "user_id"),  // 服务器分桶用
    device_id:     UUID,                          // 明文（同步顺序需要）
    event_idx:     int,                           // 明文
    ts_unix_ms:    int,                           // 明文（粒度到秒）
    kind_hmac:     HMAC(key, event_kind),         // 明文不行（暴露行为模式）→ HMAC
    ciphertext:    bytes,                         // 加密后的 payload
    nonce:         12 bytes,
    auth_tag:      16 bytes,
}
```

只有 `device_id`、`event_idx`、`ts` 是明文（为冲突排序服务）；payload 和 kind 都不可读。服务器即便配合监管也只能交出"哪些设备在哪天写了多少事件"，无法交出具体内容。

**密钥恢复**：

提供两条路径，医生在开启同步时二选一（或都选）：

1. **Recovery phrase**（推荐）：BIP39 24-word phrase 派生 KEK。医生抄写下来锁保险柜。忘记 passphrase 时用它恢复。
2. **Account-level passphrase**：邮箱 + 设备验证码登陆云账号；passphrase 即 KEK。忘了 = 数据永久不可解（云端是密文，我们没法帮忙）。

我们**强烈推荐** Recovery phrase，并在 UI 里硬塞引导（不抄下来不让继续）。

### 11.5 设备模型 (device vs identity)

**关键区分**:

| 概念 | 范围 | 主键 |
|---|---|---|
| **identity** (user_id) | 这位医生 | UUID, 跨设备共享 |
| **device** (device_id) | 这位医生的某一台 Mac | UUID, 设备本地生成 |
| **cloud_account** | 云端账号 | 通常 1:1 跟 identity 绑 |

身份是**跨设备的**——同一个 user_id 出现在 3 台 Mac 上是常态。设备是**每机一份**——办公室 Mac 和家里 Mac 是同 identity 但不同 device_id。冲突归并按 `(device_id, event_idx)` 排序去重。

`identity.json` schema v3 需要新增 `device_id` 字段（每台机器首次启动时生成，永不变）。

```jsonc
{
  "schema_version": 3,
  "mode": "local" | "managed",
  "device_id": "uuid-this-mac-...",   // ★ v3 新增
  "sync": {                            // ★ v3 新增 (opt-in)
    "enabled": true,
    "cloud_account_email": "wang@example.com",
    "user_id_hmac_prefix": "abc123...",  // 用于服务器分桶（不可逆）
    "last_pulled_at": "2026-06-27T14:00:00Z",
    "last_pushed_event_idx": 4823
  },
  "active_user_id": "uuid-...",
  "identities": [...]
}
```

### 11.6 冲突解决

凭事件日志的 append-only 属性，**绝大多数"冲突"自然消失**：

- 两台机器同一秒创建患者 → 两个不同 user_id 的 PATIENT_REGISTERED 事件 → 都保留，得到两个患者
- 两台机器同时更新同一个 finding → 两条 NODE_UPDATED 事件 → 后一个覆盖前一个（按 `(ts, device_id)` 排序）
- 同一台机器先创建再删除 → 自然顺序

唯一需要特殊处理的情况：**Layer 2 takeaway 去重**——两台设备各自蒸馏出"医生倾向先做 NGS"，会产生重复行。在 sync replay 后跑一次 Jaccard 去重（F11 的逻辑），保留较早一条，删掉重复。

**用户感知的冲突**：
- 把患者档案改了同名字 → "你的两台设备都改了张三 → 王二，自动取最新的那个，你能在 audit 里看到改名历史"
- 真正破坏性的冲突极少；event-sourcing 让这条几乎不存在

### 11.7 灾难恢复 (SC-B)

```
笔记本爆掉 → 买新 Mac → 装 Nexus → 打开 Settings → 启用同步 → 输入 cloud email
    → 邮箱验证码 + 输入 Recovery phrase
    → derived key 解密云端 event log
    → 在本机 replay 全部事件
    → 1-5 分钟后所有患者 / 记忆 / 设置回来
```

**这是云同步对用户最有说服力的卖点**——比"两台同时用"更普遍。市场上每个用过 1Password / Bear / Notion 的医生都熟悉这个流程。

### 11.8 与 Managed Mode 的关系

| 维度 | Local + Sync | Managed + Sync |
|---|---|---|
| 谁付云费 | 医生自掏（per-user $5/mo） | 包含在 license seat 价里 |
| 云端跑哪 | api.runeprotocol.ai | 同上，复用基础设施 |
| 加密密钥 | 医生 passphrase 派生 | 同上（机构**也不能**读医生的 PHI）|
| Admin 看得到吗 | N/A | **不能**——加密层在 admin 之下 |
| 离职后云端数据 | 用户自删 | admin 触发"revoke + wipe cloud" 命令 → 14 天后真删 |

**关键确认**：Managed Mode 的 admin 即使开启了 cloud sync，也**绝对**无法读到医生的 PHI。Admin 能看到的只有"Dr. Wang 的 cloud 同步开着 / 上次同步 N 小时前 / 占了 X MB 配额"——加密层架构上就在 admin 之下。

### 11.9 实施路径

| 阶段 | 范围 | 大致估时 |
|---|---|---|
| **F30.1** | identity.json schema v3 + device_id 引入 + event 表 device_id 列 | 1 day |
| **F30.2** | Cloud sync server：blob 接收 + 按 user_id_hmac 分桶 + 增量取 | 2 day |
| **F30.3** | 客户端 sync engine：argon2id + AES-256-GCM + push/pull cycle | 3 day |
| **F30.4** | 冲突 replay + Layer 2 去重 + 新设备初始化 | 2 day |
| **F30.5** | UI：Settings 启用 sync + Recovery phrase 引导 + audit | 1.5 day |
| **F30.6** | 灾难恢复测试 + 大数据量性能 + 端到端 pentest | 2 day |
| **F30.7** | Managed Mode 集成：admin 看 sync status（不解密）+ revoke wipe | 1.5 day |

总计 ~13 day。建议排期在 v0.5（Managed Mode 出私测之后），让产品先稳定再加同步。

### 11.10 不在 v1.0 但已写下来留作迭代

- **iPad / iPhone read-only**：sync engine 跨平台理论可行，但 Swift port 是另一个项目
- **多 identity 选择性同步**：现在的设计是"该 identity 的所有数据同步"。未来可能想"只同步研究 A 不同步研究 B"，复杂度高，先不做
- **Family Sharing-style** "同事临时借用我的设备但不见我的数据"：超出本设计范围

---

## 12. 不做的事（明确写下来）

不写下来，下次又会有人想做。区分 Local Mode 永远不做 vs Managed Mode 在做。

### 12.1 Local Mode 永远不做

| 不做 | 为什么 |
|---|---|
| 邮箱 + 密码注册（用于本地登陆）| 临床工具不该让医生再记一个密码。本地工具没必要。云同步是 opt-in 例外，见 §11 |
| Magic link / 短信验证（用于本地登陆）| 没有远端服务发，且对本地登陆毫无价值。云同步另说 |
| 强制密码复杂度 / 90 天轮换 | 没有强制密码 |
| 二次/多次 MFA | 同上 |
| WebAuthn 作为强制登陆方式 | 浏览器实现拒绝 IP literal RP，Tauri WebviewWindow 行为不稳定。F19 后只作为可选锁屏 |

### 12.2 Managed Mode 也不做（本设计 v1.0 不做，未来再说）

| 不做（v1.0）| 何时考虑 |
|---|---|
| LDAP / SAML / OIDC SSO | v0.5 enterprise tier 才做 |
| SCIM 自动批量配置 | v0.5+ |
| 跨机构数据 federation | 不在产品路线图 |
| 控制面存任何 PHI | **永不**——违反本设计 §10.7 不变量 |
| 控制面读医生本机的数据 | **永不**——同上 |
| 远程屏幕共享 / IT 远程协助 | 用户拒绝；用第三方 ScreenShare 工具 |

### 12.3 永远不做（无论模式）

| 不做 | 为什么 |
|---|---|
| 把 PHI 写入控制面 / 任何远端 | 设计的核心不变量 |
| 把 LLM 调用代理到我们的服务器 | 医生自己的 key 直接打 OpenAI/Gemini/Anthropic；我们不接触 prompt 内容 |
| 把医生的搜索查询发回服务器 | 同上，Tavily 等也直接打 |
| Telemetry 包含 free-text 用户输入 | 心跳只发计数器，不发任何字段值 |

---

## 13. 验收与回滚

### 13.1 验收清单

| # | 场景 | 期望结果 |
|---|---|---|
| 1 | 全新 Mac，全新 dmg | 启动后直接进 workspace，零弹窗，toast 一次"Welcome" |
| 2 | 同一 Mac 再次启动 | 静默进入，无 toast |
| 3 | 重装 dmg | 数据全在，user_id 不变 |
| 4 | 添加第二个身份 → 切换 → 切回 | patient 列表跟着身份切换 |
| 5 | 删除当前身份 | 回到首身份；被删的不再出现在 picker |
| 6 | sidecar kill -9 | LoginView 出现 + 诊断面板 |
| 7 | identity.json 损坏 / 被误删 | 从 `users` 表（权威源）重建：扫所有未删除的 user 行 → 写回 identity.json → 选最近活跃的作为 active_user_id；老数据全部保留可见。详见 §4.4 |
| 8 | identity.json 指向不存在的 user_id | 自动清掉那条 stale 条目；其余有效身份保留 |
| 8a | 同时 identity.json 损坏 + `users` 表为空 | （只有这种情况）真创建新身份。这是唯一不可恢复路径 |
| 9 | 打开锁屏 → TouchID | 解锁成功后进 workspace |
| 10 | 锁屏失败 5 次 | 强制走 LoginView |

### 13.2 回滚

如果本设计在生产出 bug：

- **回滚到 F24 单身份**：`schema_version=2 → 1` 的反向迁移留在代码里，启动时如果检测到回滚配置就只用 identities[0]。前端 picker 隐藏。
- **回滚到 F23 + LoginView**：删除 `/local-bootstrap` 调用，App.tsx 恢复显示 LoginView。
- 所有 user_id 不变，DB 数据全在。

### 13.3 灰度策略

不做远程 feature flag（这是本地 app）。靠 dmg 版本号控制：v0.2 ship 多身份，v0.3 ship 可选锁屏。每个版本 1 周观察期，确认零回归再发下一个。

---

## 14. 实施顺序

按发布阶段拆分。**v0.2 ship Local Mode 多身份**；v0.3+ 才碰 Managed Mode；v0.5 上跨设备同步。

### 14.1 v0.2 — Local Mode 多身份 (~3.5 day)

| Phase | 范围 | 估时 |
|---|---|---|
| **F26.1** | 后端: schema_version=2 + `/identities`, `/activate`, `/identities/{id}` GET/PATCH/DELETE | 0.5 day |
| **F26.2** | 前端: picker UI on avatar + 切换流程 + zustand reset | 0.5 day |
| **F26.3** | Settings: 改名 / 改 emoji / 管理身份 + 软删除 90 天 | 0.5 day |
| **F26.4** | (可选) 锁屏: TouchID + PIN + idle lock | 1 day |
| **F26.5** | 文档 + 验收清单全过 + dmg ship | 0.5 day |

### 14.2 v0.3 — Managed Mode 基础设施 (~10 day)

| Phase | 范围 | 估时 |
|---|---|---|
| **F27.1** | 控制面新仓库 + Postgres schema + FastAPI scaffold | 1 day |
| **F27.2** | `/license/activate` + 激活码生成 + 邮件 | 1.5 day |
| **F27.3** | `/license/phone-home` + revocation list + license JWT | 1.5 day |
| **F27.4** | `/audit/event` ingest + lint 校验"没有 PHI" | 0.5 day |
| **F27.5** | Admin web dashboard (React, 独立 app, simple table-based) | 2 day |
| **F27.6** | Stripe webhook + seat 限制 + 邀请节流 | 1 day |
| **F27.7** | 桌面端: `identity.json` schema v3 + 首次启动选择"个人/激活码" | 1 day |
| **F27.8** | 桌面端: 心跳后台 task + license 状态 banner | 0.5 day |
| **F27.9** | 桌面端: 离线降级 + license suspended UI | 0.5 day |
| **F27.10**| 集成测试: 邀请→激活→吊销→wipe 全链路 | 0.5 day |

### 14.3 v0.4 — Managed Mode 商业化 (~5 day)

| Phase | 范围 | 估时 |
|---|---|---|
| **F28.1** | 公开网站 / 营销页 + 注册流（"创建机构"）| 1 day |
| **F28.2** | Stripe 计费完整流（trial → 月付 → 年付 → 升级套餐）| 1.5 day |
| **F28.3** | 控制面：自助管理员 onboarding + 文档 | 1 day |
| **F28.4** | 安全审计：第三方对控制面做一轮 pentest | 1.5 day |

### 14.4 v0.5 — 跨设备同步 (~13 day)

详细 phase 见 §11.9（F30.1 - F30.7）。Local Mode + Managed Mode 都受益。

### 14.5 v0.6+ — Enterprise (按需触发，未排期)

| Phase | 范围 |
|---|---|
| F31.x | SSO (OIDC) — Okta / Azure AD 集成 |
| F31.x | SCIM — 批量自动 provision / deprovision |
| F31.x | BAA / SOC2 / HITRUST 合规审计 |
| F31.x | On-prem 部署模式（控制面 + sync server 跑客户自己机房）|

总累计：Local Mode v0.2 ~3.5 day；Managed Mode v0.3 + v0.4 = ~15 day；跨设备同步 v0.5 ~13 day。**v0.3 是商业化分水岭；v0.5 是用户黏性分水岭**（医生有了 2 台 Mac 之后，迁移成本极高）。

---

## 附录 A — 与 F24 当前实现的差异

F24（已 ship 的当前实现）只支持单身份。本设计在此基础上：

- 扩展 identity.json 为多身份数组
- 新增 5 个 API
- 头像点击行为：原来只显示当前 display_name，现在变成 picker dropdown
- "Settings → 身份"页 新增

不变的部分：
- `$RUNE_HOME/identity.json` 位置不变
- `users` 表 schema 不变（只用既有列）
- 后端 `WHERE user_id = ?` 强制隔离不变
- 重装跨期持久不变

---

## 附录 B — FAQ

**Q: 为什么不让用户用邮箱注册？**
A: 本地临床工具不需要远端账户系统。邮箱注册唯一的好处是跨设备登陆，那是 SaaS 版本的事情。增加注册流程会把每位医生的"打开 → 用"延迟到"打开 → 注册 → 验证邮箱 → 设密码 → 用"，对临床场景是纯减分项。

**Q: 没有密码不会被旁人偷看吗？**
A: 攻击模型见 §2。物理访问已登陆的 Mac 是产品层防不住的——靠 macOS 锁屏 + FileVault。**对在意这一点的用户**，§7 的可选锁屏正好解决：把 app 锁在 TouchID/PIN 后面。

**Q: 多个医生共用一台 Mac，PHI 怎么办？**
A: 不要让多个医生用同一个 macOS 账号 + 同一个 Nexus 身份。要么：(a) 每个医生用自己的 macOS 账号（最强隔离）；(b) 共用 macOS 账号但每个医生用一个 Nexus identity（中等隔离）。文档里 §6.4 会明说。

**Q: 用户想"注销/退出登陆"怎么办？**
A: 没有"注销"概念——本地 app 没有 session 可注销。语义最接近的是"清空当前身份的 JWT 缓存"（让 token 失效，下次启动重新 bootstrap）。提供一个 Settings 按钮。

**Q: 用户想"删号"呢？**
A: §6.4 的"立即清空数据"按钮调 `/wipe`，DB 全清，identity.json 移除。完成。

**Q: 用户想把自己的数据带到另一台 Mac？**
A: 三档方案（按规划顺序）：
  1. **v0.5 之前**：`/export/bundle` 手动导出 + 在新机器导入。manual flow，每周做一次能挡住基本灾难恢复需求。
  2. **v0.5 起**：在 Settings 里 opt-in 启用 §11 跨设备同步。设置 24 字 Recovery phrase，端到端加密，多 Mac 自动一致。云服务器看不到 PHI（架构层不可读）。
  3. **Managed Mode 用户**：同步 quota 含在 license 里，省一道 opt-in。

**Q: 跨设备同步的密钥忘了怎么办？**
A: 这是真实风险。我们提供两层防御：
  · 抄写 Recovery phrase（24 字 BIP39）锁保险柜 —— 推荐
  · 邮箱 magic link 重置 passphrase
  
  最坏情况：两个都忘 → 云端密文永久不可读（我们也帮不上）。本机数据仍在，重新 setup 同步就行。这是 E2EE 的代价：服务器没有后门 = 我们没办法救。

**Q: 公司若被收购或倒闭，云端数据怎么办？**
A: §10.13 + §11 联合应对：
  · license 过期不影响本机数据（永远在本机）
  · 云端密文：本机持续保留 cloud quota 30 天，期间可自助下载备份
  · 30 天后云端密文删除（反正你也是唯一能解的）
  · 本机 + 备份就是你完整的数据，没人能拿走

**Q: 同步会传我所有的患者数据吗？我担心 PHI 上云。**
A: 是 also no。技术上，同步 **传所有 user-scoped 数据**（你的患者全部都在）。但 PHI 含义层面，**云端能看到的只是不可解的字节流**：
  · 加密在你的设备本机进行
  · 密钥从 passphrase 派生，不上传
  · 服务器 schema 里**没有任何 patient_*  / chat_text 列**
  · 这跟 1Password、Signal、Bear notes 一样——服务器 hold 的是密文 blob
  
  唯一能"破解"的方式：我们偷你的 passphrase（不可能，从未离开你的设备）+ 偷你的设备。后者是物理威胁，超出软件可防御范围。

**Q: 我们怎么赚钱？**
A: §10 Managed Mode 是答案。机构买 license seats，per-seat 月付/年付，通过 Stripe 计费。Local Mode 永久免费（作为漏斗顶端）：医生先免费个人用上瘾，再让他们说服科室主任买机构 license。

**Q: 超级管理员能看医生的患者数据吗？**
A: **不能**。控制面没有 PHI 通道——它只见账号元数据 + license 状态 + 计数器。这是 §10.7 列在 schema 里的硬性不变量，CI 会强制校验。这一点对销售很重要——医院不会买一个能让我们看患者数据的工具。

**Q: 那超级管理员到底能做什么？**
A: §10.9 的权限表清晰：邀请新医生、吊销医生、看 audit（"上次登陆时间"、"chat 次数"）、改 billing。**看不到任何临床内容**。

**Q: 医生离职怎么办？**
A: admin 在 dashboard 点 "Revoke"。下次该医生的 Mac 心跳时收到 license_status=revoked。默认 7 天只读宽限期供他导出数据，之后本机 user_id 数据被 wipe。Admin 可勾选 "Force immediate wipe" 跳过宽限。

**Q: 那 7 天的宽限期是不是离职医生还能拿到数据？**
A: 是的，但他**只能拿自己合法接触过的患者**——后端的 `WHERE user_id = ?` 隔离不变。如果医院想"立即切断"，admin 勾 immediate wipe。

**Q: 控制面挂了或者我们公司倒闭了，医生怎么办？**
A: §10.13 的"公司倒闭"行：license_token 缓存在本机，按其自然过期（默认 1 年）继续工作。期间本机会有红条提示"控制面失联 N 天，请联系厂商"。一年后强制只读 → 用户可走 export → Local Mode（导回个人）。**没有数据丢失风险。**

**Q: 如果医院 IT 担心数据出境（中国/欧盟法规）？**
A: 控制面 schema 里**没有 PHI**。出境的只是邮箱地址 + 计数器。绝大多数司法辖区对此不敏感。完全 on-prem 部署（控制面跑客户自己机房）排到 v0.5 enterprise。

**Q: 在中国能不能跑？**
A: 控制面可以部署在境内（阿里云 / 腾讯云），本机数据本来就在本地，所以跨境合规问题最小化。LLM 调用方向上医生 key 直接打 OpenAI/Gemini/智谱/通义，我们不代理。

**Q: 一个医生既是 Local Mode 用户又被某机构邀请，怎么办？**
A: §10.11 的"升级"路径：原有 Local 身份直接被关联到机构的 org_id，patient 数据继续保留。视觉上 picker 里那个 identity 会带 [Beijing Cancer Hospital] 之类的机构标签。如果医生离开机构，identity 退回 Local Mode。

**Q: 一个医生同时属于多个机构（兼职 / 多个研究中心）？**
A: 创建多个 identity，每个绑不同 org_id。picker 里能区分。各机构的数据**绝对**不混（不同 user_id → 不同 DB 分区）。

---

## 附录 C — 安全考量交叉表

| 关注点 | 处理位置 |
|---|---|
| API key 泄露 | F17（dmg 不含真 key）+ F12（mask preview）|
| 跨身份数据污染 | F13（删除级联）+ 本设计 §8 |
| 重装数据丢失 | F3 / F24（identity.json + DB 都在用户数据目录）|
| 钱包私钥泄露 | F17 strip |
| audit trail | 既有 `twin_event_log` + caused_by 链 |
| PHI 不能进 Layer 2 | `event_sourcing/store.py` 已有 PrivacyInvariantViolation 校验 |

---

## 附录 D — 待用户拍板的关键决策

以下决策影响后续实施，需要在 review 阶段定下。每条都有我的推荐 + 理由。

| # | 决策点 | 选项 | 我推荐 | 理由 |
|---|---|---|---|---|
| D1 | Local Mode 多身份默认是否启用 picker | A. 默认启用 / B. 单身份隐藏 picker | **A** | UI 一致性；单身份时 picker 只是缩成"当前医生"标签，零成本 |
| D2 | 删除身份的语义 | A. 软删 90 天 / B. 立即 hard delete | **A** | "我刚误删了 5 年的患者档案"是灾难；90 天 + Settings 里可恢复 |
| D3 | Managed Mode 心跳间隔 | A. 1h / B. 15min / **C. 24h** | **C** (用户拍板) | 24h 对临床工作站的真实节奏更合理:绝大多数医生每天打开 1-2 次,心跳自然搭在 app 启动时,几乎零额外网络/电量成本。Admin 紧急吊销最坏等 24h,如需即时切断走"force wipe"通道(无需依赖心跳节奏)。 |
| D4 | Managed Mode 离线最大天数 | A. 7d / B. 14d / C. admin 自配 | **C** | 不同机构容忍度差异大；做成 admin 在 dashboard 改 |
| D5 | 激活码格式 | A. 8 字符英数 (NXJC-9K3M) / B. 20 字符复杂 / C. magic link URL | **A** | 8 位足够熵（~36^8=2.8万亿），易电话/微信传，跟激活码 14 天 TTL 配合够安全 |
| D6 | 吊销时数据 wipe 默认行为 | A. 7 天宽限只读 / B. 立即 wipe / C. 永不 wipe(由 admin 决定) | **A** | 默认友好；admin 可勾 "Force immediate" 升级 |
| D7 | 控制面部署地区 | A. 仅美东 / B. 美东 + 法兰克福 + 北京 / C. 跟客户走 | 先 **A**，需要再加 | 早期客户少；加地区是几小时的事 |
| D8 | 控制面 vs 桌面端版本兼容 | A. 严格匹配 / B. 桌面端支持 N-2 个老版本 | **B** | 医生不会立即升 dmg；强制升级伤医患关系 |
| D9 | 个人用户能否被多个机构同时邀请 | A. 能 (创建多个 identity) / B. 不能 (1 个 identity 只能 1 个 org) | **A** | §10.11 已支持；多机构兼职是真实场景 |
| D10 | Managed Mode 是否强制开锁屏 | A. admin 可远程下发 policy 强开 / B. 完全本地决定 | **A** | 机构买 license 通常要求合规性；§10.6 的 policy_updates 已支持 |
| D11 | LLM API key 由谁付 | A. 医生自带（BYOK）/ B. 机构付（中转）/ C. 都行 | **A** v0.3; **C** v0.5+ | BYOK 简单，PHI 不经我们；中转涉及代理 LLM 流量，是大改 |
| D12 | 计费货币 | A. USD only / B. USD + RMB + EUR | 先 **A**，按需扩 | Stripe 多币种是配置项，不是工作量大 |
| D13 | 跨设备同步排期 | A. v0.5 单独排（推荐）/ B. 并入 v0.3 Managed / C. 拖到 v0.7+ | **A** | 13 day 工作量；先稳 Managed Mode 现金流再加同步。但不能拖太久——医生 2 台 Mac 是普遍刚需 |
| D14 | 跨设备 Recovery phrase 强制度 | A. 强制（不抄不让继续）/ B. 推荐但可跳过 / C. 只用邮箱 reset | **A** | 跳过的医生忘 passphrase 数据真丢；磨这 30 秒值得。可在 UI 上提供"打印 Recovery card"减少摩擦 |

请在 review 时按 D1-D14 逐项答复（默认全部走我的推荐就只回"按推荐"四个字）。

---

**End of design v1.0**

下一步：

1. 用户 review 本文档 + 答 D1-D14。
2. 通过后开 F26.x（Local Mode 多身份 ~3.5 day）→ ship v0.2。
3. 同期立项 F27.x（Managed Mode 基础设施 ~10 day），可与 F26 并行。
4. v0.3 ship Managed Mode（私测，邀请 1-2 个种子客户）。
5. v0.4 公开商业化。
6. v0.5 ship 跨设备同步 (F30.x ~13 day)——医生黏性分水岭。
7. v0.6+ Enterprise（SSO、SCIM、on-prem，按市场反馈触发）。
