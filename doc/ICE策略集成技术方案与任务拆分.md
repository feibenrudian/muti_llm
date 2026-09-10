# 迭代共识集成（ICE）策略集成技术方案与任务拆分（T32–T37）

| 文档版本 | v1.1 |
| --- | --- |
| 日期 | 2026-09-07 |
| 上游文档 | [requirements.md](../requirements.md)、[tech-plan.md](../tech-plan.md)（编号、DoD、测试纪律全部继承） |
| 背景材料 | [迭代共识集成（ICE）技术方案文档.md](./迭代共识集成（ICE）技术方案文档.md)（ICE 算法机制与公开基准的**背景阅读**，不作为实施依据） |
| 状态 | 初稿（待评审） |

---

## 0. 文档定位

### 0.1 与既有文档的关系

- 本文是 tech-plan 的**增量分册**：新增第二种聚合策略 `ice`（迭代共识集成）。任务编号自 **T32** 起、设计决策编号自 **D9** 起，分别延续 tech-plan 的 T01–T31 与 D1–D8。
- 完成定义（DoD）继承 tech-plan §0：每个任务的全部验收用例（UT/AE/UE）通过且回归 `make test` 全绿。
- 背景材料文档描述的是外部通用框架（llm-ensemble / multi-agent），其机制与本项目架构的取舍对照见**附录 A**；两文冲突处以本文为准。
- 本策略不引入任何新依赖：Python 3.12+ / FastAPI / SQLAlchemy 2 / httpx 既有栈内实现，密钥仍走 Fernet 加密落库，不新增任何环境变量或外部服务。

### 0.2 requirements.md 同步修订清单（随 T32 一并提交）

| 位置 | 修订 |
| --- | --- |
| 1.3 非目标第 3 条 | 改为"其他聚合策略（投票、加权、级联）仍不做；ICE 为本期唯一新增策略" |
| 2. 术语表 | 新增：**ICE（迭代共识集成）**——成员多轮迭代改进 + 仲裁者逐轮评论并判断共识，收敛后由仲裁者产出最终答案的策略 |
| FR-8 | "本期注册并实现唯一策略 council" 改为"注册 council 与 ice 两种策略" |
| FR-10 | Pipeline 字段增加 `strategy_params`（按策略校验的 JSON 参数） |
| FR-13 | Trace 详情页成员卡片带轮次标签、裁判卡片支持多轮评论 |
| 6. NFR-1 | 成员超时默认值由 60s 订正为 120s（与既有代码 orm/schemas 一致，原文档滞后）；补充：ICE 端到端延迟 ≈ Σ各轮 max(成员延迟) + Σ裁判延迟，见本文 §9 预算 |

---

## 1. 方案总览

### 1.1 目标与非目标

**目标（v1）**

- G-ICE-1：注册第二种策略 `ice`：Pipeline 配置 `strategy=ice` 即可用，对外仍是 OpenAI 兼容虚拟模型（如 `ice-v1`）；网关非流式主流程零改动，流式仅新增迭代分派分支（D15），council 路径逐字节不变。
- G-ICE-2：迭代语义——成员并发出第 0 轮答案 → 仲裁者评论并判断共识（结构化 JSON）→ 未共识则成员基于评论改进、仲裁者再评 → 循环直至共识/停滞/轮数上限 → 仲裁者同会话终局裁决产出最终答案。
- G-ICE-3：完整 Trace：每个成员的每一轮、每轮仲裁评论、终局裁决全部落库（带轮次），Web UI 可视。
- G-ICE-4：流式请求下最终答案 SSE 转发，迭代期间发送 SSE 注释行保活（OpenAI SDK 忽略、字节可保活）。

**非目标（v1 明确不做，理由见附录 A）**

- 不做 embedding 语义相似度（共识判断、停滞检测均不依赖）。
- 不做成员结构化 key_facts 投票（背景文档三层漏斗的第 1、2 层）。
- 不做反向验证 / 搅局者注入、网络拓扑（ring/chain）、自定义成员改进模板、按问题难度动态轮数。
- 不引入 llm-ensemble 等外部库，不接 LangSmith（用自研 Trace）。

### 1.2 ICE 与 council 的关系

ICE 是 council 的**多轮泛化**，最大化复用现有机制：

| 机制 | council（现状） | ICE（本方案） |
| --- | --- | --- |
| 成员第 0 轮请求 | 原始 messages 原样并发（`run_members`） | **逐字节同构**，直接复用 |
| 裁判第一段 | 评论（`DEFAULT_CRITIQUE_TEMPLATE`，自由文本） | 评论 + 共识判断（同模板骨架 + JSON 输出段，D9） |
| 裁判会话 | 评论 → 最终答案两段同会话（T31） | 逐轮评论（JSON）→ 终局裁决，N 段同会话（D10） |
| 最终指令 | `judge_prompt_template` 仅作用指令轮 | 同 |
| 候选答案匿名 | 【回答 N】不带模型名 | 同（评论与改进指令均匿名） |
| 退化性 | — | `max_rounds=1` 或第 0 轮即共识时，调用次数与成本**等同 council**（M+2 次调用） |

