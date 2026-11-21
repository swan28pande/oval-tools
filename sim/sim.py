"""Crude local simulator for sanity testing (NOT the real game). Our bot vs N hunter bots.

Usage: python3 sim/sim.py [matches] [hunters]
"""
import gzip
import json
import math
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bot as botmod  # noqa: E402
from navmap import NavMap  # noqa: E402

botmod.mf.draw.caps = {}

with gzip.open(os.path.join(ROOT, "check", "stream.jsonl.gz"), "rt") as f:
    HELLO = json.loads(f.readline())
HELLO["map"]["dir"] = os.path.join(ROOT, "arena")
CFG = HELLO["config"]
MAP = NavMap(HELLO["map"]["dir"])


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


class Mech:
    def __init__(self, id, pos, skill):
        self.id = id
        self.pos = [pos[0], pos[1], MAP.floor_at(*pos)]
        self.vel = [0, 0, 0]
        self.aim = [random.uniform(-3, 3), 0]
        self.cmd_aim = list(self.aim)
        self.hp = 1400
        self.max_hp = 1400
        self.energy = 100
        self.alive = True
        self.skill = skill
        self.weapons = [{"kind": "gun", "cooldown": 0, "ammo": 12, "reload": 0, "stats": dict(CFG["weapons"]["gun"])}, None]
        self.last_dmg = -99
        self.events = []
        self.move = [0, 0]
        self.boost = False
        self.fire = [False, False]
        self.death_t = None
        self.laser_charge = 0
        self.jink = 1
        self.jink_until = 0
        self.jboost = False

    def give_laser(self):
        self.weapons[1] = {"kind": "laser", "cooldown": 0, "charge": 0, "stats": dict(CFG["weapons"]["laser"])}


ZONE_SEED = 1
LASER_P = float(os.environ.get('LASER_P', '0.5'))
ME_LASER = os.environ.get('ME_LASER') == '1'


def zone_at(t):
    c = CFG["zone"]["start_center"]
    r = CFG["zone"]["start_radius"]
    tt = 0
    centers = [(-23.7, 2.2)]
    zr_ = random.Random(ZONE_SEED)
    for ph in CFG["zone"]["phases"]:
        ncx = c[0] + (zr_.random() - 0.5) * max(0, r - ph["radius"]) * 0.8
        ncy = c[1] + (zr_.random() - 0.5) * max(0, r - ph["radius"]) * 0.8
        nc, nr = [ncx, ncy], ph["radius"]
        hs, he = tt + ph["hold_s"], tt + ph["hold_s"] + ph["shrink_s"]
        if t < he:
            return {"center": c, "radius": r, "next_center": nc, "next_radius": nr, "shrink_start": hs,
                    "shrink_end": he, "dps": ph["dps"]}
        c, r, tt = nc, nr, he
    return {"center": c, "radius": 0, "next_center": c, "next_radius": 0, "shrink_start": tt, "shrink_end": tt + 1, "dps": 260}


