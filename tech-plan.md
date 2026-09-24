# muti_llm 技术方案与任务拆分（含验收测试用例）

| 文档版本 | v1.0 |
| --- | --- |
| 日期 | 2026-09-02 |
| 上游文档 | [requirements.md](./requirements.md) |
| 配套技术栈 | Python/FastAPI + React/Vite/shadcn + SQLite（已确认） |

---

## 0. 完成定义（DoD）—— 先读这一节

**一个任务"完成"的唯一定义：该任务的全部验收用例通过，且不破坏任何已完成任务的验收用例（回归全绿）。**

- 验收用例分三类，编号规则贯穿全文档：
  - `UT-xx-n`：单元测试（backend: pytest；frontend: vitest）
  - `AE-xx-n`：API E2E（backend: pytest，通过 ASGI 直连后端 + 快照回放服务器，不起浏览器）
  - `UE-xx-n`：UI E2E（frontend: Playwright，真实起后端 + 快照回放服务器）
- 例外：标注"基础设施例外"的任务（T01、T03、T20）只需通过其自身所列用例，T01 仅冒烟用例。
- 每个任务的测试必须与功能代码同批提交；`make test` 一键跑全部测试，作为总回归入口。

---

## 1. 技术方案

### 1.1 架构与关键设计决策

```
浏览器(管理UI, Playwright)          客户端(OpenAI SDK / curl)
        │                                  │
        ▼                                  ▼
┌────────────────────── FastAPI 单进程 ──────────────────────┐
│  /v1/chat/completions  /v1/models   ← 对外网关(自Key认证)    │
│  /api/admin/*  (Provider/Model/Pipeline/Trace/Settings)     │
│  /  (静态托管前端 build 产物)                                  │
│────────────────────────────────────────────────────────────│
│  策略引擎 StrategyEngine: council(本期) | 透传 fallback       │
│  适配器: openai_compatible(含Ollama) | anthropic             │
│────────────────────────────────────────────────────────────│
│  SQLite (WAL)  +  Fernet 加密                                │
└────────────────────────────────────────────────────────────┘
```

关键决策（与需求文档的差异/细化）：

| # | 决策 | 理由 |
| --- | --- | --- |
| D1 | **Ollama 不做独立适配器**，归入 `openai_compatible`（Ollama 自带 `/v1` 兼容端点） | 适配器从 3 种减为 2 种，砍掉一块开发与测试量 |
| D2 | **建表用 `Base.metadata.create_all`，不引入 Alembic**（v1） | 本地单机工具，无存量数据兼容负担；结构变更成本可接受；降低 AI 开发出错面。v2 需要时再引入 |
| D3 | **透传是一等公民而非临时功能**：`model` 字段解析顺序 = Pipeline 名优先 → 否则按真实模型名透传 | 透传即最好的对比测试工具，且网关的认证/日志/错误处理与 council 共用 |
| D4 | **流式语义**：成员阶段并发但非流式，裁判阶段流式转发（SSE） | MoA 类系统通行做法，需求文档已确认 |
| D5 | 管理 API `/api/admin/*` 不做认证，**整服务默认仅监听 127.0.0.1** | 本地工具，简化；对外 API 仍需 Bearer Key |
| D6 | 日志全文存储默认开启；"关闭全文"为 P1 项，随 T26 可选实现 | 控制本期范围 |
| D7 | 上游调用的**重试/超时/错误映射放在适配器基座**统一实现 | 策略层只管编排，不重复造轮子 |
| D8 | 上游调用**一律流式**，超时=**TTFT/块间空闲**（等不到上游数据才超时，不限制总生成时长；D4 的成员阶段同步改为流式聚合） | 复杂问题长生成不再被总时长超时杀死；首个字符前才允许整体重试，已产出内容后不重放 |

### 1.2 项目结构

```
muti_llm/
├── AGENTS.md                 # AI 开发约定（技术栈/命令/风格/目录）
├── Makefile                  # make dev / make test / make e2e / make seed
├── docker-compose.yml
├── backend/
│   ├── pyproject.toml        # uv 管理; fastapi uvicorn sqlalchemy aiohttp…
│   ├── app/
│   │   ├── main.py           # 入口: 组装路由+静态托管+启动建表
│   │   ├── settings.py       # pydantic-settings(端口/DB路径/加密密钥)
│   │   ├── db.py             # async engine(WAL) + session
│   │   ├── orm.py            # 全部 SQLAlchemy 表模型(单文件,表不多)
│   │   ├── schemas.py        # Pydantic 请求/响应模型(管理API+OpenAI兼容)
│   │   ├── security.py       # Fernet 加解密 + 自身Key生成/哈希校验
│   │   ├── gateways/
│   │   │   ├── llm_gateway.py     # /v1/* 认证+model解析+分发+日志
│   │   ├── adapters/
│   │   │   ├── base.py            # 重试/超时/错误映射/usage归一化
│   │   │   ├── openai_compat.py   # AsyncOpenAI(任意base_url, 含Ollama)
│   │   │   └── anthropic.py
│   │   ├── strategies/
│   │   │   ├── base.py            # Strategy 接口 + 注册表
│   │   │   └── council.py         # 成员并发/裁判组装/降级
│   │   ├── admin/              # /api/admin: providers models pipelines
│   │   │   …                    #   traces settings 各一个路由文件
│   │   └── logging_svc.py      # request_logs/model_call_logs 写入
│   └── tests/
│       ├── conftest.py        # ASGI客户端+临时DB+快照回放服务器fixture
│       ├── unit/
│       ├── e2e_api/
│       ├── snapshot_server/   # ★ 快照录制/回放服务器(见2.1)
│       └── snapshots/         # 真实LLM录制的快照数据(提交进库)
├── frontend/
│   ├── package.json           # vite react tanstack-query router tailwind
│   ├── src/
│   │   ├── lib/api.ts         # 管理API client(带类型)
│   │   ├── pages/             # Providers Models Pipelines Playground
│   │   │                      #   Traces Settings
│   │   └── components/        # shadcn/ui + Markdown查看器等
│   └── e2e/                   # Playwright(webServer配置: 后端+快照回放服务器)
└── scripts/
    └── seed_demo.py           # 一键造演示数据(指向快照回放服务器)
```

### 1.3 数据表（与需求文档 5.x 一致，落地字段）

`providers / models / pipelines / pipeline_members / request_logs / model_call_logs / app_settings(存自身Key哈希、日志保留天数等)` —— 共 7 张表，全部定义在 `orm.py`。建表即建索引：`request_logs.created_at`、`model_call_logs.request_id`。

---

## 2. 测试策略总纲

### 2.1 ★ 快照回放服务器（Snapshot Replay Server, SRS）——测试体系的地基

**原则：Record once, replay forever。** 每条 AE/UE 用例第一次运行时**真实调用一次 DeepSeek**（凭据见 2.4），将「请求 → 响应」固化为快照文件提交入库；之后所有测试运行都命中快照回放——不再消耗 API 费用、不受网络波动影响、完全确定性，且期望值就是真实 LLM 的产出。

SRS 是一个独立 FastAPI 应用（`tests/snapshot_server/`），以 OpenAI 兼容协议提供 `POST /v1/chat/completions`，两种模式：

- **replay（默认）**：规范化请求体（key 排序后 JSON 序列化）→ sha256 → 在 `tests/snapshots/` 查找：
  - 命中 → 非流式原样返回快照响应；流式按快照的 chunk 序列（含录制时的 chunk 间隔）以 SSE 重放；
  - 未命中 → 500，错误信息含请求 hash 与最接近的候选 hash 列表（快速定位非确定性来源）。
