"""
Creative brief: turns a BannerOffer into
  (a) a colour Theme used by the compositor for text/badges/scrim, and
  (b) the text-to-image prompt for the background artwork.

v1 is rule-based: free, deterministic, no extra API call. An LLM-driven brief
can replace build_brief() later without touching anything else, as long as it
returns the same BannerBrief.

The prompt deliberately asks for NO text/numbers: the model only paints
artwork; all readable text is drawn by code afterwards.
"""
from dataclasses import dataclass

from app.banner.schemas import BannerOffer

RGB = tuple[int, int, int]


@dataclass(frozen=True)
class Theme:
    name: str
    palette_prompt: str  # colour words injected into the image prompt
    text: RGB  # main text colour (drawn over the scrim)
    accent: RGB  # headline / bullets
    badge_bg: RGB  # price pill background
    badge_text: RGB  # price pill text
    scrim: RGB  # colour of the left-side readability gradient


THEMES: tuple[Theme, ...] = (
    Theme("sunshine", "warm golden yellow and soft orange",
          text=(60, 30, 10), accent=(190, 60, 10), badge_bg=(20, 110, 60),
          badge_text=(255, 255, 255), scrim=(255, 205, 50)),
    Theme("emerald", "deep emerald green with subtle gold accents",
          text=(255, 255, 255), accent=(255, 214, 90), badge_bg=(255, 214, 90),
          badge_text=(10, 70, 45), scrim=(8, 70, 48)),
    Theme("spice", "rich maroon and warm red with golden accents",
          text=(255, 255, 255), accent=(255, 215, 120), badge_bg=(255, 215, 120),
          badge_text=(110, 15, 20), scrim=(110, 18, 25)),
    Theme("midnight", "deep navy blue and violet with warm orange highlights",
          text=(255, 255, 255), accent=(255, 160, 90), badge_bg=(255, 150, 60),
          badge_text=(30, 20, 10), scrim=(22, 28, 70)),
    Theme("fresh", "light cream and mint green with fresh leafy accents",
          text=(20, 70, 40), accent=(215, 85, 20), badge_bg=(215, 85, 20),
          badge_text=(255, 255, 255), scrim=(242, 250, 236)),
)
THEMES_BY_NAME = {t.name: t for t in THEMES}


@dataclass(frozen=True)
class BannerBrief:
    theme: Theme
    prompt: str


def pick_theme(style: str | None, variation_index: int) -> Theme:
    """
    Merchant pinned a style -> keep it for every attempt (only the artwork
    varies between regenerations). Otherwise cycle themes per attempt so each
    "Regenerate" looks visibly different.
    """
    if style:
        pinned = THEMES_BY_NAME.get(style.strip().lower())
        if pinned:
            return pinned
    return THEMES[variation_index % len(THEMES)]


def build_brief(
    offer: BannerOffer,
    *,
    variation_index: int,
    has_hero: bool,
    width: int,
    height: int,
) -> BannerBrief:
    theme = pick_theme(offer.style, variation_index)

    if has_hero:
        right_zone = (
            "a clean, softly lit open area with gentle depth where a product photo "
            "will be placed later; keep it free of any food, plates or objects"
        )
    else:
        dishes = ", ".join(i.name for i in offer.items) or "a signature dish"
        right_zone = (
            f"a beautiful, appetizing, photorealistic hero shot of {dishes}, "
            "professionally styled for commercial food advertising"
        )

    prompt = (
        f"Professional promotional banner background for a food delivery app, wide "
        f"landscape composition ({width}x{height}). "
        f"Colour palette: {theme.palette_prompt}. "
        "Clean modern graphic design: smooth soft gradient, subtle decorative shapes, "
        "and a few small scattered ingredient accents (herbs, spices, leaves) along the "
        "edges. "
        "LEFT 55% of the frame: calm, low-detail, uncluttered area reserved for text. "
        f"RIGHT 40% of the frame: {right_zone}. "
        "Absolutely no text, no letters, no numbers, no logos, no watermark, no people."
    )
    return BannerBrief(theme=theme, prompt=prompt)
