"""Jake's sprites, cut out (transparent) from the sprite sheet.

No procedural squashing: the animation is a SWITCH between frames.
For every frame we pre-compute the "arcane glow" (the dilated, tinted
silhouette) ONCE into a cached surface, so drawing costs 2 blits per frame
instead of ~36 mask_surface calls -> far less CPU and battery.
"""

from __future__ import annotations

import math
from pathlib import Path

import cairo

FRAMES = Path(__file__).resolve().parent.parent / "assets" / "frames"

GLOW_PAD = 8  # margin around the sprite to fit the halo


def _load(name: str) -> cairo.ImageSurface:
    return cairo.ImageSurface.create_from_png(str(FRAMES / f"{name}.png"))


def _make_glow(surf: cairo.ImageSurface) -> cairo.ImageSurface:
    """Dilated, tinted silhouette, rendered exactly once."""
    w = surf.get_width() + 2 * GLOW_PAD
    h = surf.get_height() + 2 * GLOW_PAD
    glow = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(glow)
    # soft outer halo
    for r, alpha in ((7.0, 0.05), (5.0, 0.09)):
        for a in range(0, 360, 30):
            dx = r * math.cos(math.radians(a))
            dy = r * math.sin(math.radians(a))
            cr.set_source_rgba(1.0, 0.72, 0.15, alpha)
            cr.mask_surface(surf, GLOW_PAD + dx, GLOW_PAD + dy)
    # crisp bright rim around the outline
    for a in range(0, 360, 15):
        dx = 2.0 * math.cos(math.radians(a))
        dy = 2.0 * math.sin(math.radians(a))
        cr.set_source_rgba(1.0, 0.93, 0.5, 0.6)
        cr.mask_surface(surf, GLOW_PAD + dx, GLOW_PAD + dy)
    return glow


class Sprites:
    def __init__(self) -> None:
        self.idle = [_load(f"idle_{i}") for i in range(4)]
        self.walk = [_load(f"walk_{i}") for i in range(4)]
        self.talk = self.idle            # "talking" reuses the idle frames
        # cached glow, index-aligned with idle/walk
        self.idle_glow = [_make_glow(s) for s in self.idle]
        self.walk_glow = [_make_glow(s) for s in self.walk]
        self.pad = GLOW_PAD
        self.max_h = max(s.get_height() for s in self.idle + self.walk)

    @staticmethod
    def size(surface: cairo.ImageSurface) -> tuple[int, int]:
        return surface.get_width(), surface.get_height()