### 1.3 执行流程

```
客户端请求（model=ice-v1，stream 可选）
   │
   ▼
┌─ 第 0 轮 ──────────────────────────────────────┐
│  M 个成员并发：原始 messages 原样（同 council）  │
│  仲裁者评论+共识判断（JSON，会话第 1 轮）         │
│    ├ 共识 且 confidence ≥ 阈值 ───────────────┐ │
│    └ 未共识 ↓（失败成员整场退出）              │ │
├─ 第 k 轮（k = 1 … max_rounds-1）────────────┤ │
│  M 个成员并发：延续自身会话 + 改进指令（匿名评论）│ │
│  仲裁者再评论+判断（同会话续轮，只带最新答案）    │ │
│    ├ 共识 / 停滞 / 评论失败 ──────────────────┤ │
│    └ 继续 ↓                                  │ │
└──────────────────────────────────────────────┼─┘
                                               ▼
                        终局裁决（同会话 + judge_prompt_template）
                          │ 非流式：聚合为 JSON 响应
                          │ 流式：本阶段 SSE 转发（迭代期间发 `: ice round k/N` 注释行）
                          ▼
              OpenAI 兼容响应；usage = 全部调用之和；degraded 按容错矩阵
```

---

## 2. 关键设计决策（D9–D16，延续 tech-plan D1–D8）

| # | 决策 | 理由 |
| --- | --- | --- |
| D9 | **共识判断不依赖 embedding**：仲裁者每轮输出受约束 JSON `{"consensus", "confidence", "critique"}`，是唯一共识机制（即背景文档三层漏斗仅保留第 3 层） | 需求非目标"不做 Embedding"；零新依赖、零新 Provider 类型；**全部分支由 LLM 输出驱动 → 快照回放天然确定**（测试命门） |
| D10 | **仲裁者单会话贯穿全程**：第 0 轮评论 → 各轮评论（JSON 作为 assistant 轮）→ 终局裁决；每轮 user 轮只携带最新答案，原对话只在会话第 1 轮出现 | T31 同会话模式的 N 段泛化；控 token；`judge_prompt_template` 语义不变 |
| D11 | **兜底 = 仲裁者终局裁决，不做多数派投票**：轮数耗尽、停滞、迭代期评论失败，一律进入终局裁决调用（必要时标 degraded） | 文本答案无客观可比性，多数派不可判定（背景文档也未定义）；终局裁决复用 T31 机制且质量更高 |
| D12 | **成员迭代 = 延续成员自身会话**：`原始 messages → assistant(该成员上轮答案) → user(改进指令+匿名评论)`；改进指令模板内置不开放自定义 | system prompt 与多轮上下文天然保留；与"评论模板内置"（T31）同一先例 |
| D13 | **`pipelines.strategy_params` 通用 JSON 列 + 策略自校验钩子** `Strategy.validate_params()`，管理 API 创建/更新时调用，非法 422 | 未来 voting/weighted 策略复用，不逐策略加列；校验归策略自己，admin 保持通用 |
| D14 | **`model_call_logs` 增可空列 `round`**；role 复用 `member / judge_critique / judge`，不新增枚举；启动时轻量补列（PRAGMA 检测 + ALTER TABLE ADD COLUMN） | Trace 轮次维度最小改动；create_all 不会给已存表加列，D2（不引 Alembic）前提下的 v1 折衷；旧行 round 为 NULL 语义为"无轮次" |
| D15 | **流式：迭代阶段全部非流式聚合，仅终局裁决流式转发**；迭代期间向客户端发 SSE 注释行 `: ice round k/N`（k 从 1 起）；`strategy_params.progress_comments` 默认 true 可关；网关按"流式计划是否携带迭代状态"分派，council 路径零改动 | 复用 D4"成员非流式 + 裁判流式"语义并推广；注释行符合 SSE 规范（OpenAI SDK 忽略）但产生保活字节，避免多轮长静默触发客户端读超时 |
| D16 | **v1 范围裁剪**：不做 embedding 相似度、key_facts 投票、反向验证/搅局者、网络拓扑、动态轮数（对照见附录 A） | 控制开发与测试面；核心价值（多轮迭代 + 共识门 + 终局裁决）不受影响 |

---

## 3. 数据模型与配置变更

### 3.1 表结构（增量）

```
pipelines:       + strategy_params(json, default {})     # 按策略校验的参数
model_call_logs: + round(integer, nullable)              # ICE 迭代轮次（0 起）；终局裁决行、council/透传行均为 NULL
```

