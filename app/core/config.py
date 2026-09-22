from functools import lru_cache
from pathlib import Path
import json
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        extra="ignore",
    )

    # --- Inference provider (image-to-image restyle only) ---
    # Which provider worker.py actually calls, via app.inference.provider_factory.
    # "fal" or "replicate" — switching is a .env change only, no redeploy.
    # Neither provider's SDK/config below is deleted when unused, so you can
    # flip back and forth freely while deciding.
    INFERENCE_PROVIDER: str = "fal"

    # fal.ai — FAL_KEY is read by the fal_client SDK from this setting (we
    # inject it into the environment at provider construction time).
    FAL_KEY: str = ""
    FAL_RESTYLE_MODEL: str = "fal-ai/flux-2/turbo/edit"
    # Merchant-facing price recorded per generated image, in INR (business
    # decision — not derived from fal's per-megapixel USD billing formula).
    RESTYLE_PRICE_PER_IMAGE_INR: float = 0.7

    # Replicate — Flux Kontext Pro (or whatever REPLICATE_RESTYLE_MODEL is
    # set to). Kept fully wired up as a fallback/alternative to fal.ai.
    REPLICATE_API_TOKEN: str = ""
    REPLICATE_RESTYLE_MODEL: str = "black-forest-labs/flux-kontext-pro"
    RESTYLE_PRICE_PER_IMAGE_USD: float = 0.04

    RESTYLE_POLL_TIMEOUT_SECONDS: int = 180

    # --- Upload validation ---
    MAX_UPLOAD_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB
    ALLOWED_IMAGE_FORMATS: str

    # Currently None = feature disabled; extra_styling is ignored end-to-end
    # (see jobs.py). Set to an integer in .env to enable merchant/backend
    # styling notes with that max length, no code change needed.
    MAX_EXTRA_STYLING_LEN: int | None = None

    # One image is generated per upload; the merchant can then click
    # "Regenerate" to try again against the same source photo. This caps
    # the TOTAL number of attempts in a batch, the original included — e.g.
    # a value of 4 means 1 initial generation + up to 3 regenerations.
    # Enforced server-side in job_service.regenerate_restyle, never trust
    # the frontend button being disabled alone.
    MAX_IMAGES_PER_BATCH: int = 4
    IMAGE_SIZE: str = "1024x1024"

        # --- Banner / marketing creatives (independent of restyle) ---
    BANNER_ENABLED: bool = True
    BANNER_PROVIDER: str = "fal"  # "fal" | "replicate"
    BANNER_FAL_MODEL: str = "fal-ai/flux/schnell"
    BANNER_REPLICATE_MODEL: str = "black-forest-labs/flux-schnell"
    # Model-specific request args merged over the defaults, JSON dict.
    BANNER_EXTRA_ARGS_JSON: str = "{}"
    # Cost recorded per generated banner, in INR (business number, like RESTYLE_PRICE_*).
    BANNER_PRICE_PER_IMAGE_INR: float = 1.0
    BANNER_POLL_TIMEOUT_SECONDS: int = 120
    BANNER_WIDTH: int = 1344
    BANNER_HEIGHT: int = 512
    BANNER_OUTPUT_FORMAT: str = "png"  # png | webp | jpg
    BANNER_FONT_DIR: str = str(_PROJECT_ROOT / "app" / "assets" / "fonts")
    BANNER_REMOVE_HERO_BG: bool = False  # needs `pip install rembg`
    BANNER_QUEUE_NAME: str = "banner"
    BANNER_JOB_TIMEOUT_SECONDS: int = 300
    BANNER_MAX_ATTEMPTS_PER_BATCH: int = 5
    BANNER_DAILY_LIMIT_PER_RESTAURANT: int = 20

    # --- Storage (local disk for now; swap for S3 client later) ---
    STORAGE_BACKEND: str = "local"
    LOCAL_STORAGE_DIR: str = str(_PROJECT_ROOT / "storage")
    S3_BUCKET: str = ""
    S3_REGION: str = ""

    @field_validator("LOCAL_STORAGE_DIR", mode="before")
    @classmethod
    def normalize_local_storage_dir(cls, value: str | None) -> str:
        if value is None or value == "":
            return str(_PROJECT_ROOT / "storage")

        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (_PROJECT_ROOT / path).resolve()
        return str(path)

    # --- DB / queue ---
    DATABASE_URL: str
    REDIS_URL: str = "redis://localhost:6379/0"
    RQ_QUEUE_NAME: str = "imagegen"
    # --- DB connection pool (Postgres only; ignored for SQLite) ---
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE_SECONDS: int = 1800  # 30 minutes

    # --- HTTP behavior towards the inference API ---
    REQUEST_TIMEOUT_SECONDS: int = 60
    MAX_RETRIES: int = 3
    RETRY_BACKOFF_SECONDS: float = 2.0

    # Format Replicate returns the restyled output in, and the extension it's
    # saved with. Not tied to the input format — every job outputs this same
    # format regardless of what the merchant uploaded. Kept in .env so it can
    # be changed (e.g. to "png") without a code deploy.
    OUTPUT_IMAGE_FORMAT: str = "jpg"
    RESTYLE_JOB_TIMEOUT_SECONDS: int = 600

    @property
    def allowed_image_formats_set(self) -> set[str]:
        return {f.strip().upper() for f in self.ALLOWED_IMAGE_FORMATS.split(",") if f.strip()}

    @property
    def active_restyle_model(self) -> str:
        """The model id to pass to whichever provider INFERENCE_PROVIDER selects."""
        if self.INFERENCE_PROVIDER.strip().lower() == "replicate":
            return self.REPLICATE_RESTYLE_MODEL
        return self.FAL_RESTYLE_MODEL

    @property
    def active_banner_model(self) -> str:
        if self.BANNER_PROVIDER.strip().lower() == "replicate":
            return self.BANNER_REPLICATE_MODEL
        return self.BANNER_FAL_MODEL

    @property
    def banner_extra_args(self) -> dict:
        try:
            data = json.loads(self.BANNER_EXTRA_ARGS_JSON or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"BANNER_EXTRA_ARGS_JSON is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("BANNER_EXTRA_ARGS_JSON must be a JSON object")
        return data

@lru_cache
def get_settings() -> Settings:
    return Settings()