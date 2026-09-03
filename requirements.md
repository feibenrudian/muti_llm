# 多模型聚合 LLM 网关 —— 需求文档

| 项目名称 | muti_llm（多模型聚合 LLM 服务） |
| --- | --- |
| 文档版本 | v1.0 |
| 日期 | 2026-09-02 |
| 状态 | 初稿（待评审） |

---

## 1. 项目背景与目标

### 1.1 背景

单个 LLM 的回答质量受模型能力、领域偏差影响较大。对于高质量问答场景，一种有效的做法是：让多个 LLM 各自独立回答同一问题，再由一个"裁判模型"综合各方答案，产出最终回复（即 LLM Council / Mixture-of-Agents 模式）。

本项目要在本地部署一个这样的服务：**对外暴露标准 LLM 接口（OpenAI 兼容），内部将请求分发给多个上游 LLM，经裁判模型聚合后返回最优答案**，并配套 Web UI 进行配置管理和调用日志查询。

### 1.2 目标

- G1：对外提供 OpenAI Chat Completions 兼容 API，客户端（SDK、ChatBox、Dify 等）无需改造即可接入。
- G2：支持接入多个 LLM 供应商（OpenAI 兼容协议 + Anthropic 协议 + 本地 Ollama），可在 Web UI 中配置模型、API Key、调用参数。
- G3：实现第一种模型组合策略——**多模型并行回答 + 裁判模型仲裁**；策略框架可扩展，为后续策略（投票、加权、级联等）预留接口。
- G4：Web UI 可查询每次请求的完整链路：原始输入、每个成员模型的输入/输出、裁判模型的输入/输出、最终答案、耗时与 token 用量。

### 1.3 非目标（本期不做）

- 不做用户体系、多租户、按用户计费配额。
- 不做 Embedding / Image / Audio 等非 Chat 类接口的聚合。
- 不实现其他聚合策略（仅预留扩展点）。
- 不做分布式部署，本期单机本地运行。

---

## 2. 术语定义

| 术语 | 定义 |
| --- | --- |
| **Provider（供应商）** | 一个上游 LLM 服务接入点，含 base_url、api_key、协议类型。如 OpenAI、DeepSeek、智谱、Anthropic、本地 Ollama。 |
| **Model（模型）** | 属于某个 Provider 的具体模型，如 `gpt-4o`、`deepseek-chat`，带默认调用参数。 |
| **Pipeline（组合 / 虚拟模型）** | 一条聚合策略配置，对外表现为一个"虚拟模型名"。客户端请求中的 `model` 字段填 Pipeline 名称，如 `council-v1`。 |
| **成员模型（Member）** | Pipeline 中并行回答问题的 N 个模型。 |
| **裁判模型（Judge）** | 汇总 N 个成员答案后产出最终答案的模型。 |
| **策略（Strategy）** | 聚合算法类型。本期仅实现 `council`（多模型 + 裁判）。 |
| **请求链路（Trace）** | 一次对外请求的完整记录：原始请求 → N 次成员调用 → 1 次裁判调用 → 最终响应。 |

---

## 3. 整体架构

```
┌────────────────────────────────────────────────────────┐
│                     客户端（SDK / Web 应用）              │
└───────────────┬────────────────────────────────────────┘
                │ OpenAI 兼容协议
                ▼
┌────────────────────────────────────────────────────────┐
│                  API 网关层（对外服务）                    │
│  POST /v1/chat/completions   GET /v1/models             │
│  认证（服务自身 API Key）、参数校验、按 model 路由 Pipeline │
└───────────────┬────────────────────────────────────────┘
                ▼
┌────────────────────────────────────────────────────────┐
│                  编排层（Strategy Engine）                │
│  council 策略：                                          │
│   1. 并发调用 N 个成员模型（各自独立回答）                  │
│   2. 组装裁判 Prompt（原问题 + N 个候选答案）               │
│   3. 调用裁判模型 → 最终答案                               │
│  （策略接口抽象，可插拔扩展）                               │
└───────┬───────────────────────┬────────────────────────┘
        ▼                       ▼
┌───────────────┐      ┌────────────────────┐
│  模型接入层     │      │   Web UI + 管理 API │
│ OpenAI 协议    │      │  Provider/Model 配置 │
│ Anthropic 协议 │      │  Pipeline 配置       │
│ Ollama        │      │  调用日志 / Trace 查询│
└───────┬───────┘      └─────────┬──────────┘
        ▼                        ▼
┌────────────────────────────────────────────────────────┐
│              存储层（SQLite，本地单文件）                   │
│  配置数据 + 请求/调用日志（含输入输出全文）                  │
└────────────────────────────────────────────────────────┘
```

