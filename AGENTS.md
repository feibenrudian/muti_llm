# AGENTS.md —— AI 开发约定（改代码前必读）

本项目是"多模型聚合 LLM 网关"：对外提供 OpenAI 兼容 API，内部多模型并行回答 + 裁判模型聚合，配 Web UI 管理与 Trace 日志。

**两份权威文档，任何实现决策以它们为准：**

- [requirements.md](./requirements.md) —— 产品需求（功能、优先级、验收标准）
- [tech-plan.md](./tech-plan.md) —— 技术方案、任务拆分（T01–T29）、每任务验收用例编号

## 完成定义（DoD）

一个任务完成的唯一定义：**该任务的全部验收用例（UT/AE/UE）通过，且回归 `make test` 全绿**。用例编号写在测试函数的 docstring 首行，例如 `"""UT-03-1 快照命中(非流式)"""`。

## 技术栈（不要引入清单之外的重型依赖）

- backend：Python 3.12+ / FastAPI / SQLAlchemy 2 (async) / SQLite(WAL) / httpx / pydantic v2 / uv
- frontend：Vite + React 19 + TypeScript / Tailwind v4 / shadcn-ui(T20 引入) / TanStack Query(T20 引入) / Playwright
- 测试：pytest(+asyncio+cov) / vitest / Playwright

## 目录

```
backend/app/          业务代码（gateways/adapters/strategies/admin 等，见 tech-plan 1.2）
backend/tests/unit/   UT 用例
backend/tests/e2e_api/  AE 用例（ASGI 直连后端 + SRS，不起浏览器）
backend/tests/snapshot_server/  快照回放服务器 SRS（测试地基）
backend/tests/snapshots/        真实 LLM 快照数据（提交进库，严禁手改内容）
frontend/src/pages/   页面；frontend/e2e/ 为 Playwright 用例
```

## 常用命令

```bash
make install        # 首次：backend uv sync + frontend npm install + playwright chromium
make test           # 全量回归：后端 pytest + 前端 vitest+lint（快照回放，不触网）
make e2e            # 后端 AE + Playwright 全量
make record         # 真实调用 DeepSeek 录制/刷新快照（需要 backend/.env.test）
make dev-backend    # uvicorn --reload :8000
make dev-frontend   # vite dev server
```

## 代码规范

- Python：ruff（line-length 100，规则 E/F/I/UP/B）；全部函数带类型注解；模块级 docstring 一句话说明职责。
- TypeScript：eslint(typescript-eslint recommended)；严格模式；不用 any。
- 命名与结构照抄 tech-plan 1.2 的目录规划，新文件先确认规划里有没有位置，没有就在 PR/提交说明里说明理由。
- 注释只写"代码本身说不清的约束"，不写"这行在干什么"。

## 测试纪律（重要）

1. **快照 = 真实 LLM 的固化输入输出，严禁手改 `backend/tests/snapshots/` 下任何文件的内容**。行为变了就改代码后重跑 `make record` 重新录制。
2. **所有打到"上游 LLM"的测试必须经过 SRS**（replay 模式默认），任何测试不得直连真实付费 API；只有 `make record` 走真实调用。
3. 测试必须完全确定：请求体里不得出现时间戳、随机数、随机端口等，否则快照 hash 永远失配。
4. 新增 AE/UE 用例后，记得跑 `make record` 补录快照，再切回 `make test` 验证可回放。
5. 快照文件不含认证头；SRS 录像（`/_test/requests`）是断言"上游实际收到什么"的唯一手段。
6. **不要用 httpx2.MockTransport 模拟流式（async generator）响应体**——httpx2 内部断言会把它变成连接错误（T07 踩坑记录）。需要 mock 上游时一律线程起真实 HTTP 服务（参照 `tests/snapshot_server/` 与 `tests/mock_anthropic.py` 的模式）。

## 安全纪律

- `backend/.env.test` 含真实录制凭据：**不提交、不打印到日志、不写入任何源码或快照**。仓库公开前删除它。
- 上游 API Key 一律 Fernet 加密落库；任何 API 响应只回显掩码（尾 4 位）。
