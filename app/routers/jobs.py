import io
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from PIL import Image
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.database import get_db
from app.db.models import ImageJob
from app.schemas import (
    BatchStylesOut,
    GenerateRestyleRequest,
    JobOut,
    RegenerateRestyleRequest,
    SelectRestyleRequest,
    StyleOut,
)
from app.services import job_service
from app.services.prompt_builder import get_style_options

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs", tags=["jobs"])
settings = get_settings()

# ─── HEIC/HEIF support (optional) ──────────────────────────────────────────
# pillow-heif ships a compiled DLL that can be blocked by Windows Application
# Control policies (WDAC/AppLocker) on locked-down machines. Import failures
# here must NOT crash the whole app on startup — they should just disable
# HEIC support so JPEG/PNG/WEBP uploads keep working.
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except Exception:
    logger.warning(
        "pillow_heif unavailable — HEIC/HEIF uploads will be rejected until "
        "this is resolved.",
        exc_info=True,
    )
    HEIC_SUPPORTED = False
# ────────────────────────────────────────────────────────────────────────────

# Fixed technical mapping: Pillow's decoded format name -> file extension.
# This is NOT a setting — it never changes. Which of these are *currently
# allowed* is controlled by settings.ALLOWED_IMAGE_FORMATS (.env).
_FORMAT_TO_EXT = {
    "JPEG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
    "HEIF": "heic",  # pillow-heif reports HEIC/HEIF files as format "HEIF"
}


def _validate_and_get_ext(photo_bytes: bytes) -> str:
    try:
        with Image.open(io.BytesIO(photo_bytes)) as img:
            img.verify()
        with Image.open(io.BytesIO(photo_bytes)) as img:
            fmt = img.format
    except Exception:
        raise HTTPException(status_code=415, detail="File is not a valid image.")

    if fmt == "HEIF" and not HEIC_SUPPORTED:
        raise HTTPException(
            status_code=415,
            detail="HEIC/HEIF uploads are currently unavailable on this server.",
        )

    allowed = settings.allowed_image_formats_set
    if fmt not in allowed:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported image format '{fmt}'. Allowed: {', '.join(sorted(allowed))}.",
        )

    ext = _FORMAT_TO_EXT.get(fmt)
    if ext is None:
        raise HTTPException(
            status_code=500,
            detail=f"Format '{fmt}' is allowed but has no known extension mapping.",
        )
    return ext


@router.post("/restyle", response_model=JobOut, status_code=201)
async def create_restyle_batch(
    photo: UploadFile = File(...),
    extra_styling: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """
    Merchant uploads one source photo. NO generation happens yet: the batch
    is created in AWAITING_STYLE state. Next, the frontend shows the styles
    from GET /jobs/restyle/styles and calls POST /jobs/restyle/generate
    with the chosen style_index. Regenerate is available after that.
    """
    photo_bytes = await photo.read(settings.MAX_UPLOAD_SIZE_BYTES + 1)
    if len(photo_bytes) > settings.MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max {settings.MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB.",
        )
    if len(photo_bytes) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    photo_ext = _validate_and_get_ext(photo_bytes)

    if settings.MAX_EXTRA_STYLING_LEN is None:
        extra_styling = None
    elif extra_styling is not None:
        extra_styling = extra_styling.strip()[: settings.MAX_EXTRA_STYLING_LEN] or None

    return job_service.create_restyle_batch(
        db,
        extra_styling=extra_styling,
        photo_bytes=photo_bytes,
        photo_ext=photo_ext,
    )


@router.get("/restyle/styles", response_model=list[StyleOut])
def list_restyle_styles():
    """Style options the merchant can pick from after uploading a photo."""
    return get_style_options()


@router.post("/restyle/generate", response_model=JobOut, status_code=201)
def generate_restyle(req: GenerateRestyleRequest, db: Session = Depends(get_db)):
    """
    Merchant picked a style for an uploaded photo — starts the first
    generation for that batch. 404 unknown batch, 422 invalid style_index,
    409 if a style was already chosen for this batch.
    """
    try:
        return job_service.start_restyle(db, req.batch_id, req.style_index)
    except job_service.RestyleBatchNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except job_service.InvalidRestyleStyle as e:
        raise HTTPException(status_code=422, detail=str(e))
    except job_service.RestyleStyleAlreadyChosen as e:
        raise HTTPException(status_code=409, detail=str(e))
    except job_service.RestyleQueueUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/restyle/regenerate", response_model=JobOut, status_code=201)
def regenerate_restyle(req: RegenerateRestyleRequest, db: Session = Depends(get_db)):
    """
    Merchant didn't like the current image — regenerate against the same
    source photo with the style they pick from GET /jobs/batch/{batch_id}/styles.
    style_index is optional: omit it and the server auto-picks the next
    unused style (or a model-chosen surface once all are used).

    404 unknown batch, 422 style_index out of range, 409 if a generation is
    already in flight / no style chosen yet / MAX_IMAGES_PER_BATCH reached /
    the style was already used, 503 if the queue is unavailable.
    """
    try:
        return job_service.regenerate_restyle(db, req.batch_id, req.style_index)
    except job_service.RestyleBatchNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except job_service.InvalidRestyleStyle as e:
        raise HTTPException(status_code=422, detail=str(e))
    except (
        job_service.RestyleGenerationInProgress,
        job_service.RestyleStyleNotChosen,
        job_service.RestyleLimitReached,
        job_service.RestyleStyleAlreadyUsed,
    ) as e:
        raise HTTPException(status_code=409, detail=str(e))
    except job_service.RestyleQueueUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/restyle/select", response_model=JobOut, status_code=201)
def select_restyle(req: SelectRestyleRequest, db: Session = Depends(get_db)):
    """
    Merchant picked their favorite completed attempt. Rejected with 404 if
    the job_id doesn't exist, or 409 if it exists but isn't COMPLETED
    (e.g. a FAILED attempt with no image) — see job_service.select_restyle.
    """
    try:
        return job_service.select_restyle(db, req.job_id)
    except job_service.RestyleJobNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except job_service.RestyleJobNotSelectable as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(ImageJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/batch/{batch_id}/styles", response_model=BatchStylesOut)
def get_batch_styles(batch_id: str, db: Session = Depends(get_db)):
    """
    Styles the merchant can still pick for the NEXT regenerate (unused ones
    only: 8 at the start, then 7, 6, ...), plus can_regenerate / blocked_reason
    so the frontend knows whether to show the picker at all.
    """
    try:
        return job_service.get_available_styles(db, batch_id)
    except job_service.RestyleBatchNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/batch/{batch_id}", response_model=list[JobOut])
def get_batch(batch_id: str, db: Session = Depends(get_db)):
    """
    Full attempt history for a batch, ordered oldest-first (variation_index
    0 = original upload). Frontend uses len(result) vs MAX_IMAGES_PER_BATCH
    to decide whether to show the Regenerate button.
    """
    jobs = db.query(ImageJob).filter(ImageJob.batch_id == batch_id).order_by(ImageJob.created_at).all()
    if not jobs:
        raise HTTPException(status_code=404, detail="Batch not found")
    return jobs