- ORM 同步修改 + `db.py` 新增 `_ensure_schema()`：启动时 `PRAGMA table_info` 检测缺列 → `ALTER TABLE … ADD COLUMN`（幂等）。新建库由 create_all 直接建全。
- `CallOutcome` 增加 `round_no: int | None`，进 `to_log_kwargs()`；`logging_svc.record_call()` 增加 `round_no` 参数（默认 None）；Trace 详情 API 的 call 行增加 `round` 字段（内部传参名 `round_no` 与 DB 列/API 字段名 `round` 为同一语义，勿再引入第三个名字）。

### 3.2 ICE 策略参数（`strategy_params`）

| 参数 | 类型/范围 | 默认 | 说明 |
| --- | --- | --- | --- |
| `max_rounds` | int 1–5 | 3 | **成员生成轮数上限（含第 0 轮）**。=1 时行为退化等价 council |
| `confidence_threshold` | float 0–1 | 0.8 | 终局条件：consensus=true 且 confidence ≥ 阈值 |
| `stagnation` | bool | true | 连续两轮未共识且 confidence 不升 → 提前终局（§4.3） |
| `progress_comments` | bool | true | 流式迭代期间是否发送 SSE 注释行 |

- 校验由 `IceStrategy.validate_params()` 实现（越界/类型错 → 422，缺省项自动填充后回显）；`council` 的 `validate_params()` 拒绝非空参数（"该策略无可配参数"）。
- 成员数校验沿用 ≥1（单成员 ICE = 自我改进循环，合法）；**配置建议** M≥2 且成员来自不同 Provider（异构性是 ICE 收益来源），不作为硬校验。
- `GET /api/admin/meta/strategies` 返回已注册策略名列表，供前端下拉。

---

## 4. 执行流程详述

### 4.1 主流程（修正版伪代码，接口与现有代码对齐）

```python
async def run(self, ctx) -> StrategyResult:
    params = self.validate_params(ctx.pipeline.strategy_params)   # 默认值已填充
    outcomes: list[CallOutcome] = []

    # 第 0 轮：成员原始请求（复用 council 执行器：并发/限流/容错/TTFT 超时）
    round0 = await run_members(ctx)
    outcomes += round0
    ok = [o for o in round0 if o.status == "success"]             # 失败成员整场退出（同 council skip）
    if not ok:
        raise AllMembersFailed(...)                               # 502，附各成员摘要

    session: list[dict] = []                                      # 仲裁者会话（D10）
    critique = await self._run_round_critique(ctx, ok, session, round_no=0)
    outcomes.append(critique)
    if critique.status != "success":
        return self._critique_failed_at_round0(...)               # 容错矩阵：等同 council 今日语义
    verdict = parse_verdict(critique.response_content)            # 宽松 JSON 解析（§4.4）

    # 停滞序列只收解析成功的轮次：解析失败的 confidence=0.0 占位值不进序列（§4.3/§4.4）
    round_no, conf_history = 0, [verdict.confidence] if verdict.parsed else []
    while not self._should_finish(verdict, round_no, conf_history, params):
        round_no += 1
        refined = await self._run_refine_round(ctx, ok, verdict.critique, round_no)  # 与 ok 同序，每成员恰好一行
        outcomes += refined
        ok = [new if new.status == "success" else old             # 失败成员沿用上轮答案（§5），失败行照落
              for old, new in zip(ok, refined)]
        critique = await self._run_round_critique(ctx, ok, session, round_no)        # 评论各成员最新答案
        outcomes.append(critique)
        if critique.status != "success":
            degraded = True                                       # 迭代期评论失败：跳过剩余轮，直接终局（D11）
            break
        verdict = parse_verdict(critique.response_content)
        if verdict.parsed:
            conf_history.append(verdict.confidence)
    else:
        degraded = False

    final = await self._run_final(ctx, session, ok, stream=...)   # 终局裁决（流式模式下本调用流式转发）
    outcomes.append(final)
    return StrategyResult(final_content=final.response_content,
                          usage=sum_usage(outcomes), degraded=degraded,
                          calls=outcomes)                         # 轮次经 CallOutcome.round_no 落 Trace
```

- **成员最新答案表**：`ok` 初始为第 0 轮成功成员，此后每轮用 refine 结果按位更新（成功换新的、失败留旧的），第 k 轮评论与终局裁决始终面对各成员**最新成功轮**答案——与 §5 容错矩阵一致。
- **客户端请求参数**（`ctx.user_params`）作用于全部 R+1 次裁判调用（每轮评论 + 终局裁决），成员各轮不用——FR-9"客户端参数仅作用于裁判"的自然推广。该约定进请求体、影响快照 hash，T37 录制脚本必须一致。
- **StrategyResult 泛化**：现有 `members/critique/judge` 单值字段放不下 ICE 的 N 条轮评论 + 终局，统一改为 `calls: list[CallOutcome]`（T32 实施，council 适配后对外行为不变）。

### 4.2 提示词模板（全文，均为内置常量）