def run_match(seed, n_hunters, verbose=False):
    global ZONE_SEED
    ZONE_SEED = seed
    rnd = random.Random(seed)
    for k in STATS: STATS[k] = 0
    random.seed(seed)
    mechs = []
    def spawn():
        while True:
            x, y = rnd.uniform(-300, 400), rnd.uniform(-250, 300)
            if MAP.walkable(x, y):
                return (x, y)
    me = Mech(0, spawn(), 0)
    if ME_LASER:
        me.give_laser()
    mechs.append(me)
    tiers = ([0.0] * 4 + [0.35] * 4 + [0.65] * 4 + [0.9] * 3) if n_hunters == 15 else [rnd.uniform(0.3, 1.0) for _ in range(n_hunters)]
    for i in range(n_hunters):
        h = Mech(i + 1, spawn(), tiers[i])
        if rnd.random() < LASER_P * (0.4 + h.skill):
            h.give_laser()
        mechs.append(h)
    if os.environ.get("ME_LASER") == "rand" and rnd.random() < 0.5:
        me.give_laser()
    hello = json.loads(json.dumps(HELLO))
    hello["slot"] = 0
    b = botmod.Bot(hello)
    bullets = []
    dt = 0.05
    t = 0.0
    tick = 0
    worst_ms = 0
    worst_at = None
    tot_ms = 0
    n_act = 0
    random.seed(seed)
    active_hunters = 1
    while t < 480 and me.alive:
        zone = zone_at(t)
        zc, zr = b.zone_now(zone, t)
        if tick % 2 == 0:
            # our bot
            obs = make_obs(me, mechs, bullets, zone, t, tick)
            t0 = time.time()
            a = b.act(obs)
            ms = (time.time() - t0) * 1000
            if ms > worst_ms:
                worst_ms = ms
                worst_at = (round(t, 1), b.goal_desc)
            tot_ms += ms
            n_act += 1
            me.move = a.get("move", [0, 0])
            me.boost = a.get("boost", False)
            me.cmd_aim = a.get("aim", me.cmd_aim)
            me.fire = a.get("fire", [False, False])
            if a.get("reload") and me.weapons[0]["reload"] == 0 and me.weapons[0]["ammo"] < 12:
                me.weapons[0]["reload"] = 2.4
            me.events = []
            botmod.mf.draw.clear()
            alive_h = [m for m in mechs[1:] if m.alive]
            killed = n_hunters - len(alive_h)
            active_hunters = 1 + killed if t < 300 else 99
            for k, h in enumerate(alive_h):
                hunter_ai(h, me, k < active_hunters, t, rnd)
        for m in mechs:
            if not m.alive:
                continue
            step_mech(m, dt, t)
            fire_weapons(m, mechs, bullets, t, rnd)
            if math.hypot(m.pos[0] - zc[0], m.pos[1] - zc[1]) > zr:
                damage(m, zone["dps"] * dt, None, t, "zone")
            if t - m.last_dmg > 5 and m.hp < m.max_hp:
                m.hp = min(m.max_hp, m.hp + 45 * dt)
        step_bullets(bullets, mechs, dt, t)
        for m in mechs:
            if m.alive and m.hp <= 0:
                m.alive = False
                m.death_t = t
        t += dt
        tick += 1
    alive = sum(1 for m in mechs if m.alive)
    near_at_death = sum(1 for m in mechs[1:] if m.alive and math.hypot(m.pos[0]-me.pos[0], m.pos[1]-me.pos[1]) < 150)
    dead_after = sum(1 for m in mechs[1:] if not m.alive)
    place = 1 + sum(1 for m in mechs[1:] if m.alive or (m.death_t or 0) > (me.death_t or 1e9)) if not me.alive else 1
    return dict(seed=seed, survived_t=round(me.death_t or t, 1), kills=dead_after, place=place,
                worst_ms=round(worst_ms, 1), worst_at=worst_at, stats=dict(STATS), near=near_at_death, avg_ms=round(tot_ms / max(1, n_act), 2), hp=round(me.hp))


STATS = {'me_shots': 0, 'me_hits': 0, 'h_shots': 0, 'h_hits': 0}


def damage(m, amt, by, t, weapon, bearing=None):
    m.hp -= amt
    m.last_dmg = t
    if by is not None:
        by.last_dmg = t
    m.events.append({"type": "damaged", "amount": amt, "weapon": weapon, "bearing": bearing, "by": by.id if by else None})


