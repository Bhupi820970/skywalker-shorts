"""15 caption patterns, ported from pro-edit (Vice Cartel) — all variations of the approved GTA code edit.

Every pattern keeps the approved caption look: words BESIDE the speaker on the side away from
their face, alternating left/right when there is no face, never covering a face (mirrored /
nudged if they would), Anton font, hard black drop-shadow + outline, the same 80%->100% pop-in,
key words big + filler words small and white, every key word a different colour.

Only the placement rhythm, offsets, word grouping and size mix change between patterns:
  y      : vertical anchor per caption chunk, in face-heights from the face centre, cycled
           through the list (e.g. [0] = beside-mid, [-0.6] = beside-upper, [-0.6, 0, 0.6] = step down)
  gap    : horizontal distance from the face centre, in face-widths (approved = 0.9)
  nudge  : extra shift toward the frame centre (fraction of width)
  words  : max words per caption chunk (approved = 3)
  key/key_long/filler : text heights as a fraction of frame height (approved .098/.082/.052)
  no_face_y : vertical position (fraction of height) when nobody's face is visible
Every palette has 4-5 colours and includes the approved hot pink (255, 46, 196).
P01 is the approved edit exactly and is also the renderer default.
"""
from __future__ import annotations

PINK = (255, 46, 196)          # exact pink from vice_style_code (vicestyle.NEON["pink"])
CYAN, YELLOW, PURPLE = (0, 238, 255), (255, 228, 40), (178, 92, 255)
ORANGE, LIME, GOLD = (255, 140, 0), (170, 255, 0), (255, 200, 40)
SKY, TEAL, CORAL = (100, 200, 255), (0, 222, 200), (255, 110, 90)

_P = [
    # id, name, y-rhythm, gap, nudge, words, (key, key_long, filler), palette, no_face_y
    ("P01", "Original beside-mid (approved)", [0.0], 0.9, 0.0, 3, (.098, .082, .052), [CYAN, PINK, YELLOW, PURPLE], .30),
    ("P02", "Beside-upper", [-0.65], 0.9, 0.0, 3, (.098, .082, .052), [PINK, CYAN, YELLOW, ORANGE], .22),
    ("P03", "Beside-lower", [0.75], 0.9, 0.0, 3, (.096, .080, .050), [YELLOW, PINK, CYAN, LIME], .45),
    ("P04", "Stepping down beside", [-0.6, 0.0, 0.6], 0.9, 0.0, 3, (.098, .082, .052), [CYAN, PINK, PURPLE, GOLD], .30),
    ("P05", "Stepping up beside", [0.6, 0.0, -0.6], 0.9, 0.0, 3, (.098, .082, .052), [PINK, TEAL, YELLOW, PURPLE], .30),
    ("P06", "Close + nudge to centre", [0.0], 0.75, 0.035, 3, (.094, .080, .050), [PINK, ORANGE, CYAN, YELLOW], .30),
    ("P07", "Far beside", [-0.2], 1.25, 0.0, 3, (.098, .082, .052), [PURPLE, PINK, CYAN, LIME], .28),
    ("P08", "2-word punch, mid", [0.0], 0.9, 0.0, 2, (.108, .090, .052), [PINK, YELLOW, SKY, ORANGE], .30),
    ("P09", "2-word upper", [-0.55], 0.95, 0.0, 2, (.104, .088, .050), [CYAN, PINK, GOLD, PURPLE], .24),
    ("P10", "Big keys, tiny fillers", [0.0], 0.9, 0.0, 3, (.116, .096, .042), [PINK, CYAN, YELLOW, PURPLE, ORANGE], .30),
    ("P11", "Even mix", [-0.1], 0.9, 0.0, 3, (.088, .078, .064), [PINK, LIME, CYAN, YELLOW], .30),
    ("P12", "Upper/lower tick-tock beside", [-0.55, 0.55], 0.9, 0.0, 3, (.098, .082, .052), [PINK, SKY, YELLOW, PURPLE], .30),
    ("P13", "Wave beside (up-mid-down-mid), 2 words", [-0.55, 0.0, 0.55, 0.0], 0.9, 0.0, 2, (.102, .086, .050), [ORANGE, PINK, CYAN, LIME], .30),
    ("P14", "Upper + nudge, bold keys", [-0.5], 0.8, 0.03, 3, (.106, .088, .050), [PINK, PURPLE, GOLD, CYAN, CORAL], .24),
    ("P15", "Lower far, 2 words", [0.6], 1.15, 0.0, 2, (.100, .084, .050), [TEAL, PINK, YELLOW, ORANGE], .42),
]
PATTERNS = [{"id": i, "name": n, "y": y, "gap": g, "nudge": nu, "words": w, "key": k[0], "key_long": k[1],
             "filler": k[2], "keys": pal, "no_face_y": nf, "classic": i == "P01"}
            for i, n, y, g, nu, w, k, pal, nf in _P]
ROTATION = [p["id"] for p in PATTERNS]
BY_ID = {p["id"]: p for p in PATTERNS}
BY_ID["P00"] = BY_ID["original_gta"] = BY_ID["P01"]
assert all(PINK in p["keys"] and 4 <= len(p["keys"]) <= 5 for p in PATTERNS)


def get(pid: str | None) -> dict:
    return BY_ID.get(pid or "P01", PATTERNS[0])

