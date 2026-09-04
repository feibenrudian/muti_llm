"""以固定端口启动挂载真实快照库的 SRS（Playwright webServer 用，tech-plan 2.2）。"""

import os
from pathlib import Path

import uvicorn

from tests.snapshot_server.app import create_app

PORT = int(os.environ.get("SRS_PORT", "9801"))
SNAPSHOTS_DIR = Path(__file__).resolve().parent / "snapshots"
# Playwright 全量回放提速：即时回放（不改变请求/响应内容，仅去掉 chunk 间等待）
DELAY_SCALE = float(os.environ.get("SRS_DELAY_SCALE", "1"))

if __name__ == "__main__":
    app = create_app(SNAPSHOTS_DIR, mode="replay", delay_scale=DELAY_SCALE)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