---

## 4. 功能需求

优先级说明：P0 = 本期必须实现；P1 = 本期应实现；P2 = 可选/后续版本。

### 4.1 对外 API（API 网关层）

#### FR-1 OpenAI 兼容 Chat 接口 【P0】

- `POST /v1/chat/completions`，请求/响应结构兼容 OpenAI Chat Completions 规范。
- `model` 字段取值为 Pipeline 名称（虚拟模型），如 `council-v1`；也允许直接填真实模型名做**透传**（便于对比测试，不聚合）【P1】。
- 支持字段：`messages`、`temperature`、`max_tokens`、`stream`、`top_p`。透传给成员模型时，Pipeline 配置的默认参数与请求参数的合并规则：**请求参数优先**。
- 不支持的字段（如 `tools`、`function_call`、`logprobs` 等）：本期返回明确错误提示 `不支持的能力`，不做聚合语义。【P0】

#### FR-2 流式输出（SSE）【P0】

- `stream=true` 时以 SSE 返回，chunk 格式兼容 OpenAI。
- 流式语义：**成员阶段非流式并发**（等待完整答案），**裁判阶段流式转发**给客户端。即用户看到的打字机效果来自裁判模型的输出。
- 需正确处理客户端中断连接（取消上游调用，标记日志状态为 `client_cancelled`）。

#### FR-3 模型列表接口 【P1】

- `GET /v1/models` 返回所有启用的 Pipeline 虚拟模型（及可选的透传真实模型），格式兼容 OpenAI。

#### FR-4 服务认证 【P0】

- 服务自身签发一个 API Key（首次启动自动生成，可在 Web UI 重置）。
- 客户端以 `Authorization: Bearer <key>` 访问；未认证返回 401，格式兼容 OpenAI 错误结构。

#### FR-5 错误处理 【P0】

- 所有错误以 OpenAI 兼容的错误 JSON 返回，含 HTTP 状态码、`error.message`。
- 上游全部失败 → 返回 502 并附各上游失败摘要。
- `model` 不存在 → 404；参数非法 → 400。

### 4.2 模型接入层

#### FR-6 Provider 管理 【P0】

- Provider 字段：`名称`、`协议类型`（openai_compatible / anthropic / ollama）、`base_url`、`api_key`（加密存储）、`备注`、`启用状态`。
- 内置常见 Provider 模板（OpenAI、DeepSeek、智谱、Moonshot、Anthropic、Ollama 本地），选择模板自动填充 base_url 与协议。
- 支持任意 OpenAI 兼容的自定义中转/代理服务。

#### FR-7 Model 管理 【P0】

- Model 字段：`名称（显示用）`、`所属 Provider`、`上游模型 ID`（如 `gpt-4o`）、`默认参数`（temperature、max_tokens、top_p、超时秒数、重试次数）、`启用状态`。
- 支持对单个 Model 发起**连通性测试**（发送一条固定测试消息，返回成功/失败与延迟）。

### 4.3 编排层（策略引擎）

#### FR-8 策略框架 【P0】

- 策略以接口抽象：输入（原始请求 + Pipeline 配置），输出（最终答案 + 各阶段明细）。新策略以插件形式注册，不改动网关代码。
- 本期注册并实现唯一策略：`council`。

#### FR-9 council 策略（多模型 + 裁判）【P0】

执行流程：

1. **成员阶段**：将原始 `messages` 并发发给 Pipeline 中全部启用的成员模型（默认参数 + 请求参数合并），每个成员独立、完整地回答；单成员超时/失败不阻塞其他成员。上游一律流式调用，超时指"等待首个字符/字符间空闲"而非总时长（复杂问题长生成不超时）。
2. **裁判阶段**：将「原始对话 + 每个成员的答案（标注来源模型名）」组装进裁判 Prompt，由裁判模型生成最终答案。
   - 裁判 Prompt 模板可在 Pipeline 中自定义，提供默认模板（要求裁判比较各答案、去伪存真、给出综合最优回答）。
   - 默认模板支持变量占位符：`{{original_messages}}`、`{{candidate_answers}}`。
