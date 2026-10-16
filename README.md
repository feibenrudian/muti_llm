# muti_llm — 多模型聚合 LLM 网关

对外提供 **OpenAI 兼容 API**，内部将请求分发给多个上游 LLM 并行回答，由裁判模型聚合出最终答案，配套 Web UI 完成 Provider/Model/Pipeline 配置、试运行与全链路 Trace 查询。

内置两种聚合策略：**council**（单轮成员并发 + 裁判两段式裁决）与 **ice**（迭代共识集成：成员多轮迭代改进 + 仲裁者逐轮评论判断共识，收敛后终局裁决；详见 [doc/ICE策略集成技术方案与任务拆分.md](./doc/ICE策略集成技术方案与任务拆分.md)）。

支持**多用户**：管理员按人发 Key、按人授权模型、按人统计用量；一般用户可通过自助门户管理自己的 Key 与查看用量（详见下文[多用户与权限](#多用户与权限)）。

- 产品需求：[requirements.md](./requirements.md)
- 技术方案与任务拆分：[tech-plan.md](./tech-plan.md)
- 增量分册：[多用户管理需求文档](./doc/多用户管理需求文档.md) / [技术方案与任务拆分（T45–T52）](./doc/多用户管理技术方案与任务拆分.md)
- AI 开发约定：[AGENTS.md](./AGENTS.md)

## 快速开始

```bash
make install        # 后端 uv sync + 前端 npm install + Playwright chromium
make test           # 全量回归（后端 pytest + 前端 vitest/lint，快照回放，不触网）
make e2e            # 后端 AE + Playwright 全量（含生产模式冒烟）
make record         # 真实调用 DeepSeek 录制/刷新测试快照（需 backend/.env.test）
```

### 开发模式

```bash
make dev-backend    # uvicorn --reload :8000（API 文档 http://127.0.0.1:8000/docs）
make dev-frontend   # vite dev server :8001（/api 代理到 :8000，见 vite.config.ts）
```

### 生产部署（单进程 + SQLite）

```bash
docker compose up -d --build   # http://127.0.0.1:8000（UI 与 API 同进程）
```

数据持久化在 docker volume `muti_data`（SQLite）。首次启动自动创建内置管理员 `admin` 并生成随机初始密码，**打印在启动日志中一次**（也可用 `MUTILLM_ADMIN_PASSWORD` 显式指定）；密码丢失可用 `scripts/reset_admin_password.py` 重置。数据面 Key 见下文「多用户与权限」。

### 本机运行（service.sh）

```bash
./service.sh start     # 启动（Web UI 未构建时自动 npm run build），打印各端口与管理员初始密码
./service.sh stop      # 停止
./service.sh restart   # 重启
./service.sh status    # 查看运行状态与健康检查
MUTILLM_PORT=9000 ./service.sh start   # 自定义端口（默认 8000）
```

单进程同端口提供 Web UI（`/`）、OpenAI 兼容 API（`/v1`）、管理 API（`/api/admin`）、自助门户 API（`/api/portal`）；日志在 `run/server.log`。

## 使用

配置链路：**Providers（供应商）→ Models（模型）→ Pipelines（组合）**，之后客户端即可像调用 OpenAI 一样使用虚拟模型：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer <服务Key>" \
  -H "Content-Type: application/json" \
  -d '{"model": "council-v1", "messages": [{"role": "user", "content": "用一句话解释量子纠缠"}]}'
```

- `model` 填 Pipeline 名（如 `council-v1`，多模型聚合）或真实模型名（直接透传）
- 支持流式（`stream: true`，打字机输出来自裁判模型；空闲期每 15s 发 SSE 注释行心跳保活，OpenAI SDK 自动忽略）
- 过程流式：请求体加 `"stream_process": true`（或在「组合」页给 Pipeline 勾选「过程流式输出」作为默认，请求体显式 `false` 可临时关闭），成员/评论的产出会以 `reasoning_content`（DeepSeek 风格 think 通道）随流推出，正式答案仍走 `content`
- 客户端断开默认不中断上游（`MUTILLM_DETACH_ON_DISCONNECT=false` 可恢复"断开即取消"）：结果照常跑完落库，事后可在「调用日志」取回
- 每次请求的完整链路（原始输入 / 每个成员的输入输出 / 裁判 Prompt / 最终答案 / 首 token 与总耗时 / token）可在 Web UI「调用日志」查询，ICE 链路按轮次分组展示

### 命名空间网关（可选接入）

- `/v1/pipeline/*`：只认 Pipeline 虚拟模型；`/v1/route/{供应商}/*`：直接透传指定供应商（含上游新增的动态模型）
- 两类接入可在「设置」页分别开关；`GET /v1/models` 混合列出虚拟模型与真实模型

## 多用户与权限

- **角色**：管理员（admin）与一般用户（user）。管理台（`/login`）仅管理员可登录；未登录进站默认落到自助门户登录页（`/portal/login`）
- **用户 API Key**：每用户可持多把（`sk-mu-` 前缀），明文仅创建时展示一次，此后掩码 + 复制；停用/删除即时生效。**旧服务 Key 继续有效**（身份 = 内置 admin，既有客户端零改动）
- **模型授权**：按虚拟模型（Pipeline）/ 供应商 / 模型三类授权，取并集、默认拒绝、即时生效；管理员不受授权表约束。越权调用返回 403 `model_not_authorized`；Key 有效但用户被停用返回 403 `user_disabled`
- **用量归属**：全部请求与上游调用落库 `user_id` + 用户名快照；「调用日志」可按用户筛选，「用量统计」新增按用户 / 用户 × 模型视图与 CSV 导出
- **自助门户**（`/portal`）：一般用户登录后自助管理本人 Key、查看本人用量统计 / 调用记录 / 授权范围，可修改密码
- **安全**：口令 PBKDF2（per-user 盐）；Key 与会话令牌只存哈希；管理面（会话令牌）与数据面（Bearer Key）凭据互不通用；登录连续失败 5 次锁定 5 分钟；管理动作记录审计流水

运维兜底：`cd backend && uv run python ../scripts/reset_admin_password.py [用户名] [新密码]`（缺省重置 admin，新密码缺省随机生成打印一次）。

## 测试体系（Record once, replay forever）

所有 E2E 测试不依赖真实 API：首次用真实 DeepSeek 固化「请求→响应」为快照（`backend/tests/snapshots/`），之后全部回放。详见 tech-plan 2.1。

```bash
make test    # 日常：快照回放，零费用零网络
make record  # 新增用例后补录快照（幂等：已命中的直接回放）
```
