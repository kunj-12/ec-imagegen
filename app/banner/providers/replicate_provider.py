"""
Replicate text-to-image provider for banner backgrounds.

Uses Replicate's REST API directly (httpx, already a dependency) with
`Prefer: wait` so most predictions return in a single call, then polls if
needed. Model input names differ per model (aspect_ratio vs width/height,
output_format, ...): put them in BANNER_EXTRA_ARGS_JSON, e.g.
    BANNER_EXTRA_ARGS_JSON='{"aspect_ratio": "21:9", "output_format": "png"}'
`model` must be "owner/name" (latest version); pinned "owner/name:version"
strings are intentionally not supported here.
"""
import asyncio
import logging
import time

import httpx

from app.banner.providers.base import (
    BannerImageProvider,
    GeneratedBackground,
    call_with_retries,
    download_image,
)
from app.core.config import Settings
from app.inference.base import InferenceError

logger = logging.getLogger(__name__)

_API = "https://api.replicate.com/v1"
_TERMINAL = ("succeeded", "failed", "canceled")


class ReplicateBannerProvider(BannerImageProvider):
    name = "replicate"

    def __init__(self, settings: Settings, extra_args: dict | None = None):
        self._settings = settings
        self._extra_args = settings.banner_extra_args if extra_args is None else extra_args

    async def generate(self, *, prompt: str, model: str, width: int, height: int) -> GeneratedBackground:
        return await asyncio.to_thread(self._generate_sync, prompt, model)

    def _generate_sync(self, prompt: str, model: str) -> GeneratedBackground:
        s = self._settings
        logger.info("replicate banner starting: model=%s prompt_len=%d", model, len(prompt))
        return call_with_retries(
            lambda: self._call_once(prompt, model),
            max_retries=s.MAX_RETRIES,
            backoff_seconds=s.RETRY_BACKOFF_SECONDS,
            label="replicate banner call",
        )

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        detail = resp.text[:300]
        if resp.status_code in (401, 403):
            raise InferenceError(f"Replicate rejected the API token ({resp.status_code}): {detail}", retryable=False)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise InferenceError(f"Replicate transient error ({resp.status_code}): {detail}", retryable=True)
        raise InferenceError(f"Replicate rejected request ({resp.status_code}): {detail}", retryable=False)

    def _call_once(self, prompt: str, model: str) -> GeneratedBackground:
        s = self._settings
        if not s.REPLICATE_API_TOKEN:
            raise InferenceError("REPLICATE_API_TOKEN is not set", retryable=False)
        if "/" not in model or ":" in model:
            raise InferenceError(f"Model must be 'owner/name', got '{model}'", retryable=False)

        auth = {"Authorization": f"Bearer {s.REPLICATE_API_TOKEN}"}
        payload = {"input": {"prompt": prompt, **self._extra_args}}

        try:
            resp = httpx.post(
                f"{_API}/models/{model}/predictions",
                headers={**auth, "Prefer": "wait"},
                json=payload,
                timeout=s.REQUEST_TIMEOUT_SECONDS + 15,
            )
            self._raise_for_status(resp)
            data = resp.json()

            deadline = time.monotonic() + s.BANNER_POLL_TIMEOUT_SECONDS
            while data.get("status") not in _TERMINAL:
                if time.monotonic() > deadline:
                    raise InferenceError("Replicate prediction timed out", retryable=True)
                time.sleep(2)
                poll = httpx.get(data["urls"]["get"], headers=auth, timeout=30)
                self._raise_for_status(poll)
                data = poll.json()
        except httpx.HTTPError as e:
            raise InferenceError(f"Replicate network error: {e}", retryable=True) from e

        if data["status"] != "succeeded":
            raise InferenceError(
                f"Replicate prediction {data['status']}: {data.get('error')}",
                retryable=data["status"] == "failed",
            )

        output = data.get("output")
        url = output[0] if isinstance(output, list) and output else output if isinstance(output, str) else None
        if not url:
            raise InferenceError(f"Replicate returned no image [output={output!r}]", retryable=True)

        content, content_type = download_image(url, timeout=s.REQUEST_TIMEOUT_SECONDS)
        return GeneratedBackground(
            content=content,
            content_type=content_type,
            provider=self.name,
            model=model,
            cost_inr=s.BANNER_PRICE_PER_IMAGE_INR,
        )
