import os
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api import admin, agent, attachment, auth, billing, compliance, feedback, invite, payment, session, tool
from app.core.config import settings
from app.core.db import SessionLocal, ensure_database, init_db
from app.seed import seed
from app.services import attachment as attachment_svc
from fastapi.staticfiles import StaticFiles

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
APP_STARTED_TS = str(int(__import__("time").time()))  # 进程启动时间戳：/admin 重定向用它做缓存破坏


def _worker_loop(worker_index: int = 0):
    """消费线程。多线程时只有 0 号线程跑 reaper/waitlist ——
    避免多线程并发回收导致重复退次（reaper 内部是「查-改」而非原子 CAS）。"""
    from app.worker.queue import dequeue
    from app.worker.runner import archive_stale_records, reap_stale_pending, run_invocation
    from app.worker.session_runner import reap_sessions, run_session_step

    is_leader = worker_index == 0
    # 每 12 次轮询（约 60s）跑一次 reaper；每 720 次（约 1h）跑一次 waitlist 月度触达检查
    tick = 0
    while True:
        try:
            job = dequeue(timeout=5)
            if job:
                kind = job.get("type")
                if kind == "tool_async":
                    run_invocation(job["invocation_id"])
                elif kind in ("session_start", "session_turn_start"):
                    run_session_step(job["session_id"], turn_id=job.get("turn_id"))
                elif kind == "session_resume":
                    run_session_step(
                        job["session_id"], turn_id=job.get("turn_id"), resume=job.get("resume")
                    )
            tick += 1
            if is_leader and tick % 12 == 0:
                n = reap_stale_pending()
                if n:
                    print(f"[reaper] reclaimed {n} stale pending invocation(s)")
                m = reap_sessions()
                if m:
                    print(f"[reaper] reclaimed {m} stale session(s)")
                from app.core.db import SessionLocal as _SL
                from app.services.billing import expire_grants

                _db = _SL()
                try:
                    e = expire_grants(_db)
                    if e:
                        print(f"[reaper] expired {e} quota grant(s)")
                finally:
                    _db.close()
                a = archive_stale_records()
                if a:
                    print(f"[reaper] auto-archived {a} stale record(s)")
            if is_leader and tick % 720 == 0:
                from app.core.db import SessionLocal
                from app.services.billing import notify_waitlist_for_new_month

                db = SessionLocal()
                try:
                    n = notify_waitlist_for_new_month(db)
                    if n:
                        # TODO: 订阅消息真实触达接入点（次月 1 号提醒开售）
                        print(f"[waitlist] notified {n} user(s) for new month")
                finally:
                    db.close()
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            print(f"[worker#{worker_index}] error: {e}")
            import time

            time.sleep(1)


def create_app() -> FastAPI:
    app = FastAPI(title="万宝AI助手 API")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def startup():
        ensure_database()
        init_db()
        try:
            attachment_svc.ensure_bucket()
        except Exception as e:  # noqa: BLE001
            print(f"[s3] ensure_bucket warning: {e}")
        db = SessionLocal()
        try:
            seed(db)
        finally:
            db.close()
        # 启动异步任务 worker（复用已有 Redis 作为队列）
        # 多轮会话会放大排队，故支持多消费线程（BLPOP 对多消费者原子安全）；
        # reaper/waitlist 只由 0 号线程执行，避免并发重复回收与重复退次。
        n_workers = max(1, int(settings.WORKER_CONCURRENCY or 1))
        for i in range(n_workers):
            threading.Thread(target=_worker_loop, args=(i,), daemon=True).start()
        print(f"[worker] started {n_workers} consumer thread(s)")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(agent.router, prefix="/api/agent", tags=["agent"])
    app.include_router(session.router, prefix="/api/agent", tags=["session"])
    app.include_router(attachment.router, prefix="/api/attachment", tags=["attachment"])
    app.include_router(tool.router, prefix="/api/tools", tags=["tools"])
    app.include_router(billing.router, prefix="/api/billing", tags=["billing"])
    app.include_router(payment.router, prefix="/api/payment", tags=["payment"])
    app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
    app.include_router(feedback.router, prefix="/api/feedback", tags=["feedback"])
    app.include_router(compliance.router, prefix="/api/compliance", tags=["compliance"])
    app.include_router(invite.router, prefix="/api/invite", tags=["invite"])

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", tags=["ui"], include_in_schema=False)
    def site_home():
        # 官网落地页（含 MaxKB 客服浮窗嵌入）
        return FileResponse(
            os.path.join(STATIC_DIR, "site", "index.html"),
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/admin", tags=["ui"])
    def admin_ui():
        # 重定向到带启动时间戳的 URL：每次发版浏览器视为新地址，彻底绕开旧缓存
        from fastapi.responses import RedirectResponse

        return RedirectResponse(f"/admin/home?v={APP_STARTED_TS}", status_code=302)

    @app.get("/admin/home", tags=["ui"])
    def admin_ui_home():
        # no-cache：页面每次重新验证 ETag，避免发版后浏览器用旧缓存
        return FileResponse(
            os.path.join(STATIC_DIR, "admin", "index.html"),
            headers={"Cache-Control": "no-cache"},
        )

    return app


app = create_app()
