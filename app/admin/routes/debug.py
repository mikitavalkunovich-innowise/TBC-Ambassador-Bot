"""
Admin debug playground — test generation prompts without going through the Telegram bot.

Endpoints:
  GET  /admin/debug           — playground page
  POST /admin/debug/upload    — upload user reference photo (returns photo_id)
  POST /admin/debug/generate  — run generation with stored photo + prompt (updates budget)
"""
import logging
import uuid

import aiofiles
from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import get_current_admin
from app.core.database import get_db_session
from app.core.storage import get_absolute_path, get_upload_path, ensure_dirs
from app.services import settings_service
from app.services.ai_service import generate_composite_photo

router = APIRouter()
templates = Jinja2Templates(directory="app/admin/templates")
logger = logging.getLogger(__name__)


class GenerateRequest(BaseModel):
    photo_id: str
    prompt: str
    system_instruction: str = ""
    model: str = "gemini-3-pro-image"
    thinking_budget: int | None = None
    temperature: float | None = None


@router.get("/current-prompt", response_model=None)
async def get_current_prompt(
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Return current generation_prompt and system_instruction from DB (for «Load from Settings»)."""
    prompt = await settings_service.get(session, "generation_prompt") or ""
    system_instruction = await settings_service.get(session, "system_instruction") or ""
    return JSONResponse({"prompt": prompt, "system_instruction": system_instruction})


@router.get("", response_class=HTMLResponse, response_model=None)
async def debug_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> HTMLResponse:
    """Render the debug playground page."""
    current_prompt = await settings_service.get(session, "generation_prompt") or ""
    current_system_instruction = await settings_service.get(session, "system_instruction") or ""
    return templates.TemplateResponse(
        "debug.html",
        {
            "request": request,
            "active_page": "debug",
            "current_prompt": current_prompt,
            "current_system_instruction": current_system_instruction,
        },
    )


@router.post("/upload", response_model=None)
async def upload_photo(
    file: UploadFile = File(...),
    _admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """
    Save the uploaded reference photo to uploads/debug/ and return a photo_id.
    The photo_id is used in subsequent /generate calls — no need to re-upload.
    """
    ensure_dirs()
    photo_id = uuid.uuid4().hex
    filename = f"photo_{photo_id}.jpeg"
    dest = get_upload_path("debug", filename)

    data = await file.read()
    async with aiofiles.open(dest, "wb") as f:
        await f.write(data)

    preview_url = f"/media/debug/{filename}"
    logger.info("Debug photo uploaded: %s", dest)
    return JSONResponse({"photo_id": photo_id, "preview_url": preview_url})


@router.post("/generate", response_model=None)
async def debug_generate(
    body: GenerateRequest,
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """
    Generate a composite image using the stored user photo and the provided prompt.
    Updates the budget counter exactly like a regular bot generation.
    """
    # Load stored user photo
    user_photo_path = get_upload_path("debug", f"photo_{body.photo_id}.jpeg")
    if not user_photo_path.exists():
        return JSONResponse({"error": "Photo not found. Please upload a photo first."}, status_code=400)

    async with aiofiles.open(user_photo_path, "rb") as f:
        user_photo_bytes = await f.read()

    # Load ambassador photo
    ambassador_path_rel = await settings_service.get(session, "ambassador_photo_path")
    if not ambassador_path_rel:
        return JSONResponse({"error": "Ambassador photo not configured in Settings."}, status_code=400)

    ambassador_path = get_absolute_path(ambassador_path_rel)
    if not ambassador_path.exists():
        return JSONResponse({"error": f"Ambassador photo file not found: {ambassador_path}"}, status_code=400)

    async with aiofiles.open(ambassador_path, "rb") as f:
        ambassador_bytes = await f.read()

    # Load ambassador face crop (best-effort)
    face_crop_path = ambassador_path.parent / ("ambassador_face_crop" + ambassador_path.suffix)
    ambassador_face_crop_bytes: bytes | None = None
    if face_crop_path.exists():
        async with aiofiles.open(face_crop_path, "rb") as f:
            ambassador_face_crop_bytes = await f.read()

    # Run generation
    try:
        result = await generate_composite_photo(
            user_photo_bytes_list=[user_photo_bytes],
            ambassador_photo_bytes=ambassador_bytes,
            prompt_template=body.prompt,
            extra_prompt="",
            ambassador_face_crop_bytes=ambassador_face_crop_bytes,
            system_instruction=body.system_instruction or None,
            model=body.model or "gemini-3-pro-image",
            thinking_budget=body.thinking_budget,
            temperature=body.temperature,
        )
    except Exception as exc:
        logger.exception("Debug generation failed")
        return JSONResponse({"error": f"Generation failed: {exc}"}, status_code=500)

    # Save result
    ensure_dirs()
    result_filename = f"result_{uuid.uuid4().hex}.webp"
    result_path = get_upload_path("debug", result_filename)
    async with aiofiles.open(result_path, "wb") as f:
        await f.write(result.image_bytes)

    # Update budget (same as regular generations)
    await settings_service.add_budget_spent(session, result.cost_usd)

    image_url = f"/media/debug/{result_filename}"
    logger.info(
        "Debug generation complete. cost=$%.6f input_tokens=%d",
        float(result.cost_usd),
        result.input_tokens,
    )

    return JSONResponse({
        "image_url": image_url,
        "cost_usd": float(result.cost_usd),
        "input_tokens": result.input_tokens,
        "model": body.model,
        "thinking_budget": body.thinking_budget,
        "temperature": body.temperature,
    })
