import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import ImageJob, JobStatus
from app.queue import image_queue, redis_conn
from app.services.storage import get_storage
from app.services.prompt_builder import RESTYLE_VARIATION_STYLES, get_style_options
from app.worker import process_image_job

# Redis key backing the atomic counter that hands out batch_number values
# (1, 2, 3, ...) for storage folder naming. INCR is atomic in Redis, so this
# is safe under concurrent uploads without needing a DB-level sequence.
_BATCH_NUMBER_COUNTER_KEY = "restyle:batch_number_seq"


def _next_batch_number() -> int:
    return redis_conn.incr(_BATCH_NUMBER_COUNTER_KEY)

logger = logging.getLogger(__name__)
settings = get_settings()


class RestyleJobNotFound(Exception):
    """No ImageJob row exists with the given id."""
    pass


class RestyleJobNotSelectable(Exception):
    """
    Job exists but its status isn't COMPLETED (e.g. FAILED, PENDING, or
    PROCESSING) — it has no usable image_path, so it cannot be marked as
    the merchant's selected result. Raised instead of silently setting
    is_selected=True on a job with no image, which would corrupt the
    "one selected job per batch" guarantee.
    """
    pass


class RestyleBatchNotFound(Exception):
    """No ImageJob rows exist with the given batch_id."""
    pass


class RestyleGenerationInProgress(Exception):
    """
    A job in this batch is still PENDING/PROCESSING. Raised instead of
    silently enqueuing a second concurrent generation for the same batch —
    guards against double-clicks / refresh-and-reclick on the Regenerate
    button creating two simultaneous renders of the same source image.
    """
    pass


class RestyleLimitReached(Exception):
    """
    The batch already has settings.MAX_IMAGES_PER_BATCH rows (original
    upload + regenerations included). Raised so the router can return a
    clear 409/422 instead of quietly enqueuing past the configured cap.
    """
    pass


class RestyleStyleNotChosen(Exception):
    """The batch is still AWAITING_STYLE — the merchant hasn't picked a style yet."""
    pass


class RestyleStyleAlreadyChosen(Exception):
    """A style was already chosen (generation already started) for this batch."""
    pass


class InvalidRestyleStyle(Exception):
    """style_index is outside the range of available styles."""
    pass


class RestyleStyleAlreadyUsed(Exception):
    """
    The requested style already has a completed or in-flight attempt in this
    batch. Regenerate only offers styles that have not been used yet.
    """
    pass


class RestyleQueueUnavailable(Exception):
    """The job row was saved but could not be pushed onto the queue (e.g. Redis down)."""
    pass


