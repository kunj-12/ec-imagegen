"""
Banner image-provider interface (text-to-image background generation).

Deliberately separate from app.inference.base.InferenceProvider: that one is
image-to-image and requires a source photo; banners start from text only and
need a wide aspect ratio. The restyle providers are untouched.
"""
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, TypeVar

import httpx

from app.inference.base import InferenceError

logger = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass
class GeneratedBackground:
    content: bytes
    content_type: str  # e.g. "image/png"
    provider: str
    model: str
    cost_inr: float


class BannerImageProvider(ABC):
    name: str

    @abstractmethod
    async def generate(self, *, prompt: str, model: str, width: int, height: int) -> GeneratedBackground:
        """Generate one background image from a text prompt."""
        raise NotImplementedError


def call_with_retries(fn: Callable[[], T], *, max_retries: int, backoff_seconds: float, label: str) -> T:
    """Retry fn() on retryable InferenceError only; non-retryable errors surface immediately."""
    last_err: InferenceError | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except InferenceError as e:
            last_err = e
            if not e.retryable or attempt == max_retries:
                raise
            wait = backoff_seconds * attempt
            logger.warning("%s failed (attempt %s/%s): %s - retrying in %.1fs", label, attempt, max_retries, e, wait)
            time.sleep(wait)
    raise InferenceError(f"{label} failed: {last_err}", retryable=False)


def download_image(url: str, *, timeout: float) -> tuple[bytes, str]:
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise InferenceError(f"Could not download generated image: {e}", retryable=True) from e
    content_type = resp.headers.get("content-type", "image/png").split(";")[0].strip()
    return resp.content, content_type