def step_mech(m, dt, t):
    ws = 19.5 if (m.boost and m.energy > 0) else 11.7
    if m.weapons[1] and m.weapons[1]["kind"] == "laser" and m.weapons[1].get("charge", 0) > 0:
        ws *= 0.5
    mv = m.move
    n = math.hypot(*mv)
    if n > 1:
        mv = [mv[0] / n, mv[1] / n]
    tv = [mv[0] * ws, mv[1] * ws]
    for i in range(2):
        dv = tv[i] - m.vel[i]
        lim = 58.5 * dt
        m.vel[i] += max(-lim, min(lim, dv))
    if m.boost and n > 0.1:
        m.energy = max(0, m.energy - 17 * dt)
    else:
        m.energy = min(100, m.energy + 18 * dt)
    nx, ny = m.pos[0] + m.vel[0] * dt, m.pos[1] + m.vel[1] * dt
    if MAP.walkable(nx, ny):
        m.pos[0], m.pos[1] = nx, ny
    elif MAP.walkable(nx, m.pos[1]):
        m.pos[0] = nx
        m.vel[1] = 0
    elif MAP.walkable(m.pos[0], ny):
        m.pos[1] = ny
        m.vel[0] = 0
    else:
        m.vel[0] = m.vel[1] = 0
    m.pos[2] = MAP.floor_at(m.pos[0], m.pos[1])
    # aim slew
    dy = wrap(m.cmd_aim[0] - m.aim[0])
    m.aim[0] = wrap(m.aim[0] + max(-4 * dt, min(4 * dt, dy)))
    dp = max(-0.7, min(1.3, m.cmd_aim[1])) - m.aim[1]
    m.aim[1] += max(-3 * dt, min(3 * dt, dp))
    for w in m.weapons:
        if w:
            w["cooldown"] = max(0, w["cooldown"] - dt)
            if w["kind"] == "gun" and w["reload"] > 0:
                w["reload"] = max(0, w["reload"] - dt)
                if w["reload"] == 0:
                    w["ammo"] = 12


def aimdir(m):
    y, p = m.aim
    return (math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), math.sin(p))


def fire_weapons(m, mechs, bullets, t, rnd):
    g = m.weapons[0]
    if g and m.fire[0] and g["cooldown"] <= 0 and g["ammo"] > 0 and g["reload"] == 0:
        d = aimdir(m)
        sp = math.radians(0.6)
        yaw = m.aim[0] + rnd.gauss(0, sp)
        pit = m.aim[1] + rnd.gauss(0, sp)
        d = (math.cos(pit) * math.cos(yaw), math.cos(pit) * math.sin(yaw), math.sin(pit))
        o = [m.pos[0] + d[0] * 3, m.pos[1] + d[1] * 3, m.pos[2] + 7 + d[2] * 3]
        bullets.append({"id": rnd.randint(1, 10 ** 9), "kind": "gun", "pos": o, "vel": [d[0] * 252, d[1] * 252, d[2] * 252],
                        "owner": m, "dist": 0, "dmg": 45})
        g["cooldown"] = 0.24
        g["ammo"] -= 1
        STATS['me_shots' if m.id == 0 else 'h_shots'] += 1
        if g["ammo"] == 0:
            g["reload"] = 2.4
    L = m.weapons[1]
    if L and L["kind"] == "laser":
        if m.fire[1] and L["cooldown"] <= 0:
            L["charge"] = min(0.75, L["charge"] + 0.05)
            L["held"] = L.get("held", 0) + 0.05
            if L["held"] > 2.75:
                m.fire[1] = False
        if (not m.fire[1]) and L["charge"] > 0:
            frac = 0.35 + 0.65 * L["charge"] / 0.75
            d = aimdir(m)
            o = (m.pos[0], m.pos[1], m.pos[2] + 7)
            for v in mechs:
                if v is m or not v.alive:
                    continue
                c = (v.pos[0] - o[0], v.pos[1] - o[1], v.pos[2] + 4.5 - o[2])
                tt = c[0] * d[0] + c[1] * d[1] + c[2] * d[2]
                if tt < 0 or tt > 280:
                    continue
                cx, cy, cz = o[0] + d[0] * tt, o[1] + d[1] * tt, o[2] + d[2] * tt
                if math.hypot(cx - v.pos[0], cy - v.pos[1]) < 2.5 and v.pos[2] - 0.5 < cz < v.pos[2] + 9.5:
                    if MAP.los(o, (cx, cy, cz)):
                        dmg = 315 * frac
                        rear = abs(wrap(math.atan2(o[1] - v.pos[1], o[0] - v.pos[0]) - v.aim[0])) > math.pi / 2
                        dmg *= 1.5 if rear else 1
                        damage(v, dmg, m, t, "laser", math.atan2(o[1] - v.pos[1], o[0] - v.pos[0]))
                        m.events.append({"type": "hit", "target": v.id, "amount": dmg, "weapon": "laser"})
                    break
            L["charge"] = 0
            L["held"] = 0
            L["cooldown"] = 1.65