**第 0 轮仲裁评论 + 共识判断**（`ICE_CRITIQUE_TEMPLATE`，复用 `render_judge_prompt` 渲染，候选答案匿名）：

```
你将看到用户的问题，以及多个 AI 模型分别给出的回答。
请逐个评价每个回答的优劣：指出事实错误、关键信息遗漏、逻辑或表述问题，并给出简短的可信度结论。
随后判断这些回答是否已形成共识。

【用户对话】
{{original_messages}}

【各模型回答】
{{candidate_answers}}

严格按以下 JSON 格式输出，不要输出 JSON 以外的内容：
{"consensus": true或false, "confidence": 0到1的小数, "critique": "评论文本"}
- consensus：剔除表述差异后，各回答的核心结论是否一致；
- confidence：这组回答整体一致与可信的程度；
- critique：按【回答 1】【回答 2】… 顺序逐条评论，聚焦下一轮改进方向，不评价模型本身，不复述回答原文。
```

**第 k≥1 轮仲裁评论**（同会话续轮，user 轮只携带最新答案）：

```
各模型已基于你的上一轮评论改进了回答。最新回答如下：

{{candidate_answers}}

请再次逐条评论最新回答，并判断是否已形成共识。严格按上轮相同的 JSON 格式输出。
```

**成员改进指令**（`ICE_REFINE_TEMPLATE`，第 k≥1 轮成员消息 = `原始 messages + assistant(该成员上轮答案) + user(本指令)`）：

```
以上是你此前对用户问题的回答。一位评审专家综合各匿名回答给出了如下评论：

{{critique}}

请批判性地审视该评论与你的回答：识别并吸收评论指出的正确信息与被你忽略的细节，改进你的回答。
直接输出更新后的完整回答，不要提及修改过程。
```

**终局裁决指令**：复用现有 `DEFAULT_JUDGE_TEMPLATE`（"请结合你上面给出的评论……给出最终回答"），多轮语境下语义自洽；`pipeline.judge_prompt_template` 仍仅作用于本指令轮。

### 4.3 共识 / 停滞 / 终局规则（全部由 JSON 驱动，无相似度计算）

- **终局条件**（`_should_finish`，任一满足即进入终局裁决）：
  1. `consensus == true` 且 `confidence >= confidence_threshold`；
  2. 当前已是第 `max_rounds - 1` 轮（轮数耗尽）；
  3. 停滞（`stagnation=true` 时）：连续两轮未终局且 `confidence[k] <= confidence[k-1]`（不升）；confidence 序列只含 JSON 解析成功的轮次，解析失败的 0.0 占位值不参与比较（避免假停滞提前终局，§4.4）。
- 停滞检测**不依赖 embedding**，来源即仲裁者 JSON 的 confidence 序列，回放确定。
- 轮次编号全程 **0 起**（成员轮、评论轮、Trace round 同源），文档与 UI 统一口径。

### 4.4 JSON 解析规则（宽松，失败方向固定为"多迭代"）

1. 剥 Markdown 代码围栏；截取首个平衡的 `{…}` 片段；`json.loads`。
2. 字段缺省容错：`consensus` 非 bool 或缺失 → false；`confidence` 非法 → 0.0；`critique` 缺失 → 原始全文。
3. 完全解析失败 → `consensus=false, confidence=0.0, critique=原文`，照常进入下一轮——**宁可多迭代一轮，绝不误判共识提前停**（成本有 max_rounds 硬顶，质量风险无上限）。`parse_verdict` 返回需带 `parsed` 标志，解析失败轮次不进停滞检测的 confidence 序列（防止占位 0.0 触发假停滞）。

---

## 5. 容错矩阵

| 场景 | 行为 | 与 council 关系 |
| --- | --- | --- |
| 成员第 0 轮失败/超时 | 跳过该成员，≥1 成功即继续；`member_failure=strict` 时任一失败即整体失败 | 同 council（复用 `run_members`） |
| 成员第 k≥1 轮失败/超时 | **沿用该成员第 k-1 轮答案**进入本轮评论，失败调用行照落 Trace（round=k, status=failed）；下一轮该成员继续参与（可恢复） | 新增（ICE 特有） |
| 成员全部轮失败（第 k≥1 轮全失败） | 沿用全部上轮答案继续，不 502 | 新增 |
| 全部成员第 0 轮失败 | `AllMembersFailed` → 502 附各成员摘要 | 同 council |
| 仲裁者**第 0 轮**评论失败 | 非流式：默认降级返回首个成功成员答案 + `degraded:true`；`judge_failure=strict` → 502。流式：发生在开流前，按失败返回 | 同 council（AE-18-2/18-3 语义） |
| 仲裁者**第 k≥1 轮**评论失败 | 跳过剩余迭代，**直接终局裁决**，`degraded:true`；strict 模式非流式 502、流式终止流（已开流无法改状态码），日志 failed | 新增（D11） |
| 终局裁决调用失败 | 非流式：降级首个成功成员**最新轮**答案 + degraded / strict 502；流式中途失败：沿用现有 StreamOutcome 失败落库语义 | 同 council 推广 |
| 客户端断开 | 取消全部在飞轮次任务，`status=client_cancelled`；**已完成的轮次调用行照常落库** | 同 council 推广 |
| JSON 解析失败 | 视为未共识继续迭代（§4.4） | 新增 |