- **record（`SNAPSHOT_MODE=record`，即 `make record`）**：将请求透传给真实 DeepSeek，把响应原文与流式 chunk 序列（含相邻 chunk 间隔 delay_ms）落盘为新快照后返回。`make record` = 以 record 模式重跑全部 AE/UE 用例，完成首次录制或刷新基线。

叠加在快照之上的可编程控制（容错/时序类用例不可能靠真实 LLM 触发，必须可注入）：

- `POST /_test/config`：`{"fail_times": {"model-a": 2}}` 该模型连续失败 N 次后恢复快照回放（测重试/容错）；`{"timeout_ms": ...}` 模拟挂死；`{"delay_scale": 0}` 快进 chunk 间隔加速用例。
- `GET /_test/requests?model=...`：**请求录像**——断言"裁判实际收到的 Prompt""参数合并是否生效"的唯一手段。
- `{"reset": true}`：清空注入与录像。

快照文件（每条一个 JSON，按场景目录组织，提交进库）：

```json
{
  "scenario": "council_basic",
  "request_hash": "sha256:9f3a…",
  "request": {"model": "deepseek-v4-flash", "messages": ["…"], "temperature": 0.9, "stream": false},
  "non_stream_response": {"…": "原始 OpenAI 响应原文，一字不改"},
  "stream_chunks": [{"delta_content": "…", "delay_ms": 240}]
}
```

约束：

- 请求 hash 覆盖全部影响语义的字段；测试输入必须完全确定（不含时间戳/随机数），保证"录制那一次"与"之后每次回放"的请求逐字节一致。
- 断言口径：用例从快照文件读取期望 content/usage 与运行结果比对——期望值即真实 LLM 固化输出，天然自洽，无需人工编造预期答案。

### 2.2 三层测试怎么跑

| 层 | 工具 | 被测对象 | 上游 | 典型断言 |
| --- | --- | --- | --- | --- |
| UT | pytest + pytest-asyncio | strategies/adapters/security/logging_svc 等模块函数 | 打桩（monkeypatch 适配器返回值） | 并发耗时、容错分支、模板渲染、加密往返 |
| AE | pytest + httpx.AsyncClient(ASGI) | 后端整体（管理API+网关+DB+日志） | **快照回放服务器**（replay 模式） | OpenAI 兼容响应结构、usage 汇总、DB 落库、录像内容 |
| UE | Playwright | 全栈（UI+后端+SRS） | **快照回放服务器**（replay 模式） | 页面操作流：建配置→试运行→看Trace |

- AE 的 conftest：每个用例独立临时 SQLite 文件；`app.state` 注入测试配置；启动时自动生成服务 Key 供断言读取。
- UE 的数据准备：被测页面用 UI 操作，**前置依赖（provider/model 等种子数据）直接调管理 API 或 `scripts/seed_demo.py`**，保证用例稳定。
- Anthropic 适配器本期只做 UT（打桩 HTTP），快照库不覆盖 `/v1/messages`（P1 再补）。

### 2.3 命令与门槛

```makefile
make test          # = 后端 pytest 全量(快照回放) + 前端 vitest + 前端 lint
make e2e           # = 后端 AE(已含于pytest) + Playwright 全量(快照回放)
make record        # ★ 快照录制: 真实调用 DeepSeek, 生成/刷新 tests/snapshots/ (需 .env.test 凭据)
make seed          # 起服务+灌入演示数据(指向快照回放服务器), 供人工验收
```

覆盖率门槛（pytest-cov）：`app/strategies`、`app/adapters`、`app/gateways` ≥ **80%** 语句；`app/` 整体 ≥ 60%。前端不设覆盖率门槛，以 Playwright 场景为准。

### 2.4 测试凭据（用户提供，仅用于录制快照，可明文便于调试）

| 项 | 值 |
| --- | --- |
| Provider | DeepSeek（openai_compatible 协议） |
| Base URL | `https://api.deepseek.com`（完整端点 `https://api.deepseek.com/chat/completions`，SDK 自动拼接路径） |
| API Key | `sk-979fbfa0e6134b71bb1c56c2d2c17cbe` |
| Model | `deepseek-v4-flash` |

- 存放于 `backend/.env.test`（`RECORD_API_KEY` / `RECORD_BASE_URL` / `RECORD_MODEL`），**仅 `make record`（record 模式）读取**；日常 `make test` / `make e2e` 全程不触网、不需要 Key。
- 该 Key 为一次性开发凭据，**开发完成后作废**——作废后只影响 `make record`，快照回放与全部验收测试不受影响。
- 快照文件只含请求/响应原文，不含认证头；若仓库日后公开，删除 `.env.test` 即可。

---

## 3. 任务拆分与验收用例

> 每个任务 = 功能 + 对应测试。依赖列只写强依赖。用例类型：UT/AE/UE。

### Phase 0 —— 基础设施

#### T01 项目脚手架与开发环境 〔基础设施例外：仅冒烟〕
产出：backend(uv+FastAPI+pytest 可跑通空用例)、frontend(Vite+React+TS+Tailwind+Playwright 可跑通空用例)、根 Makefile、AGENTS.md(技术栈/命令/目录约定/代码风格)、`.env.example`。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-01-1 | UT | 冒烟：`pytest` 空套件退出码 0 | 无失败即过 |
| AE-01-1 | AE | `GET /health` | 200，返回 `{"status":"ok"}` |

#### T02 数据库层与 ORM 〔7 张表 + WAL + 索引〕
产出：`db.py`(async engine，PRAGMA journal_mode=WAL)、`orm.py` 全部表、每表基础仓储函数(create/get/list/update/delete)。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-02-1 | UT | 各表 CRUD 往返 | create 后字段可完整读回；update 生效；delete 后 get 为空 |
| UT-02-2 | UT | pipeline 名唯一约束 | 重名插入抛 IntegrityError |
| UT-02-3 | UT | model_call_logs 按 request_id 查询 | 父子关联正确，返回按时间排序 |
| UT-02-4 | UT | 时区往返 | SQLite 读回的时间统一补 UTC（API 序列化带 Z/+00:00，前端不再差时区） |

#### T03 快照回放服务器 + 首次录制 〔基础设施例外：仅自身用例，但它是后续一切的地基〕
产出：`tests/snapshot_server/`（见 2.1：replay/record 双模式、失败注入、请求录像）；`make record` 目标；`backend/.env.test`（2.4 凭据）；执行首次录制生成 `tests/snapshots/` 基线，覆盖场景：透传非流式/流式、council 非流式/流式、连通性测试消息。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-03-1 | UT | 快照命中(非流式) | 以快照库中已存在的请求发起调用 → 返回 content/usage 与该快照逐字段一致 |
| UT-03-2 | UT | 快照命中(流式) | 依次收到快照 chunk 序列(含 delay)，拼接=快照全文，末行 `data: [DONE]` |
| UT-03-3 | UT | 未命中诊断 | 构造快照库外的请求 → 500，错误含请求 hash 与最接近候选列表 |
| UT-03-4 | UT | 失败注入优先 | 对某模型配 fail_times=2 → 前两次 500，第三次起恢复快照回放 |
| UT-03-5 | UT | 请求录像 | 发送请求后 `/_test/requests` 能查到完整 messages/参数原文 |
| UT-03-6 | UT | 录制往返 | record 模式下打桩真实上游(不真调) → 快照文件生成且字段完整，切回 replay 立即命中 |

#### T04 加密与安全模块
产出：`security.py`：Fernet 加解密（密钥来自 settings）、`mask_key()`(显示尾4位)、自身服务 Key 生成(随机 `sk-local-` 前缀)、SHA-256 哈希校验。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-04-1 | UT | 加解密往返 | encrypt→密文不含明文→decrypt 还原 |
| UT-04-2 | UT | mask | `sk-abc12345678` → `****5678` |
| UT-04-3 | UT | 服务Key校验 | 正确 Key 哈希比对通过；错误 Key 拒绝；两次生成的 Key 不同 |

