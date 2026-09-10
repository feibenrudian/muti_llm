# muti_llm — 多模型聚合 LLM 网关

对外提供 **OpenAI 兼容 API**，内部将请求分发给多个上游 LLM 并行回答，由裁判模型聚合出最终答案，配套 Web UI 完成 Provider/Model/Pipeline 配置、试运行与全链路 Trace 查询。

内置两种聚合策略：**council**（单轮成员并发 + 裁判两段式裁决）与 **ice**（迭代共识集成：成员多轮迭代改进 + 仲裁者逐轮评论判断共识，收敛后终局裁决；详见 [doc/ICE策略集成技术方案与任务拆分.md](./doc/ICE策略集成技术方案与任务拆分.md)）。

- 产品需求：[requirements.md](./requirements.md)
- 技术方案与任务拆分：[tech-plan.md](./tech-plan.md)
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

数据持久化在 docker volume `muti_data`（SQLite）。服务 API Key 首次启动自动生成（仅存哈希），在 Web UI「设置」页重置获取明文。

### 本机运行（service.sh）

```bash
./service.sh start     # 启动（Web UI 未构建时自动 npm run build），打印各端口
./service.sh stop      # 停止
./service.sh restart   # 重启
./service.sh status    # 查看运行状态与健康检查
MUTILLM_PORT=9000 ./service.sh start   # 自定义端口（默认 8000）
```

单进程同端口提供 Web UI（`/`）、OpenAI 兼容 API（`/v1`）、管理 API（`/api/admin`）；日志在 `run/server.log`。

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

## 测试体系（Record once, replay forever）

所有 E2E 测试不依赖真实 API：首次用真实 DeepSeek 固化「请求→响应」为快照（`backend/tests/snapshots/`），之后全部回放。详见 tech-plan 2.1。

```bash
make test    # 日常：快照回放，零费用零网络
make record  # 新增用例后补录快照（幂等：已命中的直接回放）
```
