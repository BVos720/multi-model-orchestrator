from __future__ import annotations

# Tiny 5-row block font, just the letters OrchestCLI needs. Defined as data
# and rendered by joining equal-width glyphs, so columns can't drift out of
# alignment the way hand-typed ASCII art easily does.
_FONT: dict[str, list[str]] = {
    "O": [" ███ ", "█   █", "█   █", "█   █", " ███ "],
    "R": ["████ ", "█   █", "████ ", "█  █ ", "█   █"],
    "C": [" ████", "█    ", "█    ", "█    ", " ████"],
    "H": ["█   █", "█   █", "█████", "█   █", "█   █"],
    "E": ["█████", "█    ", "████ ", "█    ", "█████"],
    "S": [" ████", "█    ", " ███ ", "    █", "████ "],
    "T": ["█████", "  █  ", "  █  ", "  █  ", "  █  "],
    "L": ["█    ", "█    ", "█    ", "█    ", "█████"],
    "I": ["█████", "  █  ", "  █  ", "  █  ", "█████"],
    " ": ["  ", "  ", "  ", "  ", "  "],
}


def render(text: str, gap: str = " ") -> str:
    """Render `text` (letters must exist in _FONT) as a 5-row block banner."""
    letters = [_FONT[ch] for ch in text.upper()]
    rows = []
    for row_i in range(5):
        rows.append(gap.join(letter[row_i] for letter in letters))
    return "\n".join(rows)


BANNER = render("OrchestCLI")
