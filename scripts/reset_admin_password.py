"""重置管理员/用户密码的运维脚本（多用户 T46 配套）。

用法（仓库根目录）：
    cd backend && uv run python ../scripts/reset_admin_password.py [用户名] [新密码]

用户名缺省为 admin；新密码缺省随机生成（打印一次）。服务运行中亦可执行（SQLite WAL）。
密码丢失恢复场景：启动日志未留存初始密码时，用本脚本重置后登录，再在 Web UI 顶栏改密。
"""

import asyncio
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


async def main() -> None:
    username = sys.argv[1] if len(sys.argv) > 1 else "admin"
    password = sys.argv[2] if len(sys.argv) > 2 else secrets.token_urlsafe(12)

    from app.auth_svc import change_password
    from app.db import create_db_engine, create_session_factory
    from app.repos import find_user_by_username
    from app.settings import settings

    engine = create_db_engine(settings.database_path)
    factory = create_session_factory(engine)
    async with factory() as session:
        user = await find_user_by_username(session, username)
        if user is None:
            print(f"用户不存在: {username}")
            raise SystemExit(1)
        await change_password(session, user.id, new_password=password, admin_reset=True)
        # 重置口令同步吊销其全部管理会话（与 API 重置同语义）
        from app.repos import revoke_user_sessions

        await revoke_user_sessions(session, user.id)
        await session.commit()
    await engine.dispose()
    print(f"已重置 {username} 的密码：{password}")
    print("请立即登录并在 Web UI 顶栏「改密」换掉。")


if __name__ == "__main__":
    asyncio.run(main())
