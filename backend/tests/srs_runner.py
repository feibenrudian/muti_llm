"""以固定端口启动挂载真实快照库的 SRS（Playwright webServer 用，tech-plan 2.2）。"""

import os
from pathlib import Path

import uvicorn

from tests.snapshot_server.app import create_app

PORT = int(os.environ.get("SRS_PORT", "9801"))
SNAPSHOTS_DIR = Path(__file__).resolve().parent / "snapshots"

if __name__ == "__main__":
    app = create_app(SNAPSHOTS_DIR, mode="replay")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
