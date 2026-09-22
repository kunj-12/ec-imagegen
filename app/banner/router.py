import io
import json
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from PIL import Image, ImageOps
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.banner import service
from app.banner.brief import THEMES_BY_NAME
from app.banner.schemas import (
    SUPPORTED_CREATIVE_TYPES,
    BannerJobOut,
    BannerOffer,
    RegenerateBannerRequest,
    SelectBannerRequest,
)
from app.core.config import get_settings
from app.db.database import get_db
from app.db.models import BannerJob

logger = logging.getLogger(__name__)
settings = get_settings()


def _require_enabled() -> None:
    if not settings.BANNER_ENABLED:
        raise HTTPException(status_code=503, detail="Banner generation is disabled.")


router = APIRouter(prefix="/banners", tags=["banners"], dependencies=[Depends(_require_enabled)])

_ALLOWED_FORMAT_NAMES = {"JPEG", "PNG", "WEBP", "HEIF"}
_MAX_HERO_SIDE = 2048


def _normalize_hero(data: bytes) -> bytes:
    """
    Validate the optional hero photo and re-encode it as a plain JPEG.
    Done here (API process, where HEIC support is registered) so the worker
    never has to decode exotic formats, EXIF rotation is baked in, and huge
    uploads are capped in size.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            img = ImageOps.exif_transpose(img)
            img.thumbnail((_MAX_HERO_SIDE, _MAX_HERO_SIDE))
            if img.mode in ("RGBA", "LA", "P"):
                rgba = img.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.split()[-1])
                img = flat
            else:
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=92)
    except Exception:
        raise HTTPException(status_code=415, detail="Hero image is not a valid image.")

    allowed = settings.allowed_image_formats_set & _ALLOWED_FORMAT_NAMES
    if fmt not in allowed:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported hero image format '{fmt}'. Allowed: {', '.join(sorted(allowed))}.",
        )
    return buf.getvalue()


@router.post("", response_model=BannerJobOut, status_code=201)
async def create_banner(
    restaurant_id: str = Form(..., min_length=1, max_length=64),
    offer: str = Form(..., description="JSON matching BannerOffer"),
    creative_type: str = Form("offer_banner"),
    hero_image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    """
    Restaurant submits an offer (and optionally a food photo). Generates ONE
    banner; use POST /banners/regenerate for further attempts on the same offer.
    """
    if creative_type not in SUPPORTED_CREATIVE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported creative_type '{creative_type}'. Supported: {', '.join(SUPPORTED_CREATIVE_TYPES)}.",
        )

    try:
        parsed = BannerOffer.model_validate_json(offer)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=json.loads(e.json()))

    if parsed.style and parsed.style.strip().lower() not in THEMES_BY_NAME:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown style '{parsed.style}'. Available: {', '.join(sorted(THEMES_BY_NAME))}.",
        )

    hero_bytes = None
    if hero_image is not None and hero_image.filename:
        raw = await hero_image.read(settings.MAX_UPLOAD_SIZE_BYTES + 1)
        if len(raw) > settings.MAX_UPLOAD_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Max {settings.MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB.",
            )
        if len(raw) == 0:
            raise HTTPException(status_code=400, detail="Uploaded hero image is empty.")
        hero_bytes = _normalize_hero(raw)

    try:
        return service.create_banner_batch(
            db,
            restaurant_id=restaurant_id,
            creative_type=creative_type,
            offer=parsed,
            hero_bytes=hero_bytes,
        )
    except service.BannerDailyLimitReached as e:
        raise HTTPException(status_code=429, detail=str(e))


@router.post("/regenerate", response_model=BannerJobOut, status_code=201)
def regenerate_banner(req: RegenerateBannerRequest, db: Session = Depends(get_db)):
    try:
        return service.regenerate_banner(db, req.batch_id)
    except service.BannerBatchNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (service.BannerGenerationInProgress, service.BannerLimitReached) as e:
        raise HTTPException(status_code=409, detail=str(e))
    except service.BannerDailyLimitReached as e:
        raise HTTPException(status_code=429, detail=str(e))


@router.post("/select", response_model=BannerJobOut, status_code=201)
def select_banner(req: SelectBannerRequest, db: Session = Depends(get_db)):
    try:
        return service.select_banner(db, req.job_id)
    except service.BannerJobNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except service.BannerJobNotSelectable as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/batch/{batch_id}", response_model=list[BannerJobOut])
def get_banner_batch(batch_id: str, db: Session = Depends(get_db)):
    jobs = db.query(BannerJob).filter(BannerJob.batch_id == batch_id).order_by(BannerJob.created_at).all()
    if not jobs:
        raise HTTPException(status_code=404, detail="Batch not found")
    return jobs


@router.get("/{job_id}", response_model=BannerJobOut)
def get_banner(job_id: int, db: Session = Depends(get_db)):
    job = db.get(BannerJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job
