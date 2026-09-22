"""
Compositor: takes the AI-generated background and draws everything that must
be exact — restaurant name, headline, item names, ₹ price badges, tagline,
validity/terms — plus the optional hero food photo.

Why code and not the image model: models routinely garble prices and Indic
scripts. Drawing text here means the price on the banner is always the price
the merchant entered, in a font we control.

Requirements (see INTEGRATION.md):
  * Font files in BANNER_FONT_DIR (Noto Sans Bold / Devanagari Bold / Gujarati Bold).
  * Pillow built with libraqm for correct Hindi/Gujarati shaping (conjuncts,
    matras). Without it, Latin text is fine but Indic text renders incorrectly;
    a warning is logged at import time.
"""
import io
import logging
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, features

from app.banner.brief import Theme
from app.banner.schemas import BannerOffer, OfferItem

logger = logging.getLogger(__name__)


class BannerCompositionError(Exception):
    """Raised for non-retryable composition failures (missing font, bad hero image...)."""


_LATIN_FONT = "NotoSans-Bold.ttf"
_DEVANAGARI_FONT = "NotoSansDevanagari-Bold.ttf"
_GUJARATI_FONT = "NotoSansGujarati-Bold.ttf"

_PIL_FORMATS = {"png": "PNG", "webp": "WEBP", "jpg": "JPEG", "jpeg": "JPEG"}

_RAQM = features.check("raqm")
if not _RAQM:
    logger.warning(
        "Pillow was built WITHOUT libraqm: Hindi/Gujarati text will not be shaped "
        "correctly. Install libraqm (e.g. apt install libraqm0) and a Pillow build "
        "that links it before serving Indic-language banners."
    )


def font_file_for(text: str) -> str:
    """Pick the font by script. Keep each field single-script (e.g. all Gujarati)."""
    for ch in text:
        cp = ord(ch)
        if 0x0A80 <= cp <= 0x0AFF:
            return _GUJARATI_FONT
        if 0x0900 <= cp <= 0x097F:
            return _DEVANAGARI_FONT
    return _LATIN_FONT


@lru_cache(maxsize=256)
def _load_font(font_dir: str, filename: str, size: int) -> ImageFont.FreeTypeFont:
    path = Path(font_dir) / filename
    if not path.is_file():
        raise BannerCompositionError(f"Font file missing: {path}")
    engine = ImageFont.Layout.RAQM if _RAQM else ImageFont.Layout.BASIC
    return ImageFont.truetype(str(path), max(size, 8), layout_engine=engine)


def _line_h(font: ImageFont.FreeTypeFont) -> int:
    return int(font.size * 1.2)


def _fit(text: str, *, font_dir: str, max_w: int, max_size: int, min_size: int):
    """Largest font size (<= max_size) whose rendered width fits max_w; ellipsis as last resort."""
    filename = font_file_for(text)
    size = max(max_size, min_size)
    while True:
        font = _load_font(font_dir, filename, size)
        if font.getlength(text) <= max_w or size <= min_size:
            break
        size -= 2
    if font.getlength(text) > max_w:
        while len(text) > 1 and font.getlength(text + "…") > max_w:
            text = text[:-1]
        text = text.rstrip() + "…"
    return font, text


def _fmt_price(value: float) -> str:
    return f"₹{int(value)}" if float(value).is_integer() else f"₹{value:.2f}"


# --------------------------------------------------------------------------- #
# Background + readability scrim
# --------------------------------------------------------------------------- #
def _prepare_background(bg_bytes: bytes, theme: Theme, width: int, height: int) -> Image.Image:
    try:
        bg = Image.open(io.BytesIO(bg_bytes))
        bg.load()
    except Exception as e:
        raise BannerCompositionError("Generated background could not be decoded") from e

    bg = ImageOps.fit(bg.convert("RGB"), (width, height), Image.Resampling.LANCZOS).convert("RGBA")

    # Left-to-right gradient in the theme colour: guarantees readable text even
    # if the model ignored the "calm left side" instruction.
    max_alpha = 225
    solid_end, fade_end = int(width * 0.30), int(width * 0.66)

    def alpha_at(x: int) -> int:
        if x <= solid_end:
            return max_alpha
        if x >= fade_end:
            return 0
        return int(max_alpha * (fade_end - x) / (fade_end - solid_end))

    ramp = Image.new("L", (width, 1))
    ramp.putdata([alpha_at(x) for x in range(width)])
    scrim = Image.new("RGBA", (width, height), theme.scrim + (255,))
    scrim.putalpha(ramp.resize((width, height), Image.Resampling.NEAREST))
    return Image.alpha_composite(bg, scrim)