def step_bullets(bullets, mechs, dt, t):
    keep = []
    for b in bullets:
        p0 = list(b["pos"])
        hit = False
        for s in range(4):
            for i in range(3):
                b["pos"][i] += b["vel"][i] * dt / 4
            b["dist"] += 252 * dt / 4
            x, y, z = b["pos"]
            if z < MAP.top[MAP._ij(x, y)[1], MAP._ij(x, y)[0]]:
                hit = True
                break
            for v in mechs:
                if v is b["owner"] or not v.alive:
                    continue
                if math.hypot(x - v.pos[0], y - v.pos[1]) < 2.8 and v.pos[2] - 0.3 < z < v.pos[2] + 9.3:
                    br = math.atan2(-b["vel"][1], -b["vel"][0])
                    rear = abs(wrap(br - v.aim[0])) > math.pi / 2
                    dmg = b["dmg"] * (1.5 if rear else 1)
                    damage(v, dmg, b["owner"], t, "gun", br)
                    STATS['me_hits' if b["owner"].id == 0 else 'h_hits'] += 1
                    b["owner"].events.append({"type": "hit", "target": v.id, "amount": dmg, "weapon": "gun"})
                    hit = True
                    break
            if hit:
                break
        if not hit and b["dist"] < 230:
            keep.append(b)
    bullets[:] = keep


def hunter_ai(h, me, active, t, rnd):
    """Expert-like hunter calibrated on live recordings: full-speed jinks ~0.8 s, boosts sometimes,
    engages around 60 m, leads shots with the target's current velocity."""
    dx, dy = me.pos[0] - h.pos[0], me.pos[1] - h.pos[1]
    d = math.hypot(dx, dy)
    b = math.atan2(dy, dx)
    zone = zone_at(t)
    if not active or not me.alive:
        nc = zone["next_center"]
        h.move = [nc[0] - h.pos[0], nc[1] - h.pos[1]]
        n = math.hypot(*h.move) or 1
        h.move = [h.move[0] / n, h.move[1] / n] if n > 20 else [0, 0]
        h.fire = [False, False]
        h.boost = False
        h.cmd_aim = [math.atan2(h.move[1], h.move[0]) if n > 20 else h.aim[0], 0]
        return
    seen = d < 200 and MAP.los((h.pos[0], h.pos[1], h.pos[2] + 8), (me.pos[0], me.pos[1], me.pos[2] + 4.5))
    h.boost = False
    if not seen or d > 110:
        if not hasattr(h, "pth") or t - getattr(h, "pth_t", -9) > 2:
            h.pth = MAP.astar((h.pos[0], h.pos[1]), (me.pos[0], me.pos[1])) or []
            h.pth_t = t
        while len(h.pth) > 1 and math.hypot(h.pth[0][0] - h.pos[0], h.pth[0][1] - h.pos[1]) < 3:
            h.pth.pop(0)
        if h.pth:
            wx, wy = h.pth[0]
            mv = [wx - h.pos[0], wy - h.pos[1]]
            n = math.hypot(*mv) or 1
            h.move = [mv[0] / n, mv[1] / n]
    else:
        if t > getattr(h, "jink_until", 0):
            h.jink = -getattr(h, "jink", 1)
            h.jink_until = t + rnd.uniform(0.5, 1.1)
            h.jboost = rnd.random() < 0.3
        s = h.jink
        rad = 0.6 if d > 80 else (-0.6 if d < 40 else 0.0)
        mv = [-math.sin(b) * s + rad * math.cos(b), math.cos(b) * s + rad * math.sin(b)]
        nx_, ny_ = h.pos[0] + mv[0] * 5, h.pos[1] + mv[1] * 5
        if not MAP.walkable(nx_, ny_):
            h.jink = -h.jink
        h.move = mv
        h.boost = h.jboost and h.energy > 30
    if seen:
        tz = me.pos[2] + 4.5
        tt = 0.05
        for _ in range(3):
            px, py = me.pos[0] + me.vel[0] * tt, me.pos[1] + me.vel[1] * tt
            tt = 0.05 + math.hypot(px - h.pos[0], py - h.pos[1]) / 252
        err = (1 - h.skill) * 0.03 + 0.004
        h.cmd_aim = [math.atan2(py - h.pos[1], px - h.pos[0]) + rnd.gauss(0, err),
                     math.atan2(tz - h.pos[2] - 7, d)]
        on = abs(wrap(h.aim[0] - h.cmd_aim[0])) < math.atan2(2.2, max(d, 1))
        h.fire = [on, False]
        if h.weapons[1]:
            L = h.weapons[1]
            if L["charge"] < 0.75:
                h.fire[1] = L["cooldown"] <= 0
            else:
                h.fire[1] = not on
    else:
        h.cmd_aim = [b, 0]
        h.fire = [False, False]


