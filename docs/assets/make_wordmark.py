#!/usr/bin/env python3
"""PAuthGateway wordmark generator.

Lockup: [gate mark] + "PAuth" (heavy) + "Gateway" (mid tone).
The mark encodes default-deny: several tool calls arrive, only the one the
plan authorized passes through the wall.
"""
from PIL import Image, ImageDraw, ImageFont

FONT = '/System/Library/Fonts/Avenir Next.ttc'
IDX_DEMI = 2      # Avenir Next Demi Bold
IDX_MED = 5       # Avenir Next Medium

U = 128           # design units: the mark is U x U
SS = 4            # supersample
SCALE = 3         # export scale (design unit -> px)

THEMES = {
    'light': dict(
        primary=(41, 90, 91),        # #295A5B  gate + passing call
        muted=(193, 198, 189),       # #C1C6BD  blocked calls
        word1=(23, 56, 57),          # #173839  "PAuth"
        word2=(58, 122, 120),        # #3A7A78  "Gateway"
    ),
    'dark': dict(
        primary=(122, 187, 182),     # #7ABBB6
        muted=(74, 86, 82),          # #4A5652
        word1=(240, 243, 239),       # #F0F3EF
        word2=(122, 187, 182),       # #7ABBB6
    ),
}


def draw_mark(d, ox, oy, s, c):
    """Gate mark inside a U x U box whose top-left is (ox, oy). s = px per unit."""
    def X(v): return ox + v * s
    def Y(v): return oy + v * s

    w = 14                     # stroke weight, design units
    wall_x0, wall_x1 = 60, 76  # the gate wall
    slot_y0, slot_y1 = 53, 75  # the opening the authorized call goes through
    r = 8

    def arrow(x0, tip, cy, col, head=26, half=18):
        d.rounded_rectangle([X(x0), Y(cy - w / 2), X(tip - head + 4), Y(cy + w / 2)],
                            radius=w / 2 * s, fill=col)
        d.polygon([(X(tip - head), Y(cy - half)), (X(tip - head), Y(cy + half)),
                   (X(tip), Y(cy))], fill=col)

    # --- unauthorized tool calls: stopped at the wall ---
    arrow(2, 50, 26, c['muted'])
    arrow(2, 50, 102, c['muted'])

    # --- the wall, split by the slot ---
    d.rounded_rectangle([X(wall_x0), Y(3), X(wall_x1), Y(slot_y0)],
                        radius=r * s, fill=c['primary'])
    d.rounded_rectangle([X(wall_x0), Y(slot_y1), X(wall_x1), Y(125)],
                        radius=r * s, fill=c['primary'])

    # --- the authorized call: straight through ---
    arrow(2, 126, 64, c['primary'], head=30, half=21)


def cap_height(font, ch='H'):
    sz = getattr(font, 'size', 100)
    t = Image.new('L', (int(sz * 4) + 400, int(sz * 4) + 400), 0)
    ImageDraw.Draw(t).text((100, 100), ch, font=font, fill=255)
    b = t.getbbox()
    return b[3] - b[1], b


def build(theme):
    c = THEMES[theme]
    s = SS * SCALE                      # px per design unit while drawing

    CAP = 86                            # wordmark cap height, design units
    GAP = 42                            # mark -> wordmark gap

    # size the font so its cap height matches CAP
    probe_sz = 400
    ratio = cap_height(ImageFont.truetype(FONT, probe_sz, index=IDX_DEMI))[0] / probe_sz
    size = round(CAP * s / ratio)
    f1 = ImageFont.truetype(FONT, size, index=IDX_DEMI)
    f2 = ImageFont.truetype(FONT, size, index=IDX_DEMI)

    probe = Image.new('L', (1, 1))
    pd = ImageDraw.Draw(probe)
    w1 = pd.textlength('PAuth', font=f1)
    w2 = pd.textlength('Gateway', font=f2)

    pad = 4                             # design units of breathing room
    Wd = pad + U + GAP + (w1 + w2) / s + pad
    Hd = U + 2 * pad

    W, H = round(Wd * s), round(Hd * s)
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    draw_mark(d, pad * s, pad * s, s, c)

    # baseline: centre the cap block of the wordmark on the mark's centre line
    _, cb = cap_height(f1)
    ascent_to_cap = cb[1] - 100         # px from draw-origin to cap top
    cap_px = cb[3] - cb[1]
    mark_cy = (pad + U / 2) * s
    ty = mark_cy - cap_px / 2 - ascent_to_cap
    tx = (pad + U + GAP) * s

    d.text((tx, ty), 'PAuth', font=f1, fill=c['word1'])
    d.text((tx + w1, ty), 'Gateway', font=f2, fill=c['word2'])

    out = img.resize((round(W / SS), round(H / SS)), Image.LANCZOS)
    path = f'docs/assets/wordmark-{theme}.png'
    out.save(path)
    print(path, out.size, 'font size', size)


for t in THEMES:
    build(t)