# --------------------------------------------------------------------------- #
# Hero food photo (right side)
# --------------------------------------------------------------------------- #
def _paste_hero(canvas: Image.Image, hero_bytes: bytes, width: int, height: int, remove_bg: bool) -> None:
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(hero_bytes)))
        img = img.convert("RGBA")
    except Exception as e:
        raise BannerCompositionError("Hero image could not be decoded") from e

    zone_w, zone_h = int(width * 0.36), int(height * 0.86)
    cx, cy = int(width * 0.79), height // 2

    if remove_bg:
        try:
            from rembg import remove  # optional dependency

            cut = remove(img).convert("RGBA")
            cut = ImageOps.contain(cut, (zone_w, zone_h), Image.Resampling.LANCZOS)
            pos = (cx - cut.width // 2, cy - cut.height // 2)
            shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            ImageDraw.Draw(shadow).ellipse(
                (pos[0], pos[1] + cut.height - int(height * 0.06), pos[0] + cut.width, pos[1] + cut.height),
                fill=(0, 0, 0, 120),
            )
            canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(12)))
            canvas.alpha_composite(cut, pos)
            return
        except Exception:
            logger.warning("Background removal unavailable/failed; using circular crop", exc_info=True)

    # Default: circular crop with a white ring and soft shadow.
    ring = max(4, min(zone_w, zone_h) // 60)
    d = min(zone_w, zone_h) - 2 * ring
    x0, y0 = cx - d // 2, cy - d // 2

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        (x0 - ring + 6, y0 - ring + 12, x0 + d + ring + 6, y0 + d + ring + 12), fill=(0, 0, 0, 110)
    )
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))

    ring_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(ring_layer).ellipse(
        (x0 - ring, y0 - ring, x0 + d + ring, y0 + d + ring), fill=(255, 255, 255, 255)
    )
    canvas.alpha_composite(ring_layer)

    photo = ImageOps.fit(img, (d, d), Image.Resampling.LANCZOS).convert("RGB")
    mask = Image.new("L", (d * 4, d * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, d * 4 - 1, d * 4 - 1), fill=255)
    canvas.paste(photo, (x0, y0), mask.resize((d, d), Image.Resampling.LANCZOS))