def make_obs(me, mechs, bullets, zone, t, tick):
    vis = []
    for m in mechs:
        if m is me or not m.alive:
            continue
        d = math.hypot(m.pos[0] - me.pos[0], m.pos[1] - me.pos[1])
        b = math.atan2(m.pos[1] - me.pos[1], m.pos[0] - me.pos[0])
        los = MAP.los((me.pos[0], me.pos[1], me.pos[2] + 8), (m.pos[0], m.pos[1], m.pos[2] + 4.5))
        if los and (d < 45 or (d < 200 and abs(wrap(b - me.aim[0])) < math.radians(60))):
            vis.append({"id": m.id, "kind": "mech", "pos": [round(v, 2) for v in m.pos], "vel": [round(v, 2) for v in m.vel],
                        "aim": list(m.aim), "hp": m.hp, "max_hp": 1400, "cubes": 0,
                        "weapons": [w["kind"] for w in m.weapons if w],
                        "charging": bool(m.weapons[1] and m.weapons[1].get("charge", 0) > 0)})
    projs = [{"id": b["id"], "kind": "gun", "pos": b["pos"], "vel": b["vel"]} for b in bullets if b["owner"] is not me]
    zc, zr = (zone["center"], zone["radius"])
    selfo = {"id": 0, "pos": list(me.pos), "vel": list(me.vel), "aim": list(me.aim), "hp": me.hp, "max_hp": me.max_hp,
             "energy": me.energy, "max_energy": 100, "grounded": True, "cubes": 0, "upgrades": [],
             "weapons": [dict(w) if w else None for w in me.weapons],
             "in_zone": True, "stats": dict(CFG["mech"])}
    return {"type": "obs", "tick": tick, "t": round(t, 2), "alive": sum(1 for m in mechs if m.alive), "self": selfo,
            "mechs": vis, "projectiles": projs, "crates": [], "cubes": [], "pickups": [], "sounds": [], "beams": [],
            "events": me.events, "zone": zone, "offer": None, "drops": []}


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    nh = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    laser_me = len(sys.argv) > 3
    res = []
    for s in range(n):
        r = run_match(1000 + s, nh)
        print(r, flush=True)
        res.append(r)
    print("mean kills", sum(r["kills"] for r in res) / len(res), "mean survive", sum(r["survived_t"] for r in res) / len(res))
