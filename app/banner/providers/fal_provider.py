"""
fal.ai text-to-image provider for banner backgrounds.

Model-agnostic on purpose: any fal text-to-image endpoint that accepts
`prompt` + `image_size` works (e.g. fal-ai/flux/schnell, fal-ai/qwen-image).
Model-specific knobs (steps, guidance, output_format, or a different size
parameter) go in BANNER_EXTRA_ARGS_JSON and are merged over the defaults, so
switching models is an .env change, not a code change.
Check each model's fal page for its accepted arguments.
"""
import asyncio
import logging
import os

import fal_client

from app.banner.providers.base import (
    BannerImageProvider,
    GeneratedBackground,
    call_with_retries,
    download_image,
)
from app.core.config import Settings
from app.inference.base import InferenceError

logger = logging.getLogger(__name__)


class FalBannerProvider(BannerImageProvider):
    name = "fal"

    def __init__(self, settings: Settings, extra_args: dict | None = None):
        self._settings = settings
        self._extra_args = settings.banner_extra_args if extra_args is None else extra_args
        if settings.FAL_KEY:
            os.environ["FAL_KEY"] = settings.FAL_KEY

    async def generate(self, *, prompt: str, model: str, width: int, height: int) -> GeneratedBackground:
        return await asyncio.to_thread(self._generate_sync, prompt, model, width, height)

    def _generate_sync(self, prompt: str, model: str, width: int, height: int) -> GeneratedBackground:
        s = self._settings
        logger.info("fal banner starting: model=%s prompt_len=%d size=%dx%d", model, len(prompt), width, height)
        return call_with_retries(
            lambda: self._call_once(prompt, model, width, height),
            max_retries=s.MAX_RETRIES,
            backoff_seconds=s.RETRY_BACKOFF_SECONDS,
            label="fal banner call",
        )

    def _call_once(self, prompt: str, model: str, width: int, height: int) -> GeneratedBackground:
        s = self._settings
        arguments = {
            "prompt": prompt,
            "image_size": {"width": width, "height": height},
            "num_images": 1,
        }
        arguments.update(self._extra_args)

        try:
            result = fal_client.subscribe(model, arguments=arguments, with_logs=False)
        except fal_client.client.FalClientError as e:
            status = getattr(e, "status_code", None)
            if status in (401, 403):
                raise InferenceError(f"fal.ai rejected the API key (status={status}): {e}", retryable=False) from e
            if status is None or status == 429 or status >= 500:
                raise InferenceError(f"fal transient error (status={status}): {e}", retryable=True) from e
            raise InferenceError(f"fal rejected request (status={status}): {e}", retryable=False) from e
        except Exception as e:
            raise InferenceError(f"fal call crashed: {e}", retryable=True) from e

        images = result.get("images") or []
        if not images:
            raise InferenceError(f"fal returned no images [result={result!r}]", retryable=True)

        content, content_type = download_image(images[0]["url"], timeout=s.REQUEST_TIMEOUT_SECONDS)
        return GeneratedBackground(
            content=content,
            content_type=content_type,
            provider=self.name,
            model=model,
            cost_inr=s.BANNER_PRICE_PER_IMAGE_INR,
        )
