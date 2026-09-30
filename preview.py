"""Render a 1200×630 social card for one share."""

from io import BytesIO

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

SIZE = (1200, 630)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def font(size, bold=False):
    return ImageFont.truetype(BOLD if bold else FONT, size)


def wrap(draw, text, face, width):
    lines = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=face) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or ["Shared video"]


def render_card(title, scope, kind, poster=None):
    base = Image.new("RGB", SIZE, "#121720")
    artwork = None
    if poster:
        try:
            with Image.open(poster) as source:
                artwork = source.convert("RGB")
        except (OSError, ValueError):
            artwork = None
    if artwork:
        atmosphere = ImageOps.fit(artwork, SIZE, method=Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(42))
        base = Image.blend(atmosphere, base, .82)
    draw = ImageDraw.Draw(base)
    draw.rounded_rectangle((38, 36, 1162, 594), radius=28, outline="#8d775c", width=2)
    draw.rounded_rectangle((70, 66, 128, 124), radius=15, fill="#e9ad70")
    draw.polygon([(93, 79), (93, 111), (113, 95)], fill="#181c23")
    draw.text((146, 82), "SHARED VIEWING", font=font(18, True), fill="#e3e5e2", stroke_width=0)

    if artwork:
        portrait = ImageOps.fit(artwork, (336, 500), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", portrait.size)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 335, 499), radius=20, fill=255)
        base.paste(portrait, (802, 65), mask)
        draw.rounded_rectangle((801, 64, 1139, 566), radius=21, outline="#e9ad70", width=2)
    else:
        draw.rounded_rectangle((808, 88, 1120, 530), radius=26, fill="#252d39", outline="#675c52", width=2)
        draw.ellipse((861, 160, 1066, 365), outline="#e9ad70", width=5)
        draw.polygon([(940, 209), (940, 318), (1013, 263)], fill="#e9ad70")
        for offset in (0, 1, 2):
            y = 402 + offset * 26
            draw.rounded_rectangle((866, y, 1060 - offset * 22, y + 7), radius=3, fill="#695a4c")

    width = 660
    for size in range(66, 39, -2):
        title_font = font(size, True)
        lines = wrap(draw, title, title_font, width)
        if len(lines) <= 3:
            break
    if len(lines) > 3:
        lines = lines[:3]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=title_font) > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    y = 183
    line_height = size + 15
    for line in lines:
        draw.text((70, y), line, font=title_font, fill="#fff9f1")
        y += line_height
    descriptor = scope or ("Movie" if kind == "movie" else "Complete series")
    descriptor = descriptor.upper()
    draw.rounded_rectangle((70, 493, 746, 554), radius=12, fill="#e9ad70")
    label_font = font(19, True)
    while draw.textlength(descriptor, font=label_font) > 636 and len(descriptor) > 4:
        descriptor = descriptor[:-2] + "…"
    draw.text((94, 512), descriptor, font=label_font, fill="#1b1c20")
    output = BytesIO()
    base.save(output, format="JPEG", quality=88, optimize=True, progressive=True)
    return output.getvalue()