#### T05 服务配置与首次启动初始化
产出：`settings.py`(pydantic-settings：HOST 默认 127.0.0.1、PORT、DB 路径、加密密钥种子、日志保留天数默认30)；启动事件：建表、`app_settings` 无 Key 时自动生成并存哈希。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-05-1 | UT | 首次启动生成 | 全新临时库启动后 `app_settings` 存在 key_hash，且能通过 T04 校验 |
| UT-05-2 | UT | 重复启动不覆盖 | 二次启动后哈希不变 |

### Phase 1 —— 适配器、管理 API 与透传网关

#### T06 LLM 适配器基座 + openai_compatible 适配器
产出：`adapters/base.py`：统一入参(归一化的消息/参数)出参(content/usage/耗时/原始错误)，重试(默认1次、指数退避)、超时、异常→本服务错误对象映射；`openai_compat.py` 基于 AsyncOpenAI 指向任意 base_url，支持非流式与流式迭代器。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-06-1 | UT | 基本调用(快照回放服务器) | 返回 content 与 usage，耗时字段>0 |
| UT-06-2 | UT | 重试成功 | 上游失败1次后成功(重试默认开启) → 结果成功 |
| UT-06-3 | UT | 重试耗尽 | 连续失败 → 抛出含上游错误摘要的适配器异常，不裸抛 httpx 异常 |
| UT-06-4 | UT | 超时 | 配置 timeout=0.2s、上游延迟1s → 抛超时错误 |
| UT-06-5 | UT | 流式迭代 | 迭代产出多个增量片段，拼接等于完整答案 |
| UT-06-6 | UT | 连通性探测 | `probe()` GET 上游模型列表返回模型 ID；上游 401 → 归一化认证错误 |
| UT-06-7 | UT | TTFT 超时 | 首事件超预算 → kind=timeout 且 retryable（仅首事件前可整体重试） |
| UT-06-8 | UT | 长生成不限总时长 | 总时长超 timeout 但事件间隔均在预算内 → 聚合成功，usage 取流内事件 |
| UT-06-9 | UT | 中途空闲超时 | 已产出内容后等不到后续数据 → timeout 且不可重试（不重复生成） |

#### T07 anthropic 适配器 〔本期仅 UT 打桩〕
产出：`anthropic.py`：messages API ↔ 归一化参数转换、usage 映射、流式事件→增量片段。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-07-1 | UT | 参数/响应映射 | Anthropic 格式请求构造正确；响应 content/usage 正确归一化 |
| UT-07-2 | UT | 流式事件转换 | content_block_delta 事件序列 → 增量片段序列 |
| UT-07-3 | UT | 复用基座 | 错误/重试行为与 T06 一致(打桩验证走了 base 重试逻辑) |
| UT-07-4 | UT | 连通性探测 | `probe()` GET 上游模型列表返回模型 ID，请求携带 x-api-key |

#### T08 Provider 管理 API
产出：`/api/admin/providers` CRUD + 启用开关 + 协议枚举校验；api_key 落库前 Fernet 加密；响应中 api_key 只回显掩码。`POST /api/admin/providers/{id}/test` 连通性测试（GET 上游模型列表，验证 URL/Key，不消耗对话 token）。创建与测试成功时按上游模型列表自动补建 Model（best-effort，上游不可达不阻塞创建）；删除 Provider 级联删除其 Model 及失效 Pipeline（原 409 删除保护取消）。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| AE-08-1 | AE | 创建+查询 | 201；响应含掩码 Key；DB 中存的是密文（用例内直接查库断言） |
| AE-08-2 | AE | 更新 | 改 base_url 后 GET 返回新值 |
| AE-08-3 | AE | 级联删除 | 删 Provider → 204，其 Model 一并删除；裁判失效/成员清空的 Pipeline 删除，仍有成员与有效裁判的 Pipeline 保留 |
| AE-08-4 | AE | 校验 | 非法 protocol 枚举 → 422 |
| AE-08-5 | AE | 连通性成功 | `POST /providers/{id}/test` 指向 SRS → `{"ok":true,"latency_ms":≥0,"models":[…]}`，SRS 录像收到带认证头的探测请求 |
| AE-08-6 | AE | 连通性失败 | 指向不存在端口 → `{"ok":false,"error":"…"}`，HTTP 仍为 200（业务结果而非异常） |
| AE-08-7 | AE | 错误 Key | 上游 401 → ok:false 且 error 指向认证失败 |
| AE-08-8 | AE | 自动同步 | 创建指向 SRS 的 Provider → 自动建出上游模型；重复"测试"不重复建；死地址创建不阻塞、模型留空 |

#### T09 Model 管理 API + 连通性测试
产出：`/api/admin/models` CRUD；`POST /api/admin/models/{id}/test` 实际调用一次该模型（经适配器，默认固定测试消息），返回 成功/失败/延迟ms。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| AE-09-1 | AE | CRUD | 创建绑定 provider_id 的模型，列表/更新/删除正常 |
| AE-09-2 | AE | 连通性成功 | 模型指向快照回放服务器 → `{"ok":true,"latency_ms":≥0}`，且 SRS 录像里收到该测试请求 |
| AE-09-3 | AE | 连通性失败 | 指向不存在端口 → `{"ok":false,"error":"…"}`，HTTP 仍为 200（业务结果而非异常） |