---

## 6. 流式语义（D15 展开）

```
stream=true 时序：
客户端 ── 请求 ──▶ 网关
                 ├─ 第 0 轮成员并发 + 评论（开流前；失败可正常返回错误 JSON/降级，同 council）
                 ├─ 首字节：`: ice round 1/N`（SSE 注释行，progress_comments=true 时）
                 ├─ 第 k 轮：成员并发 → `: ice round k+1/N` → 评论
                 ├─ 终局裁决：SSE data chunk 流式转发（首 role 事件 → content 增量 → finish → [DONE]）
```

- 注释行格式：`: ice round {k}/{N}\n\n`，k=已完成的成员轮序号（1 起）、N=`max_rounds`。SSE 规范注释，OpenAI 官方 SDK 解析器忽略；`progress_comments=false` 时完全不发。**注释行仅从进入第 1 轮迭代起发射**：第 0 轮（成员并发 + 评论）全部在开流前完成，该阶段客户端静默时长与 council 相同，无保活覆盖。
- 网关分派：策略 `prepare_stream` 返回的 StreamPlan 若**不带迭代状态**（council、ICE 第 0 轮即共识）→ 走现有 `event_stream` 路径，逐字节不变；**带迭代状态**（ICE 需继续轮次）→ 走新增迭代路径（注释行 + 轮次执行 + 终局流式）。council 回归用例保证零行为变化。
- 取消传播：断开时取消在飞轮次任务，后台任务落已完成轮次行 + `client_cancelled`（沿用现有 background persist 机制，扩展到迭代期）。
- 非流式请求无进度通道，整段等待（延迟预算见 §9）。

---

## 7. 测试策略

### 7.1 确定性论证（快照回放的命门）

- ICE 的全部分支（共识/未共识/停滞/终局）由**仲裁者 JSON 输出**驱动，测试中该输出来自快照回放 → 回放期完全确定。
- 无 embedding、无随机数、无时间戳进请求体；每轮请求 = 模板 + 快照答案的确定性拼接。
- `record_scenarios.py` 直接 import `strategies/ice.py` 的渲染函数构造每轮请求（沿用 council.py 的"录制与运行时逐字节一致"机制）。

### 7.2 快照场景（T37 录制，命名沿用 `{场景}__{hash}.json` 平铺）

| 场景 | 链路形态 | 验证点 |
| --- | --- | --- |
| `ice_r0_consensus` | M=2：成员×2 → 评论(共识) → 终局 | 第 0 轮共识快路径，M+2 次调用 |
| `ice_refine_consensus` | 成员×2 → 评论(未共识) → 改进成员×2 → 评论(共识) → 终局 | 迭代轮消息形态、会话延续 |
| `ice_max_rounds` | max_rounds=2 两轮未共识 → 终局 | 轮数耗尽非降级 |
| `ice_stream` | 同 `ice_refine_consensus` 请求 + stream=true | 终局 chunk 序列、注释行时序 |

- 容错用例不经专门录制：复用上述场景请求 + SRS 注入。**SRS 小扩展**：`/_test/config` 支持 `fail_at_calls: {"model-b": [2]}`（对该模型的第 n 次调用失败，1 起）——现 `fail_times` 只能"前 N 次连续失败"，无法表达"第 1 轮成功、第 2 轮失败"。
- 重录注意：场景轮数由真实 LLM 的 consensus 决定，`make record` 重录可能翻转分支——场景断言写"与快照一致"而非硬编码轮数；翻转时同步更新场景定义。

### 7.3 UT 打桩口径

策略层 UT 一律打桩适配器（不起网络），桩按调用序返回预设 JSON/失败，断言：调用序列与消息形态、轮次控制、容错分支、usage 求和、Trace 行结构。覆盖率维持 `app/strategies` ≥ 80%。

---

## 8. 任务拆分与验收用例（T32–T37）

> 格式与 tech-plan §3 一致：每任务 = 功能 + 测试，用例编号 UT/AE/UE-xx-n。

### T32 数据模型与策略参数框架

