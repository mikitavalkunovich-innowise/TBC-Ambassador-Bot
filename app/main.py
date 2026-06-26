"""
TBC Ambassador Bot — FastAPI application entry point.

Serves:
  - POST /bot/webhook  — Telegram webhook endpoint
  - GET  /admin/*      — Admin panel
  - GET  /media/*      — Uploaded file serving
  - GET  /health       — Railway health check
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from aiogram.types import Update
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.admin.router import router as admin_router
from app.core.config import get_settings
from app.core.database import async_session_factory
from app.core.storage import ensure_dirs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Bundled ambassador photos — committed to the repo so they are always present.
# Both files are copied to the uploads volume on startup if not already there.
_RESOURCES_DIR = Path(__file__).parent / "resources"
_BUNDLED_FILES: list[tuple[str, str]] = [
    ("ambassador_canonical.jpeg", "ambassador/ambassador_canonical.jpeg"),
    ("ambassador_face_crop.jpeg", "ambassador/ambassador_face_crop.jpeg"),
    ("card_promo.webp", "card_promo/card_promo.webp"),
]


def _ensure_bundled_ambassador_photo(uploads_path: Path) -> None:
    """Copy the canonical ambassador photos from the app package to the uploads volume."""
    import shutil
    for src_name, dst_rel in _BUNDLED_FILES:
        src = _RESOURCES_DIR / src_name
        dst = uploads_path / dst_rel
        if dst.exists():
            continue
        if not src.exists():
            logger.warning("Bundled file not found at %s", src)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        logger.info("Copied bundled file to %s", dst)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    settings = get_settings()

    # Ensure upload directories exist
    ensure_dirs()
    logger.info("Upload directories verified at %s", settings.uploads_dir)

    # Copy bundled ambassador photo to the uploads volume if it isn't there yet.
    # app/resources/ambassador_canonical.jpeg is committed to the repo and always
    # deployed with the code; /data/uploads/ is a persistent Railway volume that
    # starts empty on first deploy.
    _ensure_bundled_ambassador_photo(settings.uploads_path)

    # Seed default settings and try to initialize the bot
    async with async_session_factory() as session:
        from app.services.settings_service import seed_defaults, get as get_setting
        await seed_defaults(session)
        bot_token = await get_setting(session, "bot_token")

    if bot_token:
        try:
            from app.bot.instance import initialize
            bot, dp = await initialize(bot_token)

            # Register Telegram webhook
            await bot.set_webhook(
                url=settings.webhook_url,
                drop_pending_updates=True,
            )
            logger.info("Webhook registered: %s", settings.webhook_url)
            app.state.bot = bot
            app.state.dp = dp
        except Exception:
            logger.exception("Failed to initialize bot. Set bot_token in admin panel and restart.")
            app.state.bot = None
            app.state.dp = None
    else:
        logger.warning(
            "Bot token not configured. Go to /admin/settings to set it, then restart the service."
        )
        app.state.bot = None
        app.state.dp = None

    # Start background auto-purge task (48h cycle)
    from app.tasks.purge_task import run_purge_loop
    purge_task = asyncio.create_task(run_purge_loop(async_session_factory))

    yield

    # Shutdown background tasks
    purge_task.cancel()
    try:
        await purge_task
    except asyncio.CancelledError:
        pass

    # Shutdown bot
    if getattr(app.state, "bot", None):
        try:
            await app.state.bot.delete_webhook()
        except Exception:
            pass
        from app.bot.instance import shutdown
        await shutdown()
        logger.info("Bot shutdown complete")


app = FastAPI(title="TBC Ambassador Bot", lifespan=lifespan, docs_url=None, redoc_url=None)

# --- Static media files ---
settings = get_settings()
media_path = Path(settings.uploads_dir)
media_path.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(media_path)), name="media")

# --- Admin panel ---
app.include_router(admin_router)


# --- Telegram webhook ---

@app.post("/bot/webhook")
async def telegram_webhook(request: Request) -> JSONResponse:
    """Receive Telegram updates via webhook."""
    if app.state.dp is None or app.state.bot is None:
        return JSONResponse({"ok": False, "error": "Bot not initialized"}, status_code=503)

    data = await request.json()
    update = Update.model_validate(data)
    try:
        await app.state.dp.feed_update(bot=app.state.bot, update=update)
    except Exception:
        # Never let handler exceptions propagate to the ASGI layer.
        # An unhandled exception here causes Railway to restart the container,
        # wiping MemoryStorage and all FSM state.
        logger.exception(
            "Unhandled exception processing update id=%s",
            getattr(update, "update_id", "?"),
        )
    return JSONResponse({"ok": True})


# --- Health check ---

@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "bot_active": app.state.bot is not None})


@app.get("/r/card-order/{delivery_id}", response_model=None)
async def track_card_order_click(delivery_id: str) -> RedirectResponse:
    """
    Track order-button clicks and redirect to the bank app URL.

    Public endpoint (no auth). Fail-open: always redirects even if DB fails.
    """
    from app.core.database import async_session_factory
    from app.services.card_promo_service import get_order_redirect_url, record_click

    redirect_url = "https://app.tbcbank.uz/SfqR/hzztbuhk"
    try:
        async with async_session_factory() as session:
            redirect_url = await get_order_redirect_url(session)
            await record_click(session, delivery_id)
    except Exception:
        logger.exception("Failed to record card promo click for delivery_id=%s", delivery_id)

    return RedirectResponse(url=redirect_url, status_code=302)


# --- Root redirect ---

@app.get("/")
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/admin/")
