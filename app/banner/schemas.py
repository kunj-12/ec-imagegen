from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Only one creative type today. Add e.g. "promo_poster" here later and branch
# on it in brief.py / compositor.py — the table, queue and API stay the same.
CreativeType = Literal["offer_banner"]
SUPPORTED_CREATIVE_TYPES = ("offer_banner",)


class OfferItem(BaseModel):
    model_config = {"str_strip_whitespace": True}

    name: str = Field(min_length=1, max_length=50)
    price: float | None = Field(default=None, ge=0, le=100000)
    original_price: float | None = Field(default=None, ge=0, le=100000)


class BannerOffer(BaseModel):
    """
    Everything the merchant wants printed on the banner. All of this text is
    drawn by code (compositor.py), never by the image model, so prices are
    always exact.
    """
    model_config = {"str_strip_whitespace": True}

    restaurant_name: str = Field(min_length=1, max_length=40)
    headline: str | None = Field(default=None, max_length=40)  # e.g. "FLAT 20% OFF"
    items: list[OfferItem] = Field(default_factory=list, max_length=3)
    tagline: str | None = Field(default=None, max_length=60)
    valid_till: str | None = Field(default=None, max_length=30)
    terms: str = Field(default="T&C apply", max_length=40)
    # Optional theme name (see brief.THEMES). None = auto-cycle per attempt.
    style: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _needs_content(self):
        if not self.headline and not self.items:
            raise ValueError("Provide a headline or at least one item.")
        return self


class BannerJobOut(BaseModel):
    model_config = {"protected_namespaces": (), "from_attributes": True}

    id: int
    batch_id: str
    restaurant_id: str
    creative_type: str
    status: str
    variation_index: int
    theme: str | None
    provider: str | None
    model_used: str | None
    is_selected: bool
    image_path: str | None
    cost_inr: float | None
    error_message: str | None
    created_at: datetime


class RegenerateBannerRequest(BaseModel):
    batch_id: str


class SelectBannerRequest(BaseModel):
    job_id: int
