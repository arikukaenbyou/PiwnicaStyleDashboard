"""Place dashboard panels in the free space around a character on the wallpaper.

The holdout mask (bool H x W, True = character) comes from a PNG whose alpha marks the
character. A panel that does not fit is skipped -- it never covers the character.
"""
import numpy as np

MARGIN = 20


def free_spot(mask, w, h, anchor, margin=MARGIN, step=8):
    """Top-left corner of a w x h rectangle that touches no masked pixel, or None.
    anchor: 'top' (from the top, centred), 'bl' / 'br' (bottom-left / bottom-right corner)."""
    H, W = mask.shape
    if w + 2 * margin > W or h + 2 * margin > H:
        return None
    ii = np.pad(mask.astype(np.int32).cumsum(0).cumsum(1), ((1, 0), (1, 0)))

    def clear(x, y):
        x0, y0 = max(0, x - margin), max(0, y - margin)
        x1, y1 = min(W, x + w + margin), min(H, y + h + margin)
        return ii[y1, x1] - ii[y0, x1] - ii[y1, x0] + ii[y0, x0] == 0

    if anchor == 'top':
        xs = sorted(range(margin, W - w - margin + 1, step), key=lambda x: abs(x - (W - w) / 2))
        ys = range(margin, H - h - margin + 1, step)
    else:
        xs = [margin if anchor == 'bl' else W - w - margin]
        ys = range(H - h - margin, margin - 1, -step)
    for y in ys:
        for x in xs:
            if clear(x, y):
                return x, y
    return None


def panel_sizes(W):
    """Candidate widths (largest first) and height per anchor, scaled to the monitor width."""
    top = min(1120, W - 2 * MARGIN)
    corner = min(440, (W - 3 * MARGIN) // 2)
    return {'top': ([top, int(top * 0.93), int(top * 0.86)], 290),
            'bl': ([corner, corner - 20, corner - 40, corner - 60], 250),
            'br': ([corner, corner - 20, corner - 40, corner - 60], 250)}


def layout(mask):
    """{'sysmon': (x, y, w, h), 'df': ..., 'sensors': ...} -- only panels that fit.
    A placed panel blocks the space for the next ones; the top panel stays in the upper
    half of the screen and the corner panels in the lower half."""
    H, W = mask.shape
    occ = mask.copy()
    out = {}
    sizes = panel_sizes(W)
    for name, anchor in (('sysmon', 'top'), ('df', 'bl'), ('sensors', 'br')):
        widths, h = sizes[anchor]
        for w in widths:
            if w <= 0:
                continue
            spot = free_spot(occ, w, h, anchor)
            if spot and ((anchor == 'top' and spot[1] + h <= H / 2) or (anchor != 'top' and spot[1] >= H / 2)):
                x, y = spot
                out[name] = (x, y, w, h)
                occ[y:y + h, x:x + w] = True
                break
    return out