def create_restyle_batch(
    db: Session,
    *,
    extra_styling: str | None,
    photo_bytes: bytes,
    photo_ext: str,
) -> ImageJob:
    """
    Merchant uploaded their own photo. Saves it and creates the batch in
    AWAITING_STYLE state — nothing is enqueued and no provider is called
    until the merchant picks a style (see start_restyle).

    photo_ext is the *validated* real extension (see jobs.py), never the
    client-supplied filename — avoids trusting user input in a storage path.
    """
    storage = get_storage(settings)
    batch_id = str(uuid.uuid4())
    batch_number = _next_batch_number()

    # NOTE: batch_number (not batch_id) is what names the folder in storage
    # now — purely so files are easy to spot by eye. batch_id UUID is still
    # the real identifier used everywhere else (API, DB lookups, frontend).
    source_key = storage.build_key(batch_id=str(batch_number), name="source", ext=photo_ext)
    source_path = storage.save(key=source_key, content=photo_bytes)

    job = ImageJob(
        batch_id=batch_id,
        batch_number=batch_number,
        status=JobStatus.AWAITING_STYLE,
        variation_index=0,
        extra_styling=extra_styling,
        source_image_path=source_path,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def start_restyle(db: Session, batch_id: str, style_index: int) -> ImageJob:
    """
    Merchant picked a style: record it on the batch's first job and enqueue
    the generation. Row is locked so a double-click can't start it twice.
    """
    if not 0 <= style_index < len(RESTYLE_VARIATION_STYLES):
        raise InvalidRestyleStyle(
            f"style_index must be between 0 and {len(RESTYLE_VARIATION_STYLES) - 1}"
        )

    if db.query(ImageJob).filter(ImageJob.batch_id == batch_id).count() == 0:
        raise RestyleBatchNotFound(f"No batch with id {batch_id}")

    job = (
        db.query(ImageJob)
        .filter(ImageJob.batch_id == batch_id, ImageJob.status == JobStatus.AWAITING_STYLE)
        .with_for_update()
        .first()
    )
    if job is None:
        raise RestyleStyleAlreadyChosen(f"A style was already chosen for batch {batch_id}")

    job.style_index = style_index
    job.status = JobStatus.PENDING
    db.commit()
    db.refresh(job)

    _enqueue_or_fail(db, job, revert_to_awaiting=True)
    return job


# A PENDING/PROCESSING row older than the RQ job timeout (+ this grace) cannot
# still be alive — its worker was killed or crashed before it could record a
# failure. Such rows are treated as failed so they never block the batch
# forever ("generation already in progress").
_STALE_JOB_GRACE_SECONDS = 60


def _is_stale(job: ImageJob) -> bool:
    if job.status not in (JobStatus.PENDING, JobStatus.PROCESSING):
        return False
    ts = job.updated_at
    if ts is None:
        return False
    if ts.tzinfo is None:  # SQLite returns naive datetimes
        ts = ts.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age > settings.RESTYLE_JOB_TIMEOUT_SECONDS + _STALE_JOB_GRACE_SECONDS


def _is_active(job: ImageJob) -> bool:
    """A generation that is genuinely still running or queued."""
    return job.status in (JobStatus.PENDING, JobStatus.PROCESSING) and not _is_stale(job)


def _consumed_styles(jobs: list[ImageJob]) -> set[int]:
    """
    Styles that are used up in this batch: completed, or currently running.
    A style whose attempt FAILED produced no image, so the merchant may pick
    it again (failed rows still count towards MAX_IMAGES_PER_BATCH).
    """
    return {
        job.style_index
        for job in jobs
        if job.style_index is not None and (job.status == JobStatus.COMPLETED or _is_active(job))
    }


def _enqueue_or_fail(db: Session, job: ImageJob, *, revert_to_awaiting: bool = False) -> None:
    """
    Pushes the job onto the queue. If that fails (Redis down, etc.) the row
    would otherwise sit at PENDING forever and block the batch, so it is
    resolved here: reverted to AWAITING_STYLE (first generation, so the
    merchant can simply pick again) or marked FAILED (regenerate).
    """
    try:
        image_queue.enqueue(
            process_image_job, job.id, job_timeout=settings.RESTYLE_JOB_TIMEOUT_SECONDS
        )
    except Exception:
        logger.exception("Failed to enqueue restyle job %s", job.id)
        if revert_to_awaiting:
            job.status = JobStatus.AWAITING_STYLE
            job.style_index = None
        else:
            job.status = JobStatus.FAILED
            job.error_message = "Could not queue the generation job. Please try again."
        db.commit()
        raise RestyleQueueUnavailable("Generation service is temporarily unavailable. Please try again.")


def _next_unused_style(existing: list[ImageJob]) -> int | None:
    """
    Used only when regenerate is called WITHOUT a style_index (auto mode).
    Continues after the merchant's first pick and wraps around, skipping
    styles already used in this batch. E.g. first pick 2 of 8 -> 3,4,5,6,7,0,1.
    Returns None when every style has been used (caller then lets the model
    choose the surface freely).
    """
    total = len(RESTYLE_VARIATION_STYLES)
    used = _consumed_styles(existing)
    first = existing[0].style_index or 0
    for step in range(1, total + 1):
        candidate = (first + step) % total
        if candidate not in used:
            return candidate
    return None


def _blocked_reason(existing: list[ImageJob]) -> str | None:
    if any(job.status == JobStatus.AWAITING_STYLE for job in existing):
        return "style_not_chosen"
    if any(_is_active(job) for job in existing):
        return "generation_in_progress"
    if len(existing) >= settings.MAX_IMAGES_PER_BATCH:
        return "limit_reached"
    return None


def get_available_styles(db: Session, batch_id: str) -> dict:
    """
    What the frontend shows after each generation: the curated styles this
    batch has NOT used yet (8 at first, then 7, 6, ...), plus whether
    regenerate is currently allowed and why not if blocked.
    """
    existing = (
        db.query(ImageJob)
        .filter(ImageJob.batch_id == batch_id)
        .order_by(ImageJob.variation_index)
        .all()
    )
    if not existing:
        raise RestyleBatchNotFound(f"No batch with id {batch_id}")

    consumed = _consumed_styles(existing)
    styles = [opt for opt in get_style_options() if opt["index"] not in consumed]
    reason = _blocked_reason(existing)

    return {
        "batch_id": batch_id,
        "styles": styles,
        "attempts_used": sum(1 for job in existing if job.status != JobStatus.AWAITING_STYLE),
        "max_attempts": settings.MAX_IMAGES_PER_BATCH,
        "can_regenerate": reason is None,
        "blocked_reason": reason,
        "free_choice_available": reason is None and not styles,
    }


def regenerate_restyle(db: Session, batch_id: str, style_index: int | None = None) -> ImageJob:
    """
    Merchant didn't like the current image and clicked "Regenerate": run
    the SAME source photo through the provider again as a new row in the
    same batch.

    style_index given  -> that exact style (must still be unused).
    style_index None   -> auto: next unused style, or a model-chosen surface
                          once every curated style has been used.

    Guards (all enforced here, server-side — never trust the frontend
    button being disabled):
      - InvalidRestyleStyle: style_index out of range.
      - RestyleStyleNotChosen: the merchant hasn't picked a first style yet.
      - RestyleGenerationInProgress: another attempt in this batch is still
        PENDING/PROCESSING.
      - RestyleLimitReached: MAX_IMAGES_PER_BATCH rows exist.
      - RestyleStyleAlreadyUsed: the requested style was already used.

    Concurrency: the batch's first row is locked (SELECT ... FOR UPDATE) and
    the batch is re-read AFTER the lock is granted, so two simultaneous
    requests (double-click, two tabs) are serialized — the second one sees
    the first one's PENDING row and gets RestyleGenerationInProgress.
    """
    if style_index is not None and not 0 <= style_index < len(RESTYLE_VARIATION_STYLES):
        raise InvalidRestyleStyle(
            f"style_index must be between 0 and {len(RESTYLE_VARIATION_STYLES) - 1}"
        )

    anchor = (
        db.query(ImageJob)
        .filter(ImageJob.batch_id == batch_id)
        .order_by(ImageJob.variation_index)
        .with_for_update()
        .first()
    )
    if anchor is None:
        raise RestyleBatchNotFound(f"No batch with id {batch_id}")

    # Separate statement => fresh snapshot taken after the lock was granted.
    existing = (
        db.query(ImageJob)
        .filter(ImageJob.batch_id == batch_id)
        .order_by(ImageJob.variation_index)
        .populate_existing()
        .all()
    )

    for job in existing:
        if _is_stale(job):
            logger.warning("Marking stale restyle job %s as failed", job.id)
            job.status = JobStatus.FAILED
            job.error_message = "Generation timed out."

    reason = _blocked_reason(existing)
    if reason == "style_not_chosen":
        raise RestyleStyleNotChosen(f"Batch {batch_id} has no style selected yet")
    if reason == "generation_in_progress":
        raise RestyleGenerationInProgress(
            f"Batch {batch_id} already has a generation in progress"
        )
    if reason == "limit_reached":
        raise RestyleLimitReached(
            f"Batch {batch_id} has reached the maximum of "
            f"{settings.MAX_IMAGES_PER_BATCH} images"
        )

    if style_index is None:
        chosen_style = _next_unused_style(existing)
    else:
        if style_index in _consumed_styles(existing):
            raise RestyleStyleAlreadyUsed(
                f"Style {style_index} was already used in batch {batch_id}"
            )
        chosen_style = style_index

    template = existing[0]  # source photo + extra_styling are identical across a batch

    job = ImageJob(
        batch_id=batch_id,
        batch_number=template.batch_number,  # same folder as the rest of this batch
        status=JobStatus.PENDING,
        variation_index=max(j.variation_index for j in existing) + 1,
        style_index=chosen_style,
        extra_styling=template.extra_styling,
        source_image_path=template.source_image_path,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    _enqueue_or_fail(db, job)
    return job


def select_restyle(db: Session, job_id: int) -> ImageJob:
    """
    Merchant picked their favorite attempt from a restyle batch. This does
    not enqueue a new render — the chosen output is already full quality —
    it just records the preference and clears any prior selection in the
    same batch.

    Only a COMPLETED job (i.e. one that actually has a generated
    image_path) can be selected. FAILED/PENDING/PROCESSING jobs are
    rejected with RestyleJobNotSelectable — selecting one of those would
    mark a batch's "final pick" as a job with no image, which the
    frontend has no sane way to render.
    """
    chosen = db.get(ImageJob, job_id)
    if chosen is None:
        raise RestyleJobNotFound(f"No restyle job with id {job_id}")

    if chosen.status != JobStatus.COMPLETED:
        raise RestyleJobNotSelectable(
            f"Job {job_id} has status '{chosen.status.value}', not completed — "
            f"it has no image and cannot be selected"
        )

    db.query(ImageJob).filter(
        ImageJob.batch_id == chosen.batch_id,
        ImageJob.is_selected.is_(True),
    ).update({"is_selected": False})

    chosen.is_selected = True
    db.commit()
    db.refresh(chosen)
    return chosen