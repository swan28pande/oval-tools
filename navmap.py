"""Static map model: floor heights, walkability grid, line-of-sight and A* pathfinding."""
from __future__ import annotations

import heapq
import json
import math
import os
import time
from array import array

import numpy as np

SQ2 = math.sqrt(2.0)


def _poly_mask(xs, ys, ring):
    """Even-odd point-in-polygon for grid point arrays xs (nx,), ys (ny,) -> bool (ny, nx)."""
    px = np.asarray(ring[0::2], dtype=np.float64)
    py = np.asarray(ring[1::2], dtype=np.float64)
    X = xs[None, :]
    Y = ys[:, None]
    inside = np.zeros((len(ys), len(xs)), dtype=bool)
    n = len(px)
    j = n - 1
    for i in range(n):
        xi, yi, xj, yj = px[i], py[i], px[j], py[j]
        if yi != yj:
            cond = (yi > Y) != (yj > Y)
            xint = (xj - xi) * (Y - yi) / (yj - yi) + xi
            inside ^= cond & (X < xint)
        j = i
    return inside


def _dilate(mask, r):
    """Binary dilation with a disc of radius r cells."""
    out = mask.copy()
    ri = int(math.ceil(r))
    H, W = mask.shape
    for dy in range(-ri, ri + 1):
        for dx in range(-ri, ri + 1):
            if dx == 0 and dy == 0 or dx * dx + dy * dy > r * r:
                continue
            ys0, ys1 = max(0, dy), H + min(0, dy)
            xs0, xs1 = max(0, dx), W + min(0, dx)
            out[ys0:ys1, xs0:xs1] |= mask[ys0 - dy:ys1 - dy, xs0 - dx:xs1 - dx]
    return out


class NavMap:
    def __init__(self, mapdir: str, mech_height: float = 9.0, step_up: float = 1.2, radius: float = 2.5):
        t0 = time.time()
        with open(os.path.join(mapdir, "v2", "world.json")) as f:
            w = json.load(f)
        tr = w["terrain"]
        self.minX, self.minY, self.step = tr["minX"], tr["minY"], tr["step"]
        self.nx, self.ny = tr["nx"], tr["ny"]
        raw = np.fromfile(os.path.join(mapdir, "v2", tr["file"]), dtype="<i2")
        self.terrain = (raw.reshape(self.ny, self.nx).astype(np.float32) * tr.get("scale", 0.01))
        b = w["bounds"]
        self.bminX, self.bmaxX, self.bminY, self.bmaxY = b["minX"], b["maxX"], b["minY"], b["maxY"]

        ny, nx = self.ny, self.nx
        xs = self.minX + np.arange(nx) * self.step
        ys = self.minY + np.arange(ny) * self.step
        floor = self.terrain.copy()
        wall = np.zeros((ny, nx), dtype=bool)
        top = self.terrain.copy()  # sight / shot blocking height (ground-attached solids)
        prisms = sorted(w["prisms"], key=lambda p: p["base"])
        self.prisms = prisms
        for p in prisms:
            ring = p["ring"]
            rx, ry = ring[0::2], ring[1::2]
            i0 = max(0, int((min(rx) - self.minX) / self.step) - 1)
            i1 = min(nx, int((max(rx) - self.minX) / self.step) + 2)
            j0 = max(0, int((min(ry) - self.minY) / self.step) - 1)
            j1 = min(ny, int((max(ry) - self.minY) / self.step) + 2)
            if i1 <= i0 or j1 <= j0:
                continue
            m = _poly_mask(xs[i0:i1], ys[j0:j1], ring)
            for hole in p.get("holes") or []:
                if len(hole) >= 6:
                    m &= ~_poly_mask(xs[i0:i1], ys[j0:j1], hole)
            if not m.any():
                continue
            fl = floor[j0:j1, i0:i1]
            base, tp = p["base"], p["top"]
            kind = p["kind"]
            # can we pass under it?
            under = base >= fl + mech_height + 0.2
            if kind == "bridge":
                walkon = (~under) & m
                fl[walkon] = np.maximum(fl[walkon], tp)
            else:
                low = (tp - fl) <= step_up
                stepon = m & ~under & low
                fl[stepon] = np.maximum(fl[stepon], tp)
                wl = m & ~under & ~low
                wall[j0:j1, i0:i1] |= wl
                # sight blocking only for solids that start near the ground
                tps = top[j0:j1, i0:i1]
                blk = m & (base <= self.terrain[j0:j1, i0:i1] + 3.0)
                tps[blk] = np.maximum(tps[blk], tp)
        self.floor = floor
        self.top = top
        self.top_flat = array("f", top.astype(np.float32).ravel().tobytes())
        self.floor_flat = array("f", floor.astype(np.float32).ravel().tobytes())
        # steepness of floor
        gy, gx = np.gradient(floor.astype(np.float64), self.step)
        slope = np.sqrt(gx * gx + gy * gy)
        steep = (slope > 1.1) & ~wall
        blocked1 = _dilate(wall, radius + 0.7) | _dilate(steep, 1.0)
        # border margin
        blocked1[:4, :] = True
        blocked1[-4:, :] = True
        blocked1[:, :4] = True
        blocked1[:, -4:] = True
        self.blocked1 = blocked1
        self.blk_flat = bytes(blocked1.astype(np.uint8).ravel().tobytes())
        # nav grid (2 m)
        self.cs = 2.0
        self.gx0 = self.minX
        self.gy0 = self.minY
        self.gnx = nx // 2
        self.gny = ny // 2
        sub = blocked1[1:self.gny * 2:2, 1:self.gnx * 2:2]
        self.gblocked = sub.copy()
        self.gfree = (~sub).ravel().tolist()
        self.load_s = time.time() - t0

    # ---------------------------------------------------------------- queries
    def _ij(self, x, y):
        i = int((x - self.minX) / self.step + 0.5)
        j = int((y - self.minY) / self.step + 0.5)
        return min(max(i, 0), self.nx - 1), min(max(j, 0), self.ny - 1)

    def ground_at(self, x, y):
        i, j = self._ij(x, y)
        return float(self.terrain[j, i])

    def walkable(self, x, y):
        i, j = self._ij(x, y)
        return not self.blk_flat[j * self.nx + i]

    def los(self, a, b, pad=0.0, step=1.5):
        """True if segment a->b (3D) is not blocked by buildings/terrain (pure Python, early exit)."""
        ax, ay, az = a[0], a[1], a[2]
        dx, dy, dz = b[0] - ax, b[1] - ay, b[2] - az
        d = math.hypot(dx, dy)
        n = int(d / step) + 1
        if n <= 1:
            return True
        top = self.top_flat
        nx, ny = self.nx, self.ny
        mx, my, inv = self.minX - 0.5 * self.step, self.minY - 0.5 * self.step, 1.0 / self.step
        sx, sy, sz = dx / n, dy / n, dz / n
        x, y, z = ax, ay, az
        for _ in range(n - 1):
            x += sx
            y += sy
            z += sz
            i = int((x - mx) * inv)
            j = int((y - my) * inv)
            if i < 0 or j < 0 or i >= nx or j >= ny:
                continue
            if z <= top[j * nx + i] + pad:
                return False
        return True

    def floor_at(self, x, y):
        i, j = self._ij(x, y)
        return float(self.floor_flat[j * self.nx + i])

    # ---------------------------------------------------------------- grid helpers
    def gcell(self, x, y):
        I = int((x - self.gx0 - 1.0) / self.cs + 0.5)
        J = int((y - self.gy0 - 1.0) / self.cs + 0.5)
        return min(max(I, 0), self.gnx - 1), min(max(J, 0), self.gny - 1)

    def gpos(self, I, J):
        return self.gx0 + 1.0 + I * self.cs, self.gy0 + 1.0 + J * self.cs

    def nearest_free(self, x, y, maxr=12):
        I, J = self.gcell(x, y)
        free = self.gfree
        W = self.gnx
        if free[J * W + I]:
            return I, J
        for r in range(1, maxr + 1):
            best = None
            for dJ in range(-r, r + 1):
                for dI in (-r, r) if abs(dJ) != r else range(-r, r + 1):
                    a, b = I + dI, J + dJ
                    if 0 <= a < W and 0 <= b < self.gny and free[b * W + a]:
                        d = dI * dI + dJ * dJ
                        if best is None or d < best[0]:
                            best = (d, a, b)
            if best:
                return best[1], best[2]
        return None

    def grid_line_free(self, I0, J0, I1, J1):
        """Straight line between grid cells passes only free cells (supercover-ish sampling)."""
        free = self.gfree
        W = self.gnx
        dI, dJ = I1 - I0, J1 - J0
        n = int(max(abs(dI), abs(dJ)) * 2) + 1
        for k in range(1, n + 1):
            t = k / n
            a = int(I0 + dI * t + 0.5)
            b = int(J0 + dJ * t + 0.5)
            if not free[b * W + a]:
                return False
        return True

    def astar(self, start, goal, max_exp=8000, weight=1.6, budget_s=0.008):
        """Path of world (x,y) waypoints from start to goal. Partial if the budget runs out."""
        s = self.nearest_free(*start)
        g = self.nearest_free(*goal, maxr=25)
        if s is None or g is None:
            return None
        W, Hh = self.gnx, self.gny
        N = W * Hh
        if not hasattr(self, "_gs"):
            self._gs = [0.0] * N
            self._st = [0] * N
            self._cl = [0] * N
            self._came = [0] * N
            self._stamp = 0
            nb = []
            for dI, dJ, c in ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                              (1, 1, SQ2), (1, -1, SQ2), (-1, 1, SQ2), (-1, -1, SQ2)):
                nb.append((dJ * W + dI, dI, dJ, c, dJ * W, dI))
            self._nb = nb
        self._stamp += 1
        stamp = self._stamp
        gs, st, cl, came = self._gs, self._st, self._cl, self._came
        free = self.gfree
        si = s[1] * W + s[0]
        gi = g[1] * W + g[0]
        gI, gJ = g
        if si == gi:
            return [goal]
        k2 = (SQ2 - 1)
        def h(idx):
            dI = abs(idx % W - gI)
            dJ = abs(idx // W - gJ)
            return (dI + k2 * dJ if dI > dJ else dJ + k2 * dI) * weight
        gs[si] = 0.0
        st[si] = stamp
        came[si] = -1
        openh = [(h(si), si)]
        best, besth = si, h(si)
        nbrs = self._nb
        exp = 0
        found = False
        deadline = time.perf_counter() + budget_s
        push, pop = heapq.heappush, heapq.heappop
        while openh:
            f, cur = pop(openh)
            if cl[cur] == stamp:
                continue
            if cur == gi:
                found = True
                break
            cl[cur] = stamp
            exp += 1
            if exp > max_exp or (exp & 127 == 0 and time.perf_counter() > deadline):
                break
            cI = cur % W
            cJ = cur - cI
            if cI == 0 or cI == W - 1 or cur < W or cur >= N - W:
                continue
            gc = gs[cur]
            for off, dI, dJ, c, offJ, offI in nbrs:
                ni = cur + off
                if not free[ni] or cl[ni] == stamp:
                    continue
                if dI and dJ and (not free[cur + offI] or not free[cur + offJ]):
                    continue
                ng = gc + c
                if st[ni] != stamp or ng < gs[ni]:
                    gs[ni] = ng
                    st[ni] = stamp
                    came[ni] = cur
                    hn = h(ni)
                    if hn < besth:
                        besth, best = hn, ni
                    push(openh, (ng + hn, ni))
        end = gi if found else best
        cells = []
        c = end
        while c != -1:
            cells.append((c % W, c // W))
            c = came[c]
        cells.reverse()
        # string pulling
        pts = [cells[0]]
        k = 0
        while k < len(cells) - 1:
            j = len(cells) - 1
            lim = min(len(cells) - 1, k + 40)
            j = lim
            while j > k + 1 and not self.grid_line_free(cells[k][0], cells[k][1], cells[j][0], cells[j][1]):
                j -= 1
            pts.append(cells[j])
            k = j
        out = [self.gpos(I, J) for I, J in pts[1:]]
        if found:
            out.append((goal[0], goal[1]) if self.walkable(goal[0], goal[1]) else out[-1] if out else self.gpos(*g))
        return out or [self.gpos(end % W, end // W)]