3. **响应组装**：以 OpenAI 格式返回裁判答案；`usage` 汇总所有调用的 token 数。

容错规则（Pipeline 可配置）：

| 场景 | 行为（可配置项） |
| --- | --- |
| 个别成员失败/超时 | 默认：跳过该成员，只要 ≥1 个成员成功即继续（可配置为"任一失败即整体失败"） |
| 全部成员失败 | 返回 502，附各成员失败原因 |
| 裁判失败 | 默认：降级返回第一个成功成员的答案，响应中标注 `degraded: true`（可配置为直接报错） |

#### FR-10 Pipeline（组合）管理 【P0】

- Pipeline 字段：`名称（即虚拟模型名，如 council-v1）`、`策略类型`（本期固定 council）、`成员模型列表`（有序、可重复、每项可覆盖默认参数）、`裁判模型`、`裁判 Prompt 模板`、`并发数上限`、`成员超时`、`容错策略`、`启用状态`。
- 校验规则：至少 1 个成员模型 + 1 个裁判模型；成员与裁判可为同一上游模型；虚拟模型名不得与真实模型名冲突时以 Pipeline 优先。

### 4.4 Web UI（管理后台）

#### FR-11 Provider / Model 配置页 【P0】

- Provider、Model 的增删改查、启用/停用；API Key 输入框密文显示、保存后不再回显明文（只显示尾 4 位）；一键连通性测试并显示延迟。

#### FR-12 Pipeline 配置页 【P0】

- Pipeline 列表 + 新建/编辑表单：勾选成员模型（可排序、可设单项参数覆盖）、选择裁判模型、编辑裁判 Prompt 模板（带默认模板与占位符说明）、配置超时与容错。
- 提供"试运行"按钮：在 UI 中发起一次对话测试，直接展示各成员答案、裁判输入与最终答案（等价于一次带日志的调用）。

#### FR-13 调用日志 / Trace 查询页 【P0】

- 列表页：按时间范围、Pipeline（虚拟模型）、成员模型、调用状态（成功/失败/降级/取消）、关键字（匹配原始输入内容）筛选；展示请求时间、虚拟模型、耗时、总 token、状态。
- 详情页（单次请求 Trace 视图，按时间线展示）：
  1. **原始请求**：完整 messages 与参数；
  2. **每次成员调用卡片**：模型名、实际发出的入参（含合并后参数）、完整输出、耗时、token、状态/错误信息；
  3. **裁判调用卡片**：组装后的裁判 Prompt 全文（即裁判模型的输入）、输出（最终答案）、耗时、token；
  4. **最终响应**：返回给客户端的内容与总耗时。
- 日志支持手动清空与按保留天数自动清理（默认 30 天，可配置）【清理 P1】。

#### FR-14 服务设置页 【P1】

- 查看服务地址与对外 API Key（可重置）、日志保留天数、系统运行状态（版本、启动时间、存储占用）。

#### FR-15 统计面板 【P2】

- 按天/按 Pipeline / 按成员模型统计：请求数、成功率、P50/P95 延迟、token 消耗。

### 4.5 日志与可观测性

#### FR-16 调用明细记录 【P0】

- 每次对外请求生成一条主记录（request_id），每次上游模型调用生成一条子记录，父子关联。
- 记录字段见 5.2 数据模型；**输入输出保存全文**（可配置关闭全文存储以节省空间【P1】）。

---

## 5. 数据模型（概要）

### 5.1 配置数据

```
providers:  id, name, protocol(openai_compatible|anthropic|ollama),
            base_url, api_key_encrypted, remark, enabled, created_at, updated_at

models:     id, provider_id, display_name, upstream_model_id,
            default_params(json: temperature/max_tokens/top_p/timeout/retry),
            enabled, created_at, updated_at

pipelines:  id, name(虚拟模型名, unique), strategy(默认 council),
            judge_model_id, judge_prompt_template,
            member_timeout_seconds, fault_tolerance(json), max_concurrency,
            enabled, created_at, updated_at

pipeline_members: id, pipeline_id, model_id, sort_order,
                  param_overrides(json)
```

### 5.2 日志数据