#### T10 网关骨架：认证 + model 解析 + /v1/models
产出：`gateways/llm_gateway.py`：Bearer 认证中间件（仅 /v1/* 生效）；`model` 字段解析（Pipeline 名优先 → 透传真实模型，都无则 404）；`GET /v1/models` 返回启用 Pipeline + 透传模型；OpenAI 兼容错误结构(401/404/400)。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| AE-10-1 | AE | 认证 | 无 Key/错 Key → 401 且 body 为 OpenAI error 结构；正确 Key(种子读取) → 非401 |
| AE-10-2 | AE | model 解析 | model=不存在名 → 404 `model_not_found`；model=已配置真实模型名 → 路由到透传分支(400 参数缺失亦可，证明命中) |
| AE-10-3 | AE | models 列表 | 种子1个Pipeline后列表含该虚拟模型，object=`model` 结构兼容 |

#### T11 透传调用（非流式）+ 调用日志
产出：透传分支：参数合并(请求优先)→适配器→OpenAI 响应；`logging_svc.py`：每次请求写 request_logs + 每次上游调用写 model_call_logs（含全文）。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| AE-11-1 | AE | 端到端 | 种子模型指向快照服务器 → 响应 content/usage 与快照记录完全一致，结构兼容 OpenAI(choices/usage/finish_reason) |
| AE-11-2 | AE | 参数合并优先级 | 模型默认 temperature=0.1、请求传0.9 → SRS 录像中的请求 temperature=0.9 |
| AE-11-3 | AE | 日志落库 | 调用后 DB：request_logs 1条(status=success, 耗时>0, token>0)；model_call_logs 1条(role=member/passthrough, 含完整请求与响应全文) |
| AE-11-4 | AE | 上游失败 | 失败注入(优先于快照) → 502 + OpenAI error；日志 status=failed 且 error_message 非空 |

#### T12 透传调用（流式 SSE）+ 客户端取消
产出：`stream=true` 时 SSE 转发适配器流；首 chunk 前置 `role` 事件、末尾 `[DONE]`；客户端断开触发取消并落 `client_cancelled`。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| AE-12-1 | AE | SSE 格式 | chunk 序列拼接=完整答案；每个均为 `data: {...}\n\n`；末行 `data: [DONE]` |
| AE-12-2 | AE | 流式也落库 | 读完整流后 DB 落 request_logs(最终全文=拼接结果) |
| AE-12-3 | AE | 取消 | httpx 流式读取首 chunk 后主动断开 → 短暂等待后日志 status=`client_cancelled` |

### Phase 2 —— council 策略引擎

#### T13 策略引擎框架
产出：`strategies/base.py`：Strategy 接口(`async run(ctx) -> StrategyResult`)、注册表 decorator、按 pipeline.strategy 查找；未注册策略名 → 明确错误。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-13-1 | UT | 注册与查找 | 注册 council 后能取到实例；未知名抛 StrategyNotFound |
| UT-13-2 | UT | 结果结构 | StrategyResult 含 final_content/usage汇总/degraded 标记/成员明细列表 |

#### T14 参数合并与成员并发执行器
产出：council 内部执行器：成员任务并发(受 pipeline.max_concurrency 限流)、单成员超时、部分失败容错(默认≥1成功即继续，可配置严格模式)、成员级参数合并：模型默认 < Pipeline 覆盖（客户端请求参数不作用于成员，仅作用于裁判）。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-14-1 | UT | 并发生效 | 3个打桩成员各延迟200ms → 总耗时 <500ms（串行必>600ms） |
| UT-14-2 | UT | 部分失败容错 | 3成员1个失败 → 返回2个成功结果，无异常 |
| UT-14-3 | UT | 全部失败 | 抛 AllMembersFailed，含各成员错误摘要 |
| UT-14-4 | UT | 严格模式 | 容错=strict 时任一失败即整体失败 |
| UT-14-5 | UT | 单成员超时 | 成员超时0.3s、打桩延迟1s → 该成员标记 timeout，其余正常 |
| UT-14-6 | UT | 并发上限 | max_concurrency=2、4成员各延迟100ms → 用打桩计数器断言同时在飞≤2 |
| UT-14-7 | UT | 参数合并 | 成员=默认<覆盖 两层合并正确，请求参数不透传给成员（打桩捕获实际入参） |
| UT-14-8 | UT | 上游流式调用（D8） | 成员 payload stream=true 且带 stream_options.include_usage；member_timeout 为成员 TTFT 预算 |

#### T15 裁判 Prompt 组装
产出：默认模板(需求文档7.2)渲染：`{{original_messages}}`/`{{candidate_answers}}` 占位符；自定义模板支持；成员答案带来源模型名标注。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-15-1 | UT | 默认模板 | 渲染结果含原问题文本、每个成员名与其答案、且成员答案顺序=配置顺序 |
| UT-15-2 | UT | 自定义模板 | 模板 `A:{{candidate_answers}}` 渲染仅替换占位符，不注入其它内容 |
| UT-15-3 | UT | 消息序列化 | 多轮 system/user/assistant 消息被完整序列化进 original_messages |

#### T16 council 非流式完整链路 + Trace 日志
产出：`council.py` 串起 执行器→组装→裁判调用→usage 汇总→响应组装；日志：1条 request_logs + N条成员 + 1条裁判 model_call_logs；裁判入参全文（组装后的 Prompt）入库。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| AE-16-1 | AE | 端到端 | 种子 2成员+1裁判 → 响应 content=快照库中该场景裁判响应的 content（真实 LLM 固化值），model=council-v1，结构兼容 OpenAI |
| AE-16-2 | AE | usage 汇总 | 响应 usage = 3次调用(2成员+裁判)快照 usage 之和（期望值从快照文件读取，精确断言） |
| AE-16-3 | AE | 裁判收到正确Prompt | SRS 录像：judge 请求的 messages 含两个成员各自的快照答案全文与模型名标注 |
| AE-16-4 | AE | Trace 完整 | DB: 1 request_logs(success) + 3 model_call_logs(2 member + 1 judge)，judge 记录的 request_payload 含组装 Prompt 全文，role 字段正确 |
| AE-16-5 | AE | 请求参数仅作用裁判 | 请求 temp=0.7、成员覆盖 0.9 → 录像显示成员实发 0.9、裁判两次调用实发 0.7 |

#### T17 council 流式（裁判阶段 SSE）
产出：成员阶段照旧非流式并发；裁判以流式调用，增量片段实时下发；请求中的 `stream:true` 走此路径。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| AE-17-1 | AE | SSE 端到端 | 拼接 chunk = 裁判预设答案；末行 `[DONE]`；chunk 的 model 字段=council-v1 |
| AE-17-2 | AE | 时序 | 裁判快照为多 chunk 且 chunk 间隔>0（录制时确保答案较长）→ 客户端分多批收到（断言收包间隔>100ms，证明真流式非缓冲） |
| AE-17-3 | AE | 流式Trace | 流结束后 DB 落完整记录，response_content=拼接结果 |

#### T18 容错矩阵与降级
产出：全部失败→502(附各成员摘要)；裁判失败→降级返回首个成功成员答案+`degraded:true`(响应扩展字段)+日志标记；裁判失败可配置为直接报错；客户端取消→取消成员/裁判调用+`client_cancelled`。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| AE-18-1 | AE | 全部成员失败 | 2成员均注入失败 → 502，error 含两个成员名；日志 failed |
| AE-18-2 | AE | 裁判失败降级 | 裁判注入失败、成员成功 → 200，content=首个成功成员答案，`degraded:true`，日志 status=degraded |
| AE-18-3 | AE | 降级关闭 | 容错配置 strict_judge → 裁判失败时 502 而非降级 |
| UT-18-1 | UT | 取消传播 | 成员打桩支持取消信号 → 模拟断开后所有在飞任务被取消，日志 client_cancelled |

#### T19 Pipeline 管理 API + 校验
产出：`/api/admin/pipelines` CRUD：成员有序可重复、每项参数覆盖、裁判模型、Prompt 模板、超时/容错/并发配置；校验：≥1成员+1裁判、名称唯一且合法(^[a-z0-9-_]+$)、启停。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| AE-19-1 | AE | CRUD+成员排序 | 创建带3成员的 pipeline，GET 返回成员顺序与各项覆盖参数一致；更新排序生效 |
| AE-19-2 | AE | 校验-成员不足 | 0成员 → 422；无裁判 → 422 |
| AE-19-3 | AE | 校验-名称 | 重名/非法字符 → 422/409 |
| AE-19-4 | AE | 启停联动 | 停用后 /v1/models 不再列出，调用该名 → 404(不再命中Pipeline, 走解析兜底) |

### Phase 3 —— Web UI

#### T20 前端骨架与布局 〔基础设施例外：仅冒烟〕
产出：路由(6页面占位)/侧边导航/shadcn 接入/TanStack Query+api client(带类型，类型与后端 schema 对齐)/全局错误提示；Playwright webServer 配置(后端+快照回放服务器一键拉起)。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UE-20-1 | UE | 冒烟 | 打开 / 可见导航6项，无控制台报错(page.error 断言) |
| UT-20-1 | UT | api client | vitest：分页参数序列化/错误响应解析纯函数正确 |
| UT-20-2 | UT | 协议自动识别 | vitest：base_url 主机名含 anthropic → anthropic；其余/空值/非法输入 → openai_compatible 兜底 |

#### T21 Provider 管理页
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UE-21-1 | UE | 新建 | 表单填名称/base_url/Key（协议按 URL 自动识别并显示提示）→ 列表出现，Key 列显示 `****xxxx` 尾4位掩码，非明文 |
| UE-21-2 | UE | 编辑+启停 | 编辑 base_url 保存生效；停用后列表状态变化，且 /v1 相关引用行为由后端决定(UI只管展示) |
| UE-21-3 | UE | 级联删除 | 删 Provider 后其模型从模型页一并消失，页面不白屏 |
| UE-21-4 | UE | 连通性测试 | 指向 SRS 的 Provider 点"测试" → 显示成功/延迟/可用模型；坏地址 → 显示失败原因 |
| UE-21-5 | UE | 协议自动识别 | 无协议下拉；输入 api.anthropic.com → 表单显示"已识别协议：anthropic"，保存后列表协议列为 anthropic |
| UE-21-6 | UE | 自动建模型 | 创建指向 SRS 的供应商 → 模型页自动出现上游模型行 |

#### T22 Model 管理页 + 连通性测试
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UE-22-1 | UE | 新建绑定 | 选择 Provider、填上游模型ID与默认参数 → 列表出现且详情正确 |
| UE-22-2 | UE | 连通性 | 指向快照回放服务器的模型点"测试" → 显示成功与延迟(ms)；指向坏地址 → 显示失败原因 |
| UE-22-3 | UE | 参数表单 | temperature/max_tokens/timeout 非法输入(超范围/非数字)被前端校验拦截 |

#### T23 Pipeline 管理页
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UE-23-1 | UE | 新建全流程 | 建 2成员(可拖动/按钮排序)+裁判+默认模板 → 保存成功，详情回显顺序与配置 |
| UE-23-2 | UE | 校验 | 不选成员或裁判 → 保存被拦并提示 |
| UE-23-3 | UE | 模板编辑 | 编辑器修改模板后保存，重进页面回显一致；提供"恢复默认模板"按钮 |

#### T24 试运行 Playground
产出：页面选 Pipeline+输入消息→调网关 `/v1/chat/completions`(用服务Key)→分区展示：各成员答案卡片 / 裁判Prompt全文(Markdown) / 最终答案；展示耗时。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UE-24-1 | UE | 完整试运行 | 回放模式下试运行 → 页面出现两个成员卡片(内容=各自快照答案全文) + 裁判Prompt(含两成员答案) + 最终答案=裁判快照答案 |
| UE-24-2 | UE | 失败可读 | 全部成员注入失败 → 页面展示错误信息与各成员失败原因，不白屏不挂起 |

### Phase 4 —— 日志、设置与交付

#### T25 Trace 查询 API（列表+详情）
产出：`/api/admin/traces?start&end&pipeline&model&status&keyword&page`（keyword 匹配原始输入）；`/api/admin/traces/{request_id}` 返回完整时间线结构(request + calls[] 含全文/耗时/token/错误)。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| AE-25-1 | AE | 筛选 | 种子造 3 条不同 pipeline/status 的 trace → 各筛选维度单独命中正确记录数 |
| AE-25-2 | AE | keyword | 输入"量子"只返回原始请求含该词的记录 |
| AE-25-3 | AE | 详情 | council trace 详情：calls 恰好 N成员+1裁判、顺序正确、全文/耗时/token/role 齐全 |
| AE-25-4 | AE | 分页 | 造 25 条 → page=2 返回 5 条(page_size=20)，meta 正确 |

#### T26 日志清理
产出：保留天数(app_settings，默认30)定时清理 + `POST /api/admin/traces/clear` 手动清空；级联删 model_call_logs。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-26-1 | UT | 保留期 | 造 now-31d/now-29d 两条 → 触发清理后仅剩 29d 一条，子记录同删 |
| AE-26-2 | AE | 手动清空 | 清空接口后列表为空，且再写入新 trace 正常(自增不冲突) |

#### T27 Trace 列表页 + 详情页（时间线）
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UE-27-1 | UE | 列表筛选 | 状态筛选"degraded"→ 仅显示降级记录；时间范围选择生效 |
| UE-27-2 | UE | 详情时间线 | 点开 council trace → 按顺序看到：原始请求 → 成员卡片×N(模型名/入参/输出/耗时/token) → 裁判卡片(Prompt全文可展开) → 最终答案 |
| UE-27-3 | UE | 失败详情 | 打开 failed trace → 成员卡片展示错误信息 |
| UT-27-1 | UT | 前端纯函数 | vitest：Trace→时间线数据结构的转换函数(排序/分组/耗时格式化)正确 |

#### T28 服务设置页
产出：查看服务地址/版本/启动时间/存储占用；重置服务Key(弹窗一次性展示新Key)；修改日志保留天数。新增：接入信息卡（origin+/v1 地址，可复制）；服务 Key 掩码展示（sk-local-***尾4）+ 一键复制完整明文（Fernet 加密副本，/v1 认证仍走哈希比对不变；遗留库无副本需重置一次）。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UE-28-1 | UE | 重置Key流程 | 重置 → 弹窗展示新Key(仅一次) → 旧Key调 /v1 返回401、新Key成功(用例内直接curl断言) |
| AE-28-3 | AE | Key 展示接口 | GET service-key：首启即可取（掩码格式+明文可过 /v1 认证）；重置后同步；遗留库 available=false |
| UE-28-3 | UE | 接入信息与复制 | 设置页显示 /v1 接入地址；Key 显示 `sk-local-***尾4` 掩码，点"复制 Key"剪贴板得到完整明文 |
| UE-28-2 | UE | 保留天数 | 改为7天 → 重新查询设置接口返回7 |

#### T29 静态托管 + 单进程部署 + docker-compose + README 〔收尾〕
产出：FastAPI 挂载 `frontend/dist`；根路径返回 UI；`docker-compose.yml`(单服务单卷)；README(启动/配置/一键验收)；`make e2e` 作为总回归入口。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| AE-29-1 | AE | 静态托管 | `GET /` 返回 200 且 content-type=text/html |
| AE-29-2 | AE | 总回归 | `make test && make e2e` 全绿（CI 语义） |
| UE-29-1 | UE | Playwright全量 | 全部 UE 用例在"单进程+静态托管"模式下重跑通过（webServer 即生产形态） |

#### T30 Trace 详情：换裁判模型重新聚合（增量需求）
产出：`POST /api/admin/traces/{id}/rejudge`（同步，body: judge_model_id）与 `POST .../rejudge/stream`（SSE：meta 入参 → delta 增量 → done 终态+token）——`app/rejudge_svc.py` 的 `prepare_rejudge` 统一校验并复用 trace 已存成员答案、按 Pipeline 当前模板重渲染裁判 Prompt（复用 `council.py` 的 `render_judge_prompt`/`build_call_request`/`merge_params`），只重调裁判不重调成员；流式端点结束后经后台任务落库（断开记 client_cancelled）；输出以 `role="judge_rerun"` 追加进 `model_call_logs`（无表结构变更），成败均落行；**不回写 RequestLog**。1.2 未规划 `rejudge_svc.py` 位置：属 Trace 域写路径服务，与 logging_svc 平级，故独立成模块。前端 `toTimeline` 将 judge/judge_rerun 行合成裁判组（卡片固定置于原始请求之后、成员卡片之前）；裁判卡片以**下拉菜单**切换裁判模型（选项=本次成员+裁判模型）：有结果直接展示，无结果即发起流式重跑（SSE 实时增量、多模型并行、失败可重试）。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| AE-30-1 | AE | 重跑成功 | 换模型重跑 → judge_rerun 行追加、载荷与原裁判逐字节一致（快照复用）、可多次重跑全部保留、RequestLog 全字段不变 |
| AE-30-2 | AE | 重跑失败 | SRS 注入失败 → 200 + status=failed 行保留错误信息，原始记录不动 |
| AE-30-3 | AE | 拒绝路径 | trace 不存在 404；无成员答案(透传/全成员失败)/pending 409；模型不存在/停用 422 |
| AE-30-4 | AE | 流式重跑 | SSE meta→delta→done 事件齐全、delta 拼接=done.content=快照内容；两路并行均成功落库 |
| AE-30-5 | AE | 流式失败 | 无 delta、done.status=failed 带错误信息，失败行照常落库 |
| UT-30-1 | UT | 前端纯函数 | vitest：toTimeline 裁判版本合组（原始在前、重跑按序、透传无裁判组） |
| UE-30-1 | UE | 详情页重跑 | 下拉默认当前裁判、选项仅本次成员+裁判；换模型流式输出实时呈现、失败显示错误、同模型可重试成功、版本间切换立即显示各自输出 |

#### T31 两段式裁判（评论 → 最终答案）（增量需求）
产出：council 裁判阶段改为同一裁判模型串行两次调用且**同一会话**——第一次**评论**（内置 `DEFAULT_CRITIQUE_TEMPLATE`，role=`judge_critique`，单轮：原对话+各答案）；第二次**最终答案**（role=`judge`，多轮：`第一次输入(逐字节相同) → 评论(assistant) → 最终指令`，`judge_prompt_template` 仅作用于指令轮，评论不依赖占位符即随会话带入）。评论失败按 judge_failure 容错（流式下评论失败发生在开流前，统一 502）；usage = 成员+评论+最终求和。重跑（同步/SSE）同步两段化：SSE 事件带 `phase`（critique/final），每次重跑成对落 `judge_critique`+`judge_rerun` 行，前端按"同模型第 k 次评论 ↔ 第 k 个最终行"配对（并行重跑落库交错也正确），裁判卡「第一次调用 · 评论」折叠块含入参与输出。快照新增 `council_critique`/`council_critique_single_07`/`council_judge_custom` 并按同会话形态重录 `council_judge*`（旧文件删除）；SRS `create_app`/runner 支持 `delay_scale`（pytest fixture 与 Playwright 默认 0 即时回放提速，时序用例自行覆盖，reset 恢复初始值）。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| AE-31-1 | AE | 两段链路 | roles=[member×2, judge_critique, judge]；评论载荷含答案且无评论段、最终载荷含评论全文；token=四快照之和 |
| UT-31-1 | UT | 渲染 | {{critique}} 注入评论全文；模板无该占位符时不注入（另：toTimeline/judgeVersionCount 评论行合组不计版本） |
| UE-31-1 | UE | 详情评论块 | 裁判卡「第一次调用 · 评论」展开含评论内容（如【回答 1】/可信度）；换裁判重跑的新版本同样带评论 |

（T15/T16/T17/T18/T25/T30 的既有用例随两段式行为更新：roles 4 行、usage 四快照之和、评论失败降级、重跑成对落行、评论行不占版本号等。）

#### T38 过程流式（stream_process=true）（增量需求）
产出：请求体新参数 `stream_process`（仅 Pipeline 流式生效；非流式/透传静默忽略；不进 `strategy_params`、不透传上游，`build_call_request` 白名单不回归）。策略层新增 `ProcessEvent`/`FinalPlanReady`（`app/strategies/base.py`）与 `Strategy.run_stream_process`——默认实现回放 `prepare_stream` 前置明细为非渐进回退；council/ICE 覆写为 `asyncio.as_completed` 完成序渐进执行（`run_one` 提层共享、ICE 第 0 轮移入生成器、`_refine_one` 共享，上游载荷逐字节不变，快照复用不重录）；渐进生成器 try/finally 取消在飞成员任务（对齐 ICE `_iterate` 取消契约）。网关新增 `_pipeline_stream_process`（pump/ferry/persist 同 ICE 迭代路径骨架）：成员/评论完成即发 `delta.reasoning_content` 块（`【成员 X】`/`【评论】`/`【第 k 轮评论】` 标题块 + 按行内容，失败成员一行失败提示），流的首 chunk 携带 role；首个成员成功即返回响应（`asyncio.wait(FIRST_COMPLETED)`），此前 pump 失败重抛走既有 502 JSON；终局裁决照旧 content 流式；前置明细不做开流前预写，由 persist 从事件累计落库。错误契约：部分成员失败 → 提示行后继续；全成员失败 → 502；评论/终局失败 → 流内终止无 [DONE]；ICE 第 0 轮评论失败由 502 转为流内终止（仅 flag 开启时，AE-35-6 opt-in 契约变化）；detach/心跳/t0 请求级计时语义不变，`first_token_ms` 仅在终局首个 content delta 记录。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| AE-38-1 | AE | council 过程流式 | 成员块×2 各自连续（完成序不定、不断言成员次序）→ 评论块 → content=裁判流式快照；[DONE] 结尾；落库 roles=[member×2, judge_critique, judge] |
| AE-38-2 | AE | ICE 多轮过程流式 | 每轮成员块→该轮评论块→终局 content=快照；call 行 round 与既有语义一致 |
| AE-38-3 | AE | 全成员失败+flag | 开流前 502 JSON（同今日错误形态）；成员失败行照落、裁判未执行 |
| AE-38-4 | AE | 评论失败+flag | 200，成员块已发出，流内终止无 [DONE]，trace=failed，评论失败行落库 |
| AE-38-5 | AE | flag 忽略 | 透传流式/非流式 pipeline 带 `stream_process` → 响应与既有行为一致，零 reasoning_content |
| AE-39-1 | AE | pipeline 默认开 | `pipelines.stream_process=true`（schema 幂等补列）+ 请求体不带 flag → 流式响应含 reasoning 过程块（供不能自定义请求体的客户端） |
| AE-39-2 | AE | 请求体覆盖默认 | pipeline 默认开 + 请求体显式 `stream_process=false` → 经典路径，零 reasoning 块 |
| UT-32-5 | UT | schema 补列 | 旧库 pipelines 无该列 → init_db 幂等补列，旧行读回 False |

#### T40 模型同步下线标记（增量需求）
产出：Provider 模型同步从"只增"改为全量 diff（`_sync_models` 取代 `_create_missing_models`）——上游列表已消失的模型打 `models.upstream_missing=true` 并联动停用（`enabled=false`，schema 幂等补列），防止继续配置过时模型；重新上架自动恢复 enabled；用户手动停用（upstream_missing=false）不被覆盖；不物理删除（级联删除仅用户显式触发）。`POST /providers/{id}/test` 响应新增 `removed`（本次新下线的模型 ID）；模型页下线模型置灰、加"上游已下线"标签并置底排序；Pipeline 成员/裁判选择器不列出下线模型（已选中的保留展示便于更换）。SRS `_test/config` 新增 `models_override`（模拟上游列表增减，reset 恢复）。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| AE-40-1 | AE | 下线同步 | 上游缩列表 → removed=[下线ID]、该模型 upstream_missing=true+enabled=false；重复测试不重复上报；手动停用不被覆盖；重新上架自动恢复 enabled |
| UT-40-1 | UT | schema 补列 | 旧库 models 无该列 → init_db 幂等补列，旧行读回 False |

#### T41 用量统计看板（增量需求）
产出：与供应商对账的 token 用量统计看板（FR-15 扩展）。数据采集先行修复三处失真：① `LlmUsage` 扩展 `cached_tokens`/`cache_write_tokens` 并归一化语义——`prompt_tokens` 恒为计费输入总量（OpenAI 兼容上游原生含 cached；Anthropic 的 input_tokens 不含缓存，适配器补和为 input+cache_read+cache_creation），命中=cached、未命中=prompt−cached−write；openai_compat 解析 `prompt_tokens_details.cached_tokens`，anthropic 适配器解析 `cache_read_input_tokens`/`cache_creation_input_tokens`。② 流式路径的 usage 事件此前被丢弃（透传流式 token 全记 0、裁判流式只记前置阶段汇总），四处 pump 改用 `stream_events_timed` 捕获 usage 并透传落库（`record_call`/`to_log_kwargs`/rejudge 落库同步加缓存两列）。③ schema 幂等补列 `model_call_logs.cached_tokens/cache_write_tokens`（无 Alembic，D2）。统计 API 新增 `app/admin/stats.py`（1.2 未规划统计端点位置：属 admin 管理域只读资源路由，与 traces.py 平级；聚合用 SQLAlchemy func.sum/func.date 直接查询，无独立写路径故不设域服务）四端点 `overview/daily/by-provider/by-model`，数据源为 `model_call_logs`（冗余存储 provider_name/upstream_model_id，配置删除后统计仍完整；`created_at` 已有索引），共用 start/end（end 含当天，语义同 traces）/provider/model 筛选，`daily` 按 UTC 天分组；命中率由前端计算。前端新增"用量统计"页（路由 `/stats`）：KPI 卡、手写堆叠柱状日趋势图（零图表依赖，与项目无组件库约定一致）、供应商/模型明细表（含命中率列）、CSV 导出（客户端生成，BOM+CRLF/引号转义）。`mock_anthropic.build_sse_events` 增加可选缓存 usage 参数（测试地基，默认行为不变）。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-41-1 | UT | schema 补列 | 旧库 model_call_logs 无缓存两列 → init_db 幂等补列，旧行读回 0 |
| UT-41-2 | UT | OpenAI 缓存解析 | chunk usage 带 prompt_tokens_details.cached_tokens → LlmUsage.cached_tokens；字段缺省 → 0 |
| UT-41-3 | UT | Anthropic 归一化 | 带 cache_read/creation → prompt=input+读+写、cached/写 分列；无字段 → 行为与现状一致 |
| UT-41-4 | UT | 前端纯函数 | hitRate/未命中（异常口径不为负）/formatTokens/formatRate/toCsv（BOM+转义） |
| AE-41-1 | AE | 聚合正确性 | SRS 回放跑 council → 四端点合计与 model_call_logs 逐行求和一致；裁判行 cached_tokens 与快照 usage 一致（解析→落库→聚合全链路） |
| AE-41-2 | AE | 筛选 | provider/model/日期范围（start=明天 → 空；end 含当天）各维度命中正确 |
| AE-41-3 | AE | 流式 usage 修复 | 透传流式与 council 流式的调用行/请求级总计 token 与快照一致（修复前为 0），请求级总计=该请求各调用行之和 |
| UE-41-1 | UE | 看板渲染 | 造数跑一次 council → 模型表 2 行、供应商表行含快照精确合计；起始=明天 → 空态，重置 → 恢复 |
| UE-41-2 | UE | CSV 导出 | 导出触发 download，文件名/BOM/中文表头/供应商行内容正确 |

#### T42 命名空间网关（增量需求）：虚拟模型 / 路由模型分离接入
产出：设置页接入开关（T41.5 补丁，先落）`expose_virtual_models`/`expose_routed_models`（bootstrap 键、缺省开）作用于老入口：`GET /v1/models` 按开关过滤两类；`/v1/chat/completions` 的 model 解析（Pipeline 优先 → 透传）跳过被关闭的类（老入口行为兼容不变，已配置服务零改动）。新增命名空间网关 `app/gateways/namespaces.py`（1.2 未规划该文件：属对外 /v1 网关域，与 llm_gateway 平级共用执行路径 `_run_pipeline`/`_passthrough`，仅 model 解析范围不同；请求体校验抽为 `validate_chat_body` 共享）：`/v1/pipeline`（说明 JSON）、`GET /v1/pipeline/models`（仅启用 Pipeline）、`POST /v1/pipeline/chat/completions`（只认 Pipeline 名）；`GET /v1/route`（启用供应商 slug 列表）、`GET /v1/route/{ident}/models`（透传上游实时列表：TTL 30s 进程内缓存 + 上游失败回落库内已同步启用模型 + 过滤本地明确停用的行，上游新模型本地无记录 → 默认列出）、`POST /v1/route/{ident}/chat/completions`（model 只认该供应商短名：启用行正常透传；本地停用行 404；无记录行动态透传——瞬态 `LlmModel(id=0)` 不落配置行，调用日志 model_id=0，统计按冗余 upstream_model_id 呈现）。供应商定位 `ident`：数字 id 优先、其次 slug；两开关对命名空间同样生效（关闭 → 命名空间整体 404）。Provider 新增 `slug`（唯一、创建时由 `derive_slug` 生成——小写归一/冲突加序号/纯中文兜底 `provider-{id}`，一次定终身不随显示名变化；数字 id 撞全数字 slug 时 id 赢）：幂等补列（D2，旧库 UNIQUE 由生成逻辑保证）+ 存量回填；`ProviderOut.slug`；供应商更新/删除/手动测试联动失效路由列表缓存（conftest 每用例清缓存防跨库串味）。前端：Providers 页加 "ID / 路由标识" 列；设置页接入信息分两块（各带开关、启用计数、命名空间 base url 复制行，按供应商列出 `/v1/route/{slug}` 并注明可用数字 id）。测试零新快照：上游列表走 SRS 确定性 `/v1/models`（`models_override`/`models_auth_fail` 注入），动态透传复用 AE-10-1 同形态载荷快照。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-42-1 | UT | slug 名字归一 | 小写、非字母数字转连字符、折叠去首尾 |
| UT-42-2 | UT | slug 查重 | 冲突加序号 -2/-3 递增 |
| UT-42-3 | UT | slug 兜底 | 纯中文 → fallback（provider-{id}）；fallback 冲突同样加序号 |
| AE-10-4 | AE | 接入开关·虚拟 | 关闭后 /v1/models 不列 Pipeline、按名调用 404；透传不受影响；重开恢复 |
| AE-10-5 | AE | 接入开关·路由 | 关闭后不列真实模型、按名调用 404；Pipeline 不受影响 |
| AE-42-1 | AE | slug 身份 | 创建自动生成；改名不变；/v1/route 列出；slug 与数字 id 等价路由且列表一致 |
| AE-42-2 | AE | slug 生成 | 大小写/空格归一；冲突 -2；纯中文 → provider-{id} |
| AE-42-3 | AE | pipeline 命名空间 | models 只列虚拟模型；chat 认 Pipeline 名（502 策略分支）、真实模型名 404 |
| AE-42-4 | AE | route chat | 启用模型透传 200；本地停用 404；本地无记录的动态透传 200（快照回放） |
| AE-42-5 | AE | 路由列表口径 | 过滤本地停用；上游新模型保留；上游失败回落库内启用列表 |
| AE-42-6 | AE | 命名空间闸门 | expose_virtual_models=false → /v1/pipeline/* 404；expose_routed_models=false → /v1/route/* 404；停用供应商 → 该命名空间 404 且不出现在 /v1/route |

#### T43 工具透传（增量需求）
产出：OpenAI 兼容工具调用（tool_calls）在**透传路径**全链路支持，聚合（pipeline）路径维持明确拒绝。①校验层：`validate_chat_body` 空值容忍——客户端默认携带的"关闭态"值（`tools: []`、`tool_choice: "auto"/"none"`、`functions: []`、`function_call: "none"/"auto"`、`logprobs: false`、`logit_bias: {}`，Unsloth Studio 等）静默忽略，legacy `functions` 非空仍 400；非空 `tools` 不在校验层拒绝，由调用方分流：透传分支放行、pipeline 分支 400"聚合模式（Pipeline）暂不支持工具调用"（T44 工具聚合落地后按开关放行）。②适配器：`LlmRequest` 扩展 `tools/tool_choice`；`StreamEvent` 扩展 `tool_calls`（OpenAI delta 分片原样）与 `finish_reason`；`LlmResult` 扩展聚合后 `tool_calls`（`merge_tool_call_deltas` 按 index 合并：id/name 取首个、arguments 串接）与 `finish_reason`（默认 stop）；openai_compat 请求透传 tools/tool_choice、流内解析分片与 finish_reason；anthropic 适配器 tools 非空 → 400 级 AdapterError（协议暂不支持）。D8 语义不变：非流式 `complete()` 仍由流聚合。③网关：`_llm_request` payload/归一化请求带 tools/tool_choice（与快照 hash 一致）；非流式响应包装 `message.tool_calls`（纯工具调用时 content=null）+ 透传 finish_reason；流式 pump 原样下发 delta.tool_calls 分片、终止块 finish_reason 透传；`model_call_logs` 幂等补列 `response_tool_calls`（JSON，聚合形态落库存证），请求级 `response_finish_reason` 落透传值。快照：`passthrough_tool_calls`（真实 DeepSeek 录制，温度 0 + 强工具相关问题 → get_weather 调用分片 + finish_reason=tool_calls）；空容忍场景上游载荷不变，零新快照。 council 聚合的语义约束（裁判无法综合"调用动作"）在 T44 前保持拒绝，报错文案明确指向能力边界。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-43-1 | UT | 分片合并 | arguments 按 index 串接、id/name 取首个、多 index 互不串 |
| UT-43-2 | UT | 空分片 | 空列表 → 空合并（调用方置 None） |
| UT-43-3 | UT | 适配器聚合 | complete() 从真实快照流聚出完整 tool_calls + finish_reason=tool_calls，与快照分片重放聚合一致 |
| AE-43-1 | AE | 空值容忍 | tools=[]/tool_choice=auto/logprobs=false/logit_bias={} → 照常透传（上游载荷与既有快照一致） |
| AE-43-2 | AE | 非流式工具 | message.tool_calls 原样返回、finish_reason=tool_calls、response_tool_calls 落库；route 命名空间同路径一致 |
| AE-43-3 | AE | 流式工具 | delta.tool_calls 分片原样下发、终止块 finish_reason=tool_calls、[DONE] 正常 |
| AE-43-4 | AE | 聚合拒绝 | pipeline 名 + 非空 tools → 400"暂不支持工具调用"；空 tools → 放行进策略分支 |
| AE-43-5 | AE | 协议闸门 | anthropic 供应商 + 非空 tools → 适配器 400 级错误，不打上游 |

#### T44 council 工具聚合模式（增量需求，已实施）
产出：pipeline 级开关 `tool_aggregation`（ORM 布尔列 + 幂等补列，默认关；PipelineCreate/Update/Out 与前端表单/Badge 同步）。开启后 council 对非空 `tools` 放行进入聚合语义（关闭/其余策略仍 400；工具模式 + stream_process → 400，过程流式不做工具聚合）。策略层（council.py）：`tool_mode` 判定 → 成员带 tools/tool_choice 并行调用（`run_one` 经 `build_call_request` 透传，CallOutcome 增 `response_tool_calls`）→ `format_tool_intent` 把各成员输出文本化（调用列 JSON、混合输出带"附说明"）→ 复用两段式裁判：评论段仅 answers 换为意图文本（`assemble_critique` answers 参数），终局段 `assemble_tool_final` 用内置指令模板 `DEFAULT_TOOL_JUDGE_TEMPLATE`（裁判自由推理合成最终调用，评论是参考非约束；亦允许裁判判定无需工具时输出文字回答）并携带 tools/tool_choice 借裁判模型原生结构化输出产出调用。程序性兜底校验 `validate_tool_calls`（function 名 ∈ tools、arguments 合法 JSON 对象）失败 → `pick_fallback_tool_calls` 降级择优（多数 function.name 派系中取配置序首个成员调用，degraded=true）。`StrategyResult` 增 `tool_calls`；网关非流式响应包装 `message.tool_calls`（纯调用时 content=null）+ `final_finish_reason` 透传；流式 `prepare_stream` 工具分支换用 `assemble_tool_final`，`_pipeline_stream` pump 原样转发裁判流内 tool_calls 分片与 finish_reason（流式无中途降级拦截，校验结果随落库呈现），persist 落 `response_tool_calls` 与请求级 `response_finish_reason`。快照 3 个新增真实录制（`tool_council_member` 无参成员变体 / `tool_council_critique` 意图评论 / `tool_council_final` 裁判合成——录制器 `_tool_intent` 复用 `format_tool_intent` + 快照 content/tool_calls，保证与运行时 Prompt 逐字节一致；成员 B 复用 `passthrough_tool_calls` 温度 0 变体）。已知边界（需求方接受）：裁判仲裁为概率性（无执行反馈）；agent loop 每轮回填触发整场聚合，成本为单模型 N 倍以上；流式路径无降级拦截。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-44-1 | UT | 意图文本化 | 纯调用 / 调用+附说明 / 纯文本 / 空输出 四形态 |
| UT-44-2 | UT | schema 兜底校验 | 合法通过；未知函数名、非法 JSON、非对象、空 拒绝 |
| UT-44-3 | UT | 降级择优 | 多数派取配置序首个；无人产出调用 → None |
| AE-44-1 | AE | 聚合主链路 | 响应 tool_calls == 快照裁判合成（含修正后的参数）、finish_reason=tool_calls、未降级；4 行调用日志（成员带 tool_calls、裁判行 response_tool_calls） |
| AE-44-2 | AE | 流式聚合 | 终局流 tool_calls 分片原样下发、finish_reason 透传、[DONE] 正常 |
| AE-44-3 | AE | 开关闸门 | tool_aggregation=false → 400"暂不支持工具调用"；无 tools 的文本聚合不受开关影响 |

---

## 4. 里程碑映射（对应需求文档 M1–M4）

| 里程碑 | 任务 | 出口判据（可 curl/可演示） |
| --- | --- | --- |
| M1 | T01–T12 | AE-11-1/12-1 通过：任一 OpenAI SDK 指向本服务透传单模型成功（流式+非流式） |
| M2 | T13–T19 | AE-16-1/17-1 通过：`council-v1` 可用，Trace 落库完整 |
| M3 | T20–T24 | UE-24-1 通过：UI 配置→试运行全流程可视 |
| M4 | T25–T29 | UE-27-2 通过 + docker-compose 冒烟：一条命令起全栈 |

## 5. 风险与备注

1. **快照命中的确定性是命门**：请求 hash 覆盖所有影响语义的字段，且任何测试请求不得含时间戳/随机数，否则回放必未命中。未命中的报错必须打印请求 hash 与最接近候选，便于定位；新增用例后必须重跑 `make record` 补录快照。
2. **录制必须防漂移**：`make record` 期间真实响应落盘后，禁止手改快照中的期望值；要改行为先改代码再重录，保证期望值始终来自真实 LLM。
3. AE-17-2（真流式时序）在负载抖动下可能偶发——实现时给断言留容差（如收包间隔 > 100ms 即算分批），必要时用 `delay_scale` 放大 chunk 间隔。
4. Playwright 用例不 mock 前端网络层，全部走真实后端+快照回放服务器，保证测的是全栈行为；代价是用例要负责数据清理（seed 前先清库）。
5. **测试凭据为一次性 Key（见 2.4），开发完成后作废**：作废后仅 `make record` 不可用，`make test`/`make e2e` 全量回放不受影响；仓库公开前删除 `backend/.env.test`，快照数据本身不含 Key。
6. P1 项（日志全文关闭开关、Anthropic E2E、统计面板）不阻塞里程碑，按需求文档优先级排后。