# --------------------------------------------------------------------------- #
# Text block (left side)
# --------------------------------------------------------------------------- #
def _draw_text_block(canvas: Image.Image, offer: BannerOffer, theme: Theme, width: int, height: int, font_dir: str) -> None:
    draw = ImageDraw.Draw(canvas)
    H = height
    mx = int(width * 0.05)
    max_w = int(width * 0.54)
    top_pad, bottom_pad, gap = int(H * 0.06), int(H * 0.05), int(H * 0.025)

    elements: list[tuple[int, Callable[[int], None]]] = []

    def add_line(text: str, *, max_size: int, min_size: int, fill) -> None:
        font, txt = _fit(text, font_dir=font_dir, max_w=max_w, max_size=max_size, min_size=min_size)
        elements.append((_line_h(font), lambda y: draw.text((mx, y), txt, font=font, fill=fill, anchor="la")))

    def add_item(item: OfferItem) -> None:
        badge_font = _load_font(font_dir, _LATIN_FONT, int(H * 0.07))
        price_txt = _fmt_price(item.price) if item.price is not None else None
        pad_x, pad_y = int(H * 0.028), int(H * 0.008)
        badge_w = int(badge_font.getlength(price_txt)) + 2 * pad_x if price_txt else 0
        badge_h = _line_h(badge_font) + 2 * pad_y if price_txt else 0
        bullet_d, bullet_gap, name_gap = int(H * 0.03), int(H * 0.025), int(H * 0.03)
        reserved = bullet_d + bullet_gap + ((name_gap + badge_w) if price_txt else 0)

        name_font, name_txt = _fit(
            item.name, font_dir=font_dir, max_w=max_w - reserved, max_size=int(H * 0.07), min_size=int(H * 0.045)
        )
        row_h = max(_line_h(name_font), badge_h)

        orig_txt = _fmt_price(item.original_price) if (price_txt and item.original_price is not None) else None
        orig_font = _load_font(font_dir, _LATIN_FONT, int(H * 0.045)) if orig_txt else None

        def draw_row(y: int) -> None:
            cy = y + row_h // 2
            draw.ellipse((mx, cy - bullet_d // 2, mx + bullet_d, cy + bullet_d // 2), fill=theme.accent)
            nx = mx + bullet_d + bullet_gap
            draw.text((nx, cy), name_txt, font=name_font, fill=theme.text, anchor="lm")
            if not price_txt:
                return
            bx = nx + int(name_font.getlength(name_txt)) + name_gap
            draw.rounded_rectangle(
                (bx, cy - badge_h // 2, bx + badge_w, cy + badge_h // 2), radius=badge_h // 2, fill=theme.badge_bg
            )
            draw.text((bx + badge_w / 2, cy), price_txt, font=badge_font, fill=theme.badge_text, anchor="mm")
            if orig_txt:
                ox = bx + badge_w + name_gap // 2
                ow = int(orig_font.getlength(orig_txt))
                if ox + ow <= mx + max_w:  # skip if it would spill into the hero zone
                    draw.text((ox, cy), orig_txt, font=orig_font, fill=theme.text, anchor="lm")
                    draw.line((ox, cy, ox + ow, cy), fill=theme.text, width=max(2, int(H * 0.006)))

        elements.append((row_h, draw_row))

    add_line(offer.restaurant_name, max_size=int(H * 0.14), min_size=int(H * 0.08), fill=theme.text)
    if offer.headline:
        add_line(offer.headline, max_size=int(H * 0.10), min_size=int(H * 0.06), fill=theme.accent)
    for item in offer.items:
        add_item(item)
    if offer.tagline:
        add_line(offer.tagline, max_size=int(H * 0.045), min_size=int(H * 0.032), fill=theme.text)

    footer_txt = " - ".join(p for p in (offer.valid_till, offer.terms) if p)
    footer_font, footer_txt = (
        _fit(footer_txt, font_dir=font_dir, max_w=max_w, max_size=int(H * 0.04), min_size=int(H * 0.03))
        if footer_txt
        else (None, "")
    )
    footer_h = _line_h(footer_font) if footer_font else 0

    total = sum(h for h, _ in elements) + gap * max(0, len(elements) - 1)
    available = H - top_pad - bottom_pad - footer_h - gap
    if total > available:
        logger.warning("Banner text block taller than available space (%d > %d); top-aligning", total, available)
        y = top_pad
    else:
        y = top_pad + (available - total) // 2

    for h, fn in elements:
        fn(y)
        y += h + gap

    if footer_font:
        draw.text((mx, H - bottom_pad - footer_h), footer_txt, font=footer_font, fill=theme.text, anchor="la")


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def compose_banner(
    *,
    background: bytes,
    offer: BannerOffer,
    theme: Theme,
    hero: bytes | None,
    width: int,
    height: int,
    font_dir: str,
    output_format: str = "png",
    remove_hero_bg: bool = False,
) -> bytes:
    fmt = _PIL_FORMATS.get(output_format.lower())
    if fmt is None:
        raise BannerCompositionError(f"Unsupported BANNER_OUTPUT_FORMAT '{output_format}'")

    canvas = _prepare_background(background, theme, width, height)
    if hero:
        _paste_hero(canvas, hero, width, height, remove_hero_bg)
    _draw_text_block(canvas, offer, theme, width, height, font_dir)

    out = canvas.convert("RGB") if fmt == "JPEG" else canvas
    buf = io.BytesIO()
    save_kwargs = {"quality": 92} if fmt in ("JPEG", "WEBP") else {"optimize": True}
    out.save(buf, fmt, **save_kwargs)
    return buf.getvalue()
