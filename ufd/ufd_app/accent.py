"""Team accent colours.

The interface itself is neutral (light or dark). The only colour that changes from club to club is
the accent: buttons, active links, key numbers, bars and highlights. This module turns a club's
colours ("Royal Blue / White") into an accent that stays readable in both themes:

* ``fill``  - buttons, bars and badges; at least 3:1 against the surface (WCAG non-text contrast)
* ``ink``   - text on a fill; at least 4.5:1
* ``text``  - the accent used as text on a surface; at least 4.5:1

The club's hue is kept; only its lightness moves. Clubs whose colours are only black and white
get a black accent in the light theme and a white one in the dark theme.
"""
import colorsys
import re
from functools import lru_cache

# The surfaces each theme draws on (keep in step with the tokens in partials/head.html).
THEMES = {
    "light": {"fill_against": ("#ffffff", "#eceff3"), "text_against": ("#ffffff", "#f2f4f7", "#eceff3"), "direction": -1},
    "dark": {"fill_against": ("#0c0d10", "#15171c"), "text_against": ("#0c0d10", "#15171c", "#000000"), "direction": 1},
}
DEFAULT_COLOURS = ("#16a34a",)
DARK_INK, LIGHT_INK = "#0b0f14", "#ffffff"
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _rgb(value):
    return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))


def _hex(rgb):
    return "#" + "".join(f"{round(max(0, min(1, c)) * 255):02x}" for c in rgb)


def luminance(value):
    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in _rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _worst(value, surfaces):
    return min(contrast(value, s) for s in surfaces)


def is_chromatic(value):
    _, lightness, saturation = colorsys.rgb_to_hls(*_rgb(value))
    return saturation >= 0.25 and 0.18 <= lightness <= 0.9


def tune(value, surfaces, minimum, direction):
    """Move the colour's lightness (hue and saturation kept) until it meets ``minimum`` on every surface."""
    h, lightness, s = colorsys.rgb_to_hls(*_rgb(value))
    current = value
    for _ in range(100):
        if _worst(current, surfaces) >= minimum:
            return current
        lightness += direction * 0.01
        if not 0 <= lightness <= 1:
            break
        current = _hex(colorsys.hls_to_rgb(h, lightness, s))
    return current


def _ink(fill):
    return LIGHT_INK if contrast(fill, LIGHT_INK) >= contrast(fill, DARK_INK) else DARK_INK


def _pick(colours, theme):
    """The club colour that becomes this theme's accent, and a second colour for gradients."""
    rules = THEMES[theme]
    chromatic = [c for c in colours if is_chromatic(c)]
    for base in chromatic:
        fill = tune(base, rules["fill_against"], 3.0, rules["direction"])
        text = tune(base, rules["text_against"], 4.5, rules["direction"])
        others = [c for c in chromatic if c != base]
        second = tune(others[0], rules["fill_against"], 3.0, rules["direction"]) if others else fill
        return fill, text, second
    for base in colours:  # black and white clubs
        if _worst(base, rules["text_against"]) >= 4.5:
            return base, base, base
    fallback = "#e5e7eb" if theme == "dark" else "#111827"
    return fallback, fallback, fallback


def clean_colours(hexes):
    seen, out = set(), []
    for value in hexes or []:
        if isinstance(value, str) and _HEX.match(value) and value.lower() not in seen:
            seen.add(value.lower())
            out.append(value.lower())
    return out


@lru_cache(maxsize=64)
def accent_tokens(colours):
    """{'light': {...}, 'dark': {...}} CSS values for a tuple of club hex colours."""
    colours = tuple(clean_colours(colours)) or DEFAULT_COLOURS
    tokens = {}
    for theme in THEMES:
        fill, text, second = _pick(colours, theme)
        tokens[theme] = {"a-fill": fill, "a-ink": _ink(fill), "accent-text": text, "club-a": fill, "club-b": second}
    return tokens


def accent_css(colours):
    """A stylesheet body overriding the accent variables for one club (hex values only, so it is safe to inline)."""
    tokens = accent_tokens(tuple(colours or ()))
    return "\n".join(
        f':root[data-theme="{theme}"] {{ ' + " ".join(f"--{k}: {v};" for k, v in values.items()) + " }"
        for theme, values in tokens.items()
    )
