"""
Creative brief: turns a BannerOffer into
  (a) a colour Theme used by the compositor for text/badges/scrim, and
  (b) the text-to-image prompt for the background artwork.

v1 is rule-based: free, deterministic, no extra API call. An LLM-driven brief
can replace build_brief() later without touching anything else, as long as it
returns the same BannerBrief.

The prompt deliberately asks for NO text/numbers: the model only paints
artwork; all readable text is drawn by code afterwards.

Dish-name handling (right_zone, no-hero path):
  - Only ONE dish is described to the image model, never all of them joined.
    Asking a text-to-image model to render 2-3 unrelated dishes convincingly
    in a single photorealistic hero shot reliably produces worse, muddier
    results than describing one dish well.
  - Only Latin-script item names are sent to the model. Text-to-image models
    do not reliably understand Devanagari/Gujarati/other non-Latin script as
    food semantics — feeding them through produces unrelated or garbled
    imagery (e.g. a Gujarati dish name has produced a plate of spaghetti in
    testing). Non-Latin names fall back to a generic (but still appetizing,
    still Indian-food-styled) subject instead of being passed through raw.
  - No dish-specific descriptions are hardcoded anywhere in this file. What
    a "momo" or a "dabeli" looks like is left entirely to the image model's
    own training + the generic photography direction below (angle, lighting,
    depth of field) — this file only ever forwards what the merchant typed,
    it never invents visual details per dish. That keeps this maintainable
    without a growing, always-incomplete dish dictionary.
"""
import re
from dataclasses import dataclass

from app.banner.schemas import BannerOffer

RGB = tuple[int, int, int]

# ASCII letters/digits + common punctuation found in dish names ("Mac & Cheese",
# "Paneer Tikka (Spicy)", "Cold-Brew"). Anything outside this range (Gujarati,
# Devanagari, other scripts) is treated as unsafe to hand to the image model.
_LATIN_SAFE_RE = re.compile(r"^[\x00-\x7F\s.,'&()/\-]*$")


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


def _is_model_safe_text(text: str) -> bool:
    """
    True if `text` is plain Latin script (ASCII letters/digits/basic
    punctuation) and therefore safe to hand to an English-prompted
    text-to-image model as a food subject description.

    Uses the same idea as compositor.font_file_for() (Unicode range
    detection), but for a different purpose: font_file_for() picks which
    font can *render* a string; this decides whether the image model can
    plausibly *understand* it as a food name. A dish name can be perfectly
    renderable by a Devanagari font and still be meaningless to the model.
    """
    text = text.strip()
    return bool(text) and bool(_LATIN_SAFE_RE.match(text))


def _primary_dish_subject(offer: BannerOffer) -> str | None:
    """
    Pick the first model-safe (Latin-script) item name to use as the AI
    hero-shot subject. Returns None if there are no items, or none of the
    item names are safe to send to the model (e.g. all Gujarati/Hindi) —
    callers should fall back to a generic subject in that case.
    """
    for item in offer.items:
        if _is_model_safe_text(item.name):
            return item.name.strip()
    return None


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
            "a clean, softly lit open area with warm natural directional light "
            "and a subtle soft shadow gradient on the surface, gentle depth, "
            "where a product photo will be placed later; keep it free of any "
            "food, plates, or objects"
        )
    else:
        dish_name = _primary_dish_subject(offer)
        subject = f"a plate of {dish_name}" if dish_name else "an appetizing, well-plated Indian dish"

        right_zone = (
            f"a professional food-photography hero shot of {subject}, "
            "photographed from a slight top-down three-quarter angle, "
            "soft natural side lighting, shallow depth of field with a gently "
            "blurred background, rich natural colours and visible texture, "
            "a wisp of steam or fresh garnish for appetite appeal, "
            "on a clean minimal surface, styled like a premium food delivery app photo"
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