产出：`orm.py` 增 `pipelines.strategy_params`、`model_call_logs.round`；`db.py` `_ensure_schema()` 轻量补列；`schemas.py` Pipeline Create/Update/Out 增字段；`strategies/base.py` 增 `validate_params` 类方法钩子（默认实现：council 拒绝非空参数）；`StrategyResult` 的 `members`/`critique`/`judge` 单值字段泛化为统一 `calls: list[CallOutcome]`（ICE 的 N 条轮评论 + 终局无法塞入单值字段；council 适配后对外行为不变）；`admin/pipelines` 创建/更新调用钩子（非法 422）；`admin/meta` 增 `GET /api/admin/meta/strategies`；`CallOutcome.round_no` → `to_log_kwargs` → `logging_svc.record_call` → Trace API `round` 字段全链贯通。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-32-1 | UT | ICE 参数校验 | 默认值自动填充（max_rounds=3 等）；max_rounds=6 / threshold=1.5 / 类型错 → ValueError；council 传非空参数 → 拒绝 |
| UT-32-2 | UT | 旧库补列 | 手工建只含旧列的表 → `_ensure_schema()` 后新列存在；旧行读回 strategy_params={}、round=None；二次执行幂等 |
| UT-32-3 | UT | round 落库 | record_call 带/不带 round_no 均正确往返 |
| AE-32-4 | AE | 管理API | 创建 strategy=ice 合法参数 → 201 回显含填充后的 strategy_params；非法参数/未知参数键 → 422；council 带 params → 422 |
| AE-32-5 | AE | meta | `GET /api/admin/meta/strategies` → 含 "council" 与 "ice" |
| AE-32-6 | AE | Trace API | 造含 round 的 call 行 → 详情响应带 round；旧 trace 行 round 为 null |

### T33 ICE 策略核心（打桩 UT）

产出：`strategies/ice.py`：`ICE_CRITIQUE_TEMPLATE`/`ICE_REFINE_TEMPLATE`、`parse_verdict()`、仲裁者会话管理、`_run_refine_round`（复用 run_members 的限流/容错骨架，消息为成员自身会话延续）、共识/停滞/终局控制、容错矩阵实现、`IceStrategy.run`。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-33-1 | UT | 第0轮即共识 | 桩评论 JSON consensus=true conf=0.9 → 总调用 M+2，无 refine 轮，非降级 |
| UT-33-2 | UT | 迭代后共识 | 第 0 轮 false → refine 轮成员消息 = 原始+assistant(旧答案)+user(改进指令，{{critique}} 已渲染) → 第 1 轮 true → 终局 |
| UT-33-3 | UT | 匿名性 | 仲裁评论输入为【回答 N】且不带模型名（复用 render_judge_prompt 断言） |
| UT-33-4 | UT | 仲裁者会话延续 | 第 k 轮评论请求含第 k-1 轮 JSON 为 assistant 轮；user 轮只含最新答案、不重复原对话 |
| UT-33-5 | UT | 轮数耗尽 | `stagnation=false`（或 confidence 逐轮递增但低于阈值，规避停滞条件）下恒 false → 恰好 max_rounds 轮成员 + 等量评论 + 1 终局，degraded=false |
| UT-33-6 | UT | 停滞检测 | conf 序列 [0.5, 0.5]（或下降）两轮未终局 → 提前终局，调用数少于 max_rounds 满轮 |
| UT-33-7 | UT | JSON 宽松解析 | 围栏/前后杂文可解析；完全非 JSON → consensus=false、critique=原文、继续迭代 |
| UT-33-8 | UT | 成员迭代轮失败 | 第 1 轮一成员失败 → 该成员沿用第 0 轮答案进评论、失败行保留(round=1)；第 1 轮全失败 → 沿用全部上轮答案不 502 |
| UT-33-9 | UT | 评论失败 | 第 0 轮评论失败 → 降级首个成功成员/strict 抛错；第 1 轮评论失败 → 终局裁决 degraded=true |
| UT-33-10 | UT | usage 汇总 | 全部调用（成员各轮+评论各轮+终局）token 求和 |

### T34 非流式端到端 + Trace + 换裁判重跑适配（AE 快照）

产出：网关零改动验证（策略注册即路由）；`rejudge_svc` 适配 ICE trace（answers = 各成员**最新成功轮**答案；重跑仍为单次两段式，不做多轮迭代——重跑目的是快速对比裁判模型质量，多轮重跑成本高且 UI 配对复杂；**重跑评论段使用 council 自由文本 `DEFAULT_CRITIQUE_TEMPLATE`**——给人看评论质量不需要共识 JSON，也避免 UI 折叠块展示裸 JSON，终局段仍用 `pipeline.judge_prompt_template`）；Trace 落库断言；SRS `fail_at_calls` 注入扩展（`snapshot_server/app.py`）。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-34-1 | UT | SRS 按序号注入 | `fail_at_calls: {"m-b": [2]}` → 该模型第 1 次调用成功、第 2 次 500、第 3 次起恢复；reset 清空 |
| AE-34-2 | AE | 端到端（快路径） | `ice_r0_consensus` → 200 content=快照终局答案；usage=(M+2) 快照之和；model=pipeline 名 |
| AE-34-3 | AE | 多轮 Trace | `ice_refine_consensus` → calls 顺序与角色：member(round0)×2 → judge_critique(round0) → member(round1)×2 → judge_critique(round1) → judge；round 字段正确；payload 全文入库 |
| AE-34-4 | AE | 轮数耗尽 | `ice_max_rounds` → 行数符合 max_rounds；status=success 非 degraded |
| AE-34-5 | AE | 容错注入 | 全成员失败 → 502 附摘要；第 0 轮评论失败 → 200 degraded:true content=首个成功成员答案；第 1 轮成员失败（fail_at_calls）→ 该行 failed 且终局正常 |
| AE-34-6 | AE | 换裁判重跑 | ICE trace rejudge → 录像断言裁判输入=各成员最新成功轮答案、评论段为 `DEFAULT_CRITIQUE_TEMPLATE` 渲染的自由文本（非共识 JSON）；judge_critique+judge_rerun 成对追加；原始 RequestLog 不变 |

