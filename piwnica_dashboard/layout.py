"""Place dashboard panels in the free space around a character on the wallpaper.

The holdout mask (bool H x W, True = character) comes from a PNG whose alpha marks the
character. A panel that does not fit is skipped -- it never covers the character.
"""
import numpy as np

MARGIN = 20
# extra panels placed after sysmon / df / sensors, at a side, in this order (display.panels)
EXTRA = ('pacman', 'updates', 'proxmox', 'claude', 'nfs', 'luneta', 'now', 'forge', 'builds')
EXTRA_H = {'pacman': 230, 'updates': 230, 'claude': 110, 'proxmox': 230, 'nfs': 220, 'luneta': 210, 'now': 200, 'forge': 130, 'builds': 210}


def free_spot(mask, w, h, anchor, margin=MARGIN, step=8, rows=(), rows_only=False):
    """Top-left corner of a w x h rectangle that touches no masked pixel, or None.
    anchor: 'top' (from the top, centred), 'bl' / 'br' (bottom-left / bottom-right corner),
    'side' (highest spot at the left or right edge).
    rows: preferred y values (one margin under a placed panel), tried before the step grid;
    rows_only: only those (aligned with the panels above), no grid."""
    H, W = mask.shape
    if w + 2 * margin > W or h + 2 * margin > H:
        return None
    ii = np.pad(mask.astype(np.int32).cumsum(0).cumsum(1), ((1, 0), (1, 0)))

    def clear(x, y):
        x0, y0 = max(0, x - margin), max(0, y - margin)
        x1, y1 = min(W, x + w + margin), min(H, y + h + margin)
        return ii[y1, x1] - ii[y0, x1] - ii[y1, x0] + ii[y0, x0] == 0

    if anchor == 'side':  # the highest free spot at the left or right edge
        xs = [W - w - margin, margin]
        aligned = {y for y in rows if margin <= y <= H - h - margin}
        ys = sorted(aligned if rows_only else set(range(margin, H - h - margin + 1, step)) | aligned)
    elif anchor == 'top':  # exactly centred first, so its edges line up with the corner panels
        xs = sorted(set(range(margin, W - w - margin + 1, step)) | {(W - w) // 2}, key=lambda x: abs(x - (W - w) / 2))
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
    top = min(1160, W - 2 * MARGIN)  # on a 1200 px wide monitor: edge to edge, like the corner panels
    corner = min(440, (W - 3 * MARGIN) // 2)
    return {'top': ([top, int(top * 0.93), int(top * 0.86)], 290),
            'bl': ([corner, corner - 20, corner - 40, corner - 60], 250),
            'br': ([corner, corner - 20, corner - 40, corner - 60], 250),
            'side': ([w for w in (440, 400, 360, 320, 300) if w <= W - 2 * MARGIN], 230)}


def layout(mask, extra=EXTRA):
    """{'sysmon': (x, y, w, h), 'df': ..., 'sensors': ..., 'pacman': ...} -- only panels that fit.
    A placed panel blocks the space for the next ones; the top panel stays in the upper
    half of the screen and the corner panels in the lower half; the extra panels (pacman, proxmox, ...)
    go last, in the given order, wherever there is room left at a side."""
    H, W = mask.shape
    occ = mask.copy()
    out = {}
    sizes = panel_sizes(W)
    for name, anchor in (('sysmon', 'top'), ('df', 'bl'), ('sensors', 'br')) + tuple((e, 'side') for e in extra):
        widths, h = sizes[anchor]
        h = EXTRA_H.get(name, h)
        rows = [MARGIN] + [py + ph + MARGIN for _px, py, _pw, ph in out.values()]  # same gap as to the screen edge
        # side panels: first only spots lined up under a placed panel (any width), then the plain grid
        tries = [(w, True) for w in widths] + [(w, False) for w in widths] if anchor == 'side' else [(w, False) for w in widths]
        for w, aligned in tries:
            if w <= 0:
                continue
            spot = free_spot(occ, w, h, anchor, rows=rows, rows_only=aligned)
            if spot and (anchor == 'side' or (anchor == 'top' and spot[1] + h <= H / 2)
                         or (anchor in ('bl', 'br') and spot[1] >= H / 2)):
                x, y = spot
                out[name] = (x, y, w, h)
                occ[y:y + h, x:x + w] = True
                break
    return out