```
request_logs:      id(request_id), pipeline_name, client_model_field,
                   request_messages(json), request_params(json),
                   response_content, response_finish_reason,
                   status(success|degraded|failed|client_cancelled),
                   total_duration_ms, total_prompt_tokens,
                   total_completion_tokens, client_ip, created_at

model_call_logs:   id, request_id, role(member|judge), model_id,
                   upstream_model_id, provider_name,
                   request_payload(json, 含合并后参数/裁判Prompt全文),
                   response_content, status, error_message,
                   duration_ms, prompt_tokens, completion_tokens, created_at
```

---

## 6. 非功能需求

| 编号 | 类别 | 要求 |
| --- | --- | --- |
| NFR-1 | 延迟 | 成员阶段并发调用，端到端延迟目标 ≈ max(成员延迟) + 裁判延迟；成员超时（TTFT/空闲，非总时长）默认 60s 可配置。 |
| NFR-2 | 并发 | 支持至少 20 并发对外请求（本地场景）；成员调用按 Pipeline 并发上限受控。 |
| NFR-3 | 安全 | 上游 API Key 加密存储、UI 不回显明文；服务自身 API Key 哈希比对；管理 UI 默认仅监听 127.0.0.1。 |
| NFR-4 | 可靠性 | 单上游故障不影响整体服务；上游调用支持配置重试（默认 1 次，指数退避）。 |
| NFR-5 | 可扩展 | 策略接口、Provider 协议适配均为插件式注册；新增一种聚合策略不修改网关与 UI 主流程。 |
| NFR-6 | 部署 | 本地一键部署：docker-compose 一条命令拉起；或提供单进程模式（内嵌 UI，SQLite 存储，零外部依赖）。 |
| NFR-7 | 存储 | SQLite 单文件；日志全文存储下 1 万条请求级别无性能问题；提供保留期自动清理。 |

---

## 7. 接口示例

### 7.1 对外调用（客户端视角与 OpenAI 完全一致）

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer <服务Key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "council-v1",
    "messages": [{"role": "user", "content": "用一句话解释量子纠缠"}],
    "temperature": 0.7
  }'
```

响应（示意）：

```json
{
  "id": "chatcmpl-req_xxx",
  "object": "chat.completion",
  "model": "council-v1",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "（裁判模型产出的最终答案）"},
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 1830,
    "completion_tokens": 156,
    "total_tokens": 1986
  }
}
```

> `usage` 为全部成员调用 + 裁判调用的 token 总和；如需区分明细，可在 Web UI 的 Trace 详情中查看。

### 7.2 裁判 Prompt 默认模板（示意）

```
你将看到一个用户的问题，以及多个 AI 模型分别给出的回答。
请综合比较这些回答：找出相互印证的关键信息，识别其中的错误或矛盾，
然后基于最可靠的信息，给出一个比任何单个回答都更准确、完整的最终回答。

【用户对话】
{{original_messages}}

【各模型回答】
[模型A/gpt-4o]: ...
[模型B/deepseek-chat]: ...

请直接输出最终回答，不要复述过程。
```

---

## 8. 里程碑建议

| 阶段 | 内容 | 交付物 |
| --- | --- | --- |
| M1 | API 网关 + Provider/Model 接入 + 透传调用 + 基础日志 | curl 可打通单个上游模型 |
| M2 | council 策略（并发成员 + 裁判 + 容错）+ 流式 | `council-v1` 虚拟模型可用 |
| M3 | Web UI：Provider/Model/Pipeline 配置 + 连通性测试 + 试运行 | 全功能可配置 |
| M4 | Trace 日志查询页 + 服务设置 + 部署脚本（docker-compose） | 完整交付 |

## 9. 验收标准

1. 使用任意 OpenAI SDK，将 base_url 指向本服务、model 填 Pipeline 名，能正常完成流式与非流式对话。
2. Web UI 中新增 2 个不同 Provider 的模型，组成 Pipeline 并试运行成功；停用其中 1 个成员后请求仍成功（容错生效）。
3. 在日志页可查到该次请求的完整 Trace：原始输入、每个成员的输入/输出、裁判 Prompt 全文与输出、耗时与 token。
4. 上游 Key 在数据库中加密存储、UI 中不可见明文。
5. docker-compose 一条命令启动全部功能。

## 10. 未来扩展（不在本期）

- 更多策略：多数投票（Voting）、加权融合、级联（前一层输出作为后一层输入的 MoA）、按问题分类动态路由。
- 多租户与按 Key 配额计费；RAG / 工具调用在聚合链路中的支持。
- 成员答案质量自动评估，反哺权重调整。