### T35 流式：进度注释 + 能力分派 + 取消（AE）

产出：`StreamPlan` 支持携带迭代状态（子类或可选字段）；`IceStrategy.prepare_stream` 第 0 轮在开流前完成（错误语义同 council），共识即返回就绪终局计划；网关迭代路径（注释行发射 + 轮次执行 + 终局流式）；取消传播扩展到迭代期。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-35-1 | UT | 注释行格式 | 发射内容 `: ice round k/N\n\n`；data chunk 序列生成不受影响 |
| AE-35-2 | AE | 流式端到端 | `ice_stream` → data chunk 拼接=快照终局答案；末行 [DONE]；chunk model=pipeline 名 |
| AE-35-3 | AE | 注释行存在且无害 | 迭代场景输出含 `: ice round 1/2` 行；OpenAI 兼容 chunk 序列仍完整可解析 |
| AE-35-4 | AE | 注释可关 | progress_comments=false → 全程无 `:` 开头行 |
| AE-35-5 | AE | council 回归 | council 流式输出无注释行，AE-17-1..3 行为不变 |
| AE-35-6 | AE | 开流前失败 | 第 0 轮评论失败（流式）→ 返回错误 JSON 非 200（同 council 语义）。**opt-in 契约变化（T38）**：`stream_process=true` 时第 0 轮成员块已发出，评论失败转为流内终止（无 [DONE]），trace=failed |
| AE-35-7 | AE | 迭代期取消 | backend_live 断开 → client_cancelled；已完成轮次行已落库 |

### T36 Web UI（UE + 前端 UT）

产出：Pipelines 表单增 strategy 下拉（选项来自 meta/strategies，未知策略只读兜底）+ ICE 参数区（4 参数带默认值与范围校验）；TraceDetail：member 行按 round 分组并带"第 k 轮"标签（round=null 不显示）、`toTimeline`/`seedVersions` 泛化（最终行认领其之前未被认领的连续 critique 行；ICE 的 N 条轮评论全部归属原始裁判版本，重跑行各自配对自己的一条评论）；Playground 对 ICE 可用。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| UT-36-1 | UT | toTimeline 轮次分组 | 多轮 member 行按 round 分组且组内保序；null round 归单组；critique 行归裁判组按 round 排序 |
| UT-36-2 | UT | 版本配对泛化 | ICE：N 评论 + 1 最终 → 该版本关联 N 条评论；重跑行各关联自己的 1 条；judgeVersionCount 不因评论行膨胀 |
| UE-36-1 | UE | 建 ICE Pipeline | 选 strategy=ice → 参数区出现且为默认值；保存后列表与详情回显一致 |
| UE-36-2 | UE | 参数校验 | max_rounds 输入 6 → 前端拦截；绕过前端后端 422 提示可读 |
| UE-36-3 | UE | Trace 轮次展示 | ICE trace 详情：成员卡带"第 0/1 轮"标签、裁判卡内多轮评论折叠块逐轮可展开 |
| UE-36-4 | UE | Playground | ICE pipeline 试运行：多轮成员卡 + 最终答案正常展示 |
| UE-36-5 | UE | 换裁判重跑 | ICE trace 下拉换裁判 → 流式两段输出（评论+最终）实时呈现 |

### T37 快照补录与总回归

产出：`record_scenarios.py` 注册 §7.2 四个 ICE 场景（import ice 渲染函数保证逐字节一致）；`make record` 补录；回归与覆盖率核查。
| 编号 | 类型 | 用例 | 断言要点 |
| --- | --- | --- | --- |
| AE-37-1 | AE | 回放确定性 | 四个 ICE 场景在 replay 模式下二次运行全部命中（含多轮链路全部中间请求 hash 稳定） |
| AE-37-2 | AE | 总回归 | `make test && make e2e` 全绿；`app/strategies` 覆盖率 ≥ 80% 保持 |

---

## 9. 里程碑与成本/延迟预算

