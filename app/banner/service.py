"""
Banner business logic. Mirrors app.services.job_service (restyle) but is fully
independent: own table, own queue, own limits.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.banner.schemas import BannerOffer
from app.banner.worker import process_banner_job
from app.core.config import get_settings
from app.db.models import BannerJob, JobStatus
from app.queue import banner_queue
from app.services.storage import get_storage

logger = logging.getLogger(__name__)
settings = get_settings()


class BannerJobNotFound(Exception):
    pass


class BannerJobNotSelectable(Exception):
    """Job exists but is not COMPLETED, so it has no image to select."""


class BannerBatchNotFound(Exception):
    pass


class BannerGenerationInProgress(Exception):
    """Another attempt in this batch is still PENDING/PROCESSING (double-click guard)."""


class BannerLimitReached(Exception):
    """Batch already has BANNER_MAX_ATTEMPTS_PER_BATCH attempts."""


class BannerDailyLimitReached(Exception):
    """Restaurant hit BANNER_DAILY_LIMIT_PER_RESTAURANT for today (UTC)."""


def _enforce_daily_limit(db: Session, restaurant_id: str) -> None:
    start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    used = (
        db.query(func.count(BannerJob.id))
        .filter(
            BannerJob.restaurant_id == restaurant_id,
            BannerJob.created_at >= start_of_day,
            BannerJob.status != JobStatus.FAILED,  # our failures don't count against the merchant
        )
        .scalar()
        or 0
    )
    if used >= settings.BANNER_DAILY_LIMIT_PER_RESTAURANT:
        raise BannerDailyLimitReached(
            f"Daily banner limit ({settings.BANNER_DAILY_LIMIT_PER_RESTAURANT}) reached for this restaurant"
        )


def _enqueue(job: BannerJob) -> None:
    banner_queue.enqueue(process_banner_job, job.id, job_timeout=settings.BANNER_JOB_TIMEOUT_SECONDS)


def create_banner_batch(
    db: Session,
    *,
    restaurant_id: str,
    creative_type: str,
    offer: BannerOffer,
    hero_bytes: bytes | None,
) -> BannerJob:
    """hero_bytes is already validated/normalized to JPEG by the router."""
    _enforce_daily_limit(db, restaurant_id)

    batch_id = str(uuid.uuid4())
    hero_path = None
    if hero_bytes:
        storage = get_storage(settings)
        hero_path = storage.save(
            key=storage.build_key(batch_id=batch_id, name="banner_hero", ext="jpg"),
            content=hero_bytes,
        )

    job = BannerJob(
        batch_id=batch_id,
        restaurant_id=restaurant_id,
        creative_type=creative_type,
        status=JobStatus.PENDING,
        variation_index=0,
        offer_data=offer.model_dump(mode="json"),
        hero_source_path=hero_path,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    _enqueue(job)
    return job


def regenerate_banner(db: Session, batch_id: str) -> BannerJob:
    existing = (
        db.query(BannerJob)
        .filter(BannerJob.batch_id == batch_id)
        .order_by(BannerJob.variation_index)
        .all()
    )
    if not existing:
        raise BannerBatchNotFound(f"No banner batch with id {batch_id}")

    if any(j.status in (JobStatus.PENDING, JobStatus.PROCESSING) for j in existing):
        raise BannerGenerationInProgress(f"Batch {batch_id} already has a generation in progress")

    if len(existing) >= settings.BANNER_MAX_ATTEMPTS_PER_BATCH:
        raise BannerLimitReached(
            f"Batch {batch_id} has reached the maximum of {settings.BANNER_MAX_ATTEMPTS_PER_BATCH} attempts"
        )

    template = existing[0]  # offer + hero are identical across a batch
    _enforce_daily_limit(db, template.restaurant_id)

    job = BannerJob(
        batch_id=batch_id,
        restaurant_id=template.restaurant_id,
        creative_type=template.creative_type,
        status=JobStatus.PENDING,
        variation_index=len(existing),
        offer_data=template.offer_data,
        hero_source_path=template.hero_source_path,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    _enqueue(job)
    return job


def select_banner(db: Session, job_id: int) -> BannerJob:
    chosen = db.get(BannerJob, job_id)
    if chosen is None:
        raise BannerJobNotFound(f"No banner job with id {job_id}")

    if chosen.status != JobStatus.COMPLETED:
        raise BannerJobNotSelectable(
            f"Job {job_id} has status '{chosen.status.value}', not completed - it has no image and cannot be selected"
        )

    db.query(BannerJob).filter(
        BannerJob.batch_id == chosen.batch_id,
        BannerJob.is_selected.is_(True),
    ).update({"is_selected": False})

    chosen.is_selected = True
    db.commit()
    db.refresh(chosen)
    return chosen
