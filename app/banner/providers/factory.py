"""
Single place that picks the banner image provider, driven by
settings.BANNER_PROVIDER (.env). Same idea as app.inference.provider_factory,
but for text-to-image banner backgrounds. provider_name / extra_args overrides
exist for the benchmark script; the worker always uses settings.
"""
from app.banner.providers.base import BannerImageProvider
from app.core.config import Settings

_SUPPORTED = ("fal", "replicate")


def get_banner_provider(
    settings: Settings,
    provider_name: str | None = None,
    extra_args: dict | None = None,
) -> BannerImageProvider:
    name = (provider_name or settings.BANNER_PROVIDER).strip().lower()

    if name == "fal":
        from app.banner.providers.fal_provider import FalBannerProvider

        return FalBannerProvider(settings, extra_args)

    if name == "replicate":
        from app.banner.providers.replicate_provider import ReplicateBannerProvider

        return ReplicateBannerProvider(settings, extra_args)

    raise ValueError(f"Unknown BANNER_PROVIDER '{name}'. Must be one of {_SUPPORTED} (set in .env).")