| 里程碑 | 任务 | 出口判据 |
| --- | --- | --- |
| M5 | T32–T34 | AE-34-2 通过：`ice-v1` 非流式可用，Trace 轮次完整落库 |
| M6 | T35–T37 | AE-35-2 + UE-36-3 通过：流式 + UI + 全量回归绿 |

**调用数与成本（诚实口径，含仲裁者）**：实际调用数 = `R×M + R + 1`（R=实际成员轮数 1..max_rounds，M=成员数）。

| 场景 | 轮数 R | 调用数（M=2，R×M+R+1） | 相对 council（M+2=4） |
| --- | --- | --- | --- |
| 第 0 轮即共识（简单问题） | 1 | 4 | 1.0×（完全等同） |
| 一轮改进后共识 | 2 | 7 | 1.75× |
| max_rounds=3 跑满 | 3 | 10 | 2.5× |

**延迟**：端到端 ≈ Σ各轮 max(成员延迟) + (R+1)×裁判延迟。max_rounds=3、成员 TTFT 预算 120s 的最坏静默可达数分钟——**建议 ICE Pipeline 配置更小的 member_timeout**，流式靠进度注释保活，非流式客户端需容忍长等待（写入 README 使用建议）。Token 侧：仲裁者会话逐轮累积评论 JSON，但 user 轮不重复原对话，增长为线性可控。

---

## 10. 风险与备注

1. **JSON 遵从率**：上游对"只输出 JSON"的遵从不保证；宽松解析的失败方向固定为"多迭代"（质量安全、成本有 max_rounds 硬顶）。录制时观察真实遵从情况，必要时在模板中加强约束后重录。
2. **进度注释兼容性**：OpenAI 官方 SDK 按 SSE 规范忽略注释行；个别自研严格解析器可能视为脏行——`progress_comments` 可关，AE-35-3 断言 data 流完整性。
3. **静默期**：非流式请求迭代期间无输出通道；流式注释行解决保活但部分客户端 UI 仍无进度显示（P2 可扩展为自定义事件，本期不做）。
4. **快照组合数**：ICE 场景请求数 = 轮数×(M+1)+1，全部由录制脚本经渲染函数构造；场景控制在 4 个，容错用例靠注入复用。
5. **列迁移**：ALTER TABLE ADD COLUMN 幂等 + UT-32-2 覆盖旧库路径；D2（无 Alembic）前提下的可接受折衷，v2 引入迁移工具时回收。
6. **重录分支翻转**：真实 LLM 的 consensus 值决定场景轮数，`make record` 重录可能改变链路形态；场景断言基于快照一致性而非硬编码轮数。
7. **背景文档的数字不背书**：1.2/7.1 的基准提升（GPQA +21.3pp 等）是外部论文条件下的结果，与本实现无对应关系，不写入任何验收标准。

---

## 附录 A：背景文档机制 → 本方案取舍对照

| 背景文档机制 | 本方案 | 理由 |
| --- | --- | --- |
| 第 1 层：embedding 余弦相似度（0.92/0.75） | **不做** | 需求非目标"不做 Embedding"；需新增 embedding 服务（新依赖/新 Provider 类型/新计费）；且相似度高不等于结论一致 |
| 第 2 层：成员 key_facts JSON 投票（≥2/3） | **不做** | 成员输出结构化 JSON 与改进轮"直接输出完整回答"的指令矛盾；自由文本事实的等价性判定又绕回相似度问题 |
| 第 3 层：仲裁者判断 | **保留并强化**：每轮评论+共识判断合并为一次结构化 JSON 调用（D9/D10） | 唯一可靠且可回放确定的机制；合并调用省一次仲裁成本 |
| 早停：平均相似度 > 0.98 | 改为 **confidence 停滞检测**（连续两轮不升，§4.3） | 原语义含糊（跨轮/跨模型未定义）且依赖 embedding |
| 反向验证 + 搅局者注入 | **不做（v1）** | 增加调用与终止条件复杂度，价值可用 max_rounds 上限近似；v2 若做需定义搅局后的终止保证 |
| 网络拓扑 fully_connected/ring/chain | **不做**：信息全部经仲裁者星型流转 | 背景文档算法本身也未使用 peer 直连，配置项是外部框架残留 |
| 兜底：多数派投票 > 仲裁强裁 > 置信度 > 安全输出 | 统一为**仲裁者终局裁决**（D11），成员失败沿容错矩阵（§5） | 多数派对文本不可判定；四级兜底与"API 不可用/轮数耗尽"两种成因混杂、且有不可达分支 |
| agents.yaml / llm-ensemble 库 / LangSmith | **不用**：`strategies/ice.py` 插件 + 自研 Trace | AGENTS.md 技术栈纪律（不引入清单外依赖）；NFR-5 插件式注册即达成的集成形态 |
| 执行模型 3 个异构 + Python 3.8+ | 成员数 ≥1（建议 ≥2 异构）/ Python 3.12+ | 与项目校验和运行时对齐 |
