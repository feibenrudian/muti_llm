FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS backend
WORKDIR /srv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/app ./app
# 与宿主同构：<root>/backend/app + <root>/frontend/dist
COPY --from=frontend /build/dist /srv/frontend/dist
ENV MUTILLM_HOST=0.0.0.0 MUTILLM_PORT=8000 MUTILLM_DATABASE_PATH=/data/muti_llm.db
VOLUME /data
EXPOSE 8000
CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
