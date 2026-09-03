.PHONY: install test test-backend test-frontend lint fmt e2e record dev-backend dev-frontend seed

install:
	cd backend && uv sync
	cd frontend && npm install && npx playwright install chromium

test-backend:
	cd backend && uv run pytest

test-frontend:
	cd frontend && npm run test && npm run lint

test: test-backend test-frontend

e2e:
	cd backend && uv run pytest tests/e2e_api
	cd frontend && npx playwright test
	cd frontend && npm run build && E2E_MODE=production npx playwright test

# 快照录制：真实调用 DeepSeek，生成/刷新 backend/tests/snapshots/（需 backend/.env.test）
record:
	cd backend && uv run python -m tests.record_scenarios

lint:
	cd backend && uv run ruff check . && uv run ruff format --check .
	cd frontend && npm run lint

fmt:
	cd backend && uv run ruff format . && uv run ruff check --fix .

dev-backend:
	cd backend && uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

dev-frontend:
	cd frontend && npm run dev

seed:
	@echo "TODO(T29): scripts/seed_demo.py 一键演示数据"
