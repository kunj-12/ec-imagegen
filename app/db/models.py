import enum
import uuid
from datetime import datetime, timezone
from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ImageJob(Base):
    """
    One row per generated *attempt*.

    Restyle-only service: one uploaded source photo -> one ImageJob per
    generation attempt, all sharing batch_id + source_image_path.
    variation_index records the attempt's position within the batch (0 =
    the merchant's initial upload, 1 = their first "Regenerate" click, 2 =
    the second, ...) and also indexes into
    prompt_builder.RESTYLE_VARIATION_STYLES, so it doubles as a record of
    which lighting/background style produced this image — useful later for
    seeing which styles particular merchants gravitate towards.

    Rows are only ever appended one at a time, driven by the merchant
    clicking Regenerate (see services.job_service.regenerate_restyle),
    capped at settings.MAX_IMAGES_PER_BATCH total rows per batch. Rejected
    attempts are kept (not deleted) for now so this history exists if
    needed later; when that's no longer wanted, non-selected rows for a
    batch can simply be deleted (`DELETE FROM image_jobs WHERE batch_id =
    ... AND is_selected = false`, plus removing their image_path files from
    storage) — nothing else references them, so pruning is safe.

    The merchant picks a favorite; is_selected marks it (enforced
    unique-per-batch in job_service, not DB).

    The full prompt text is NOT stored — it's fully deterministic from
    (variation_index, extra_styling), so it's rebuilt on demand in the
    worker and logged there for debugging instead of persisted redundantly
    across every row.
    """
    __tablename__ = "image_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(36), default=_uuid, index=True)

    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.PENDING, index=True)

    variation_index: Mapped[int] = mapped_column(Integer, default=0)
    extra_styling: Mapped[str | None] = mapped_column(Text, nullable=True)

    model_used: Mapped[str | None] = mapped_column(String(128), nullable=True)

    source_image_path: Mapped[str] = mapped_column(String(512))

    is_selected: Mapped[bool] = mapped_column(Boolean, default=False)

    image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # NOTE: column name is legacy from the Replicate/USD era. Since the
    # switch to fal.ai, this now stores the INR price
    # (settings.RESTYLE_PRICE_PER_IMAGE_INR), not USD. Left unrenamed to
    # avoid a migration — rename to cost_inr in a future cleanup pass along
    # with schemas.JobOut.cost_usd.
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

class BannerJob(Base):
    """
    One row per generated banner *attempt* (offer banners / marketing creatives).
    Independent of ImageJob (menu-photo restyle). Attempts in a batch share
    batch_id, offer_data and hero_source_path; variation_index is the attempt
    number and drives theme cycling in app.banner.brief.
    Reuses the existing `jobstatus` Postgres enum (pending/processing/completed/failed).
    """
    __tablename__ = "banner_jobs"
    __table_args__ = (Index("ix_banner_jobs_restaurant_created", "restaurant_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(36), default=_uuid, index=True)
    restaurant_id: Mapped[str] = mapped_column(String(64))
    creative_type: Mapped[str] = mapped_column(String(32), default="offer_banner")

    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus, name="jobstatus"), default=JobStatus.PENDING, index=True)
    variation_index: Mapped[int] = mapped_column(Integer, default=0)

    offer_data: Mapped[dict] = mapped_column(JSON)
    hero_source_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    theme: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(128), nullable=True)

    is_selected: Mapped[bool] = mapped_column(Boolean, default=False)
    background_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    cost_inr: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)