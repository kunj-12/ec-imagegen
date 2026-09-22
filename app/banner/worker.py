"""
RQ entry point for banner jobs. Independent of app.worker (restyle).

Pipeline per job:
  1. build brief (theme + background prompt)      - rules, no API call
  2. text-to-image background via BANNER_PROVIDER  - the only paid step
  3. compose text/prices/hero over it (Pillow)     - exact text, no API call
  4. save final image, record cost/model/theme

Every failure path marks the job FAILED (short message in DB, full detail in
logs) so a job can never stay stuck in PROCESSING.
"""
import asyncio
import logging
import time

from app.banner.brief import build_brief
from app.banner.compositor import BannerCompositionError, compose_banner
from app.banner.providers.factory import get_banner_provider
from app.banner.schemas import BannerOffer
from app.core.config import get_settings
from app.db.database import SessionLocal
from app.db.models import BannerJob, JobStatus
from app.inference.base import InferenceError
from app.services.storage import get_storage

logger = logging.getLogger(__name__)
settings = get_settings()

_CONTENT_TYPE_TO_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def _short_error_message(e: Exception, *, max_lines: int = 2, max_len: int = 300) -> str:
    lines = [ln.strip() for ln in str(e).strip().splitlines() if ln.strip()]
    short = " ".join(lines[:max_lines])
    return short if len(short) <= max_len else short[: max_len - 1].rstrip() + "…"


def _mark_failed(db, job: BannerJob, e: Exception, started_at: float, *, message: str | None = None) -> None:
    job.status = JobStatus.FAILED
    job.error_message = message or _short_error_message(e)
    db.commit()
    logger.error(
        "banner_generation_finished job_id=%s batch_id=%s status=failed error_type=%s "
        "total_seconds=%.3f error=%s",
        job.id, job.batch_id, type(e).__name__, time.perf_counter() - started_at, e,
    )


def process_banner_job(job_id: int) -> None:
    db = SessionLocal()
    started_at = time.perf_counter()
    try:
        job = db.get(BannerJob, job_id)
        if job is None:
            logger.error("process_banner_job: no job with id %s", job_id)
            return

        job.status = JobStatus.PROCESSING
        db.commit()
        logger.info(
            "banner_generation_started job_id=%s batch_id=%s restaurant_id=%s provider=%s",
            job.id, job.batch_id, job.restaurant_id, settings.BANNER_PROVIDER,
        )

        storage = get_storage(settings)
        width, height = settings.BANNER_WIDTH, settings.BANNER_HEIGHT

        try:
            offer = BannerOffer.model_validate(job.offer_data)
            hero_bytes = storage.read(job.hero_source_path) if job.hero_source_path else None

            brief = build_brief(
                offer,
                variation_index=job.variation_index,
                has_hero=hero_bytes is not None,
                width=width,
                height=height,
            )
            logger.info(
                "banner_prompt job_id=%s variation_index=%s theme=%s prompt=%r",
                job.id, job.variation_index, brief.theme.name, brief.prompt,
            )

            provider = get_banner_provider(settings)
            provider_started_at = time.perf_counter()
            background = asyncio.run(
                provider.generate(
                    prompt=brief.prompt,
                    model=settings.active_banner_model,
                    width=width,
                    height=height,
                )
            )
            provider_seconds = time.perf_counter() - provider_started_at

            # Keep the raw AI background: useful for debugging bad banners and
            # lets a future "re-layout without re-generating" feature skip the paid step.
            bg_ext = _CONTENT_TYPE_TO_EXT.get(background.content_type, "png")
            job.background_path = storage.save(
                key=storage.build_key(batch_id=job.batch_id, name=f"banner_bg_{job.variation_index}", ext=bg_ext),
                content=background.content,
            )

            final_bytes = compose_banner(
                background=background.content,
                offer=offer,
                theme=brief.theme,
                hero=hero_bytes,
                width=width,
                height=height,
                font_dir=settings.BANNER_FONT_DIR,
                output_format=settings.BANNER_OUTPUT_FORMAT,
                remove_hero_bg=settings.BANNER_REMOVE_HERO_BG,
            )

        except InferenceError as e:
            _mark_failed(db, job, e, started_at)
            return
        except BannerCompositionError as e:
            _mark_failed(db, job, e, started_at)
            return
        except Exception as e:
            logger.exception("Unexpected error in banner job %s", job_id)
            _mark_failed(db, job, e, started_at)
            return

        job.image_path = storage.save(
            key=storage.build_key(
                batch_id=job.batch_id, name=f"banner_{job.variation_index}", ext=settings.BANNER_OUTPUT_FORMAT
            ),
            content=final_bytes,
        )
        job.status = JobStatus.COMPLETED
        job.provider = background.provider
        job.model_used = background.model
        job.theme = brief.theme.name
        job.cost_inr = background.cost_inr
        db.commit()

        logger.info(
            "banner_generation_finished job_id=%s batch_id=%s status=completed provider=%s model=%s "
            "theme=%s provider_seconds=%.3f total_seconds=%.3f cost_inr=%s",
            job.id, job.batch_id, background.provider, background.model, brief.theme.name,
            provider_seconds, time.perf_counter() - started_at, background.cost_inr,
        )

    except Exception as e:
        # Last-resort net: failure before/around the inner try (db.get, first commit, final save).
        logger.exception("Fatal error in process_banner_job for job %s", job_id)
        try:
            db.rollback()
            job = db.get(BannerJob, job_id)
            if job is not None and job.status != JobStatus.COMPLETED:
                _mark_failed(db, job, e, started_at, message="Unexpected internal error.")
        except Exception:
            logger.exception("Could not even mark banner job %s as FAILED", job_id)
    finally:
        db.close()
