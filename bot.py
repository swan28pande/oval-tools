"""Survivor bot for mechfight/1.

Priorities each decision: stay in the zone, fight whatever is hunting us with led shots while
strafing and facing it, retreat to cover to regenerate when losing, and otherwise farm weapons,
cubes, crates and NPC drones to scale up.
"""
from __future__ import annotations

import gc
import math
import random
import sys
import time

import mechfight as mf

try:
    from navmap import NavMap
except Exception:  # pragma: no cover
    NavMap = None

TAU = 2 * math.pi
WEAPON_SCORE = {"laser": 3.0, "gun": 2.6, "lightning": 2.4, "mortar": 1.0}
UPGRADE_PRIO = ["armor", "plating", "laser_damage", "laser_charge", "nanites", "gun_damage", "gun_rate",
                "lightning_efficiency", "gyros", "lightning_range", "gun_mag", "servos", "sensors",
                "gun_velocity", "laser_pierce", "lightning_chain", "capacitor", "reactor", "jets",
                "mortar_reload", "mortar_velocity", "mortar_splash"]


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Track:
    __slots__ = ("id", "pos", "vel", "aim", "hp", "max_hp", "cubes", "weapons", "charging", "seen_t",
                 "charge_since", "kind", "visible", "hist")

    def __init__(self, m, t):
        self.id = m["id"]
        self.charge_since = None
        self.hist = []
        self.update(m, t)

    def update(self, m, t):
        self.kind = m.get("kind", "mech")
        self.pos = m["pos"]
        self.vel = m.get("vel", [0, 0, 0])
        self.aim = m.get("aim", [0, 0])
        self.hp = m.get("hp", 1400)
        self.max_hp = m.get("max_hp", 1400)
        self.cubes = m.get("cubes", 0) or 0
        self.weapons = m.get("weapons", ["gun"]) or []
        ch = bool(m.get("charging"))
        if ch and not getattr(self, "charging", False):
            self.charge_since = t
        if not ch:
            self.charge_since = None
        self.charging = ch
        self.seen_t = t
        self.hist.append((t, self.pos[0], self.pos[1]))
        while len(self.hist) > 2 and t - self.hist[0][0] > 1.2:
            self.hist.pop(0)

    def avg_vel(self):
        if len(self.hist) < 3:
            return self.vel
        t0, x0, y0 = self.hist[0]
        t1, x1, y1 = self.hist[-1]
        if t1 - t0 < 0.3:
            return self.vel
        return [(x1 - x0) / (t1 - t0), (y1 - y0) / (t1 - t0), self.vel[2]]

    def pred_vel(self, flight):
        """Blend instantaneous and average velocity: jinking targets revert toward their mean."""
        a = self.avg_vel()
        k = max(0.0, min(1.0, 1.0 - flight / 0.8))
        return [self.vel[0] * k + a[0] * (1 - k), self.vel[1] * k + a[1] * (1 - k), self.vel[2]]


class Bot:
    name = "oval-survivor"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.slot = hello.get("slot", 0)
        random.seed(hello.get("seed", 0))
        self.map = None
        try:
            mapdir = hello["map"]["dir"]
            mc = self.cfg["mech"]
            self.map = NavMap(mapdir, mc.get("height", 9), mc.get("step_up", 1.2), mc.get("radius", 2.5))
            self.map.astar((0.0, 0.0), (10.0, 10.0))  # allocate search arrays now
            log(f"map loaded in {self.map.load_s:.2f}s")
        except Exception as e:
            log("map load failed", repr(e))
        self.w = self.cfg["weapons"]
        self.tracks = {}
        self.npcs = {}
        self.path = None
        self.path_goal = None
        self.path_t = -99
        self.strafe_dir = 1
        self.strafe_until = 0
        self.last_pos = None
        self.stuck_hist = []
        self.stuck_until = 0
        self.last_damage_t = -99
        self.last_damage_bearing = None
        self.last_hit_t = -99
        self.attackers = {}  # id -> last time it damaged us
        self.sound_dir = None
        self.sound_t = -99
        self.mode = "roam"
        self.retreat_until = 0
        self.cover = None
        self.cover_t = -99
        self.bad_targets = {}  # crate/pickup id -> time to ignore until
        self.scan_yaw = 0.0
        self.last_t = 0
        self.prev_hp = None
        self.laser_hold_t = None
        self.goal_desc = ""
        self.explore = None
        self.explore_until = 0
        self.last_threat_pos = None
        self.last_threat_t = -99
        gc.collect()
        gc.freeze()
        gc.set_threshold(50000, 50, 100)

    # ------------------------------------------------------------------ helpers
    def los(self, a, b):
        if self.map is None:
            return True
        try:
            return self.map.los(a, b)
        except Exception:
            return True

    def weapon(self, me, kind):
        for i, wpn in enumerate(me["weapons"]):
            if wpn and wpn["kind"] == kind:
                return i, wpn
        return None, None

    def zone_now(self, z, t):
        c, r = z["center"], z["radius"]
        ss, se = z["shrink_start"], z["shrink_end"]
        if t >= ss and se > ss:
            f = min(1.0, (t - ss) / (se - ss))
            c = [c[0] + (z["next_center"][0] - c[0]) * f, c[1] + (z["next_center"][1] - c[1]) * f]
            r = r + (z["next_radius"] - r) * f
        return c, r

    def path_to(self, me, goal, t, force=False):
        """Return a steering direction (unit) toward goal using A* waypoints."""
        p = me["pos"]
        if self.map is None:
            return self.unit(goal[0] - p[0], goal[1] - p[1])
        need = force or self.path is None or self.path_goal is None or d2(self.path_goal, goal) > 6 or t - self.path_t > 2.0
        if need:
            try:
                path = self.map.astar((p[0], p[1]), (goal[0], goal[1]))
            except Exception as e:
                log("astar", repr(e))
                path = None
            self.path = path
            self.path_goal = (goal[0], goal[1])
            self.path_t = t
        if not self.path:
            return self.unit(goal[0] - p[0], goal[1] - p[1])
        while len(self.path) > 1 and d2(self.path[0], p) < 3.0:
            self.path.pop(0)
        wp = self.path[0]
        if d2(wp, p) < 1.0:
            return (0.0, 0.0)
        return self.unit(wp[0] - p[0], wp[1] - p[1])

    @staticmethod
    def unit(x, y):
        n = math.hypot(x, y)
        if n < 1e-6:
            return (0.0, 0.0)
        return (x / n, y / n)

    def aim_solution(self, me, tgt_pos, tgt_vel, speed, height):
        """Yaw/pitch to hit a target with a straight projectile of `speed` (None = hitscan)."""
        p = me["pos"]
        st = me["stats"]
        mz = p[2] + st.get("muzzle_height", 7)
        tz_off = height * 0.5
        lat = 0.07
        tx, ty, tz = tgt_pos[0], tgt_pos[1], tgt_pos[2] + tz_off
        vx, vy, vz = tgt_vel
        t = lat
        for _ in range(3):
            px, py, pz = tx + vx * t, ty + vy * t, tz + vz * t
            if speed:
                d = math.sqrt((px - p[0]) ** 2 + (py - p[1]) ** 2 + (pz - mz) ** 2)
                t = lat + d / speed
        px, py, pz = tx + vx * t, ty + vy * t, tz + vz * t
        if self.map is not None and vz != 0:
            fl = self.map.floor_at(px, py) + tz_off
            pz = max(pz, fl)
        dh = math.hypot(px - p[0], py - p[1])
        yaw = math.atan2(py - p[1], px - p[0])
        pitch = math.atan2(pz - mz, max(dh, 0.1))
        return yaw, pitch, (px, py, pz), dh

    def aim_err(self, me, yaw, pitch):
        ay, ap = me["aim"]
        return math.hypot(wrap(ay - yaw) * math.cos(pitch), ap - pitch)

    def mortar_solution(self, me, tpos, tvel):
        p = me["pos"]
        mw = self.w["mortar"]
        v = mw["speed"]
        g = mw["gravity"]
        oz = p[2] + me["stats"].get("height", 9) + 0.5
        t_f = 1.5
        for _ in range(3):
            px, py = tpos[0] + tvel[0] * t_f, tpos[1] + tvel[1] * t_f
            d = math.hypot(px - p[0], py - p[1])
            dz = tpos[2] + 1.0 - oz
            disc = v ** 4 - g * (g * d * d + 2 * dz * v * v)
            if disc < 0 or d < 1:
                return None
            th = math.atan((v * v - math.sqrt(disc)) / (g * d))
            t_f = d / (v * math.cos(th)) + 0.07
        return math.atan2(py - p[1], px - p[0]), th, d

    # ------------------------------------------------------------------ main
    def act(self, obs):
        t0 = time.time()
        t = obs["t"]
        me = obs["self"]
        p = me["pos"]
        st = me["stats"]
        act = {"tick": obs["tick"]}
        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        height = st.get("height", 9)

        # ---------------- bookkeeping
        for e in obs["events"]:
            ty = e["type"]
            if ty == "damaged" and e.get("by") is not None:
                self.last_damage_t = t
                self.last_damage_bearing = e.get("bearing")
                self.attackers[e["by"]] = t
            elif ty == "damaged":
                self.last_damage_t = t if e.get("weapon") != "zone" else self.last_damage_t
            elif ty == "hit":
                self.last_hit_t = t
            elif ty == "take_failed":
                self.bad_targets[e["id"]] = t + 6
        seen_now = set()
        for m in obs["mechs"]:
            if m["kind"] == "mech":
                seen_now.add(m["id"])
                tr = self.tracks.get(m["id"])
                if tr:
                    tr.update(m, t)
                else:
                    self.tracks[m["id"]] = Track(m, t)
            else:
                tr = self.npcs.get(m["id"])
                if tr:
                    tr.update(m, t)
                else:
                    self.npcs[m["id"]] = Track(m, t)
        for k in [k for k, v in self.tracks.items() if t - v.seen_t > 6]:
            del self.tracks[k]
        for k in [k for k, v in self.npcs.items() if t - v.seen_t > 3]:
            del self.npcs[k]
        for s in obs["sounds"]:
            if s["kind"] in ("gun", "laser", "lightning", "laser_charge", "mortar", "step", "boost", "jet") and s["dist"] != "far":
                self.sound_dir = s["bearing"]
                self.sound_t = t

        # ---------------- upgrades
        offer = obs.get("offer")
        if offer:
            best, bi = 999, 0
            for i, c in enumerate(offer["choices"]):
                r = UPGRADE_PRIO.index(c["id"]) if c["id"] in UPGRADE_PRIO else 50
                if r < best:
                    best, bi = r, i
            act["pick"] = bi

        # ---------------- zone
        zc, zr = self.zone_now(obs["zone"], t)
        z = obs["zone"]
        nc, nr = z["next_center"], z["next_radius"]
        dist_c = d2(p, zc)
        dist_n = d2(p, nc)
        speed = st.get("walk_speed", 11.7)
        time_to_shrink = z["shrink_start"] - t
        # target: be inside the next circle in time, always inside current circle
        zone_goal = None
        zone_urgent = False
        margin_n = max(4.0, min(25.0, nr * 0.25))
        if dist_c > zr - 6:
            zone_goal = zc if nr <= 0 else nc
            zone_urgent = True
        elif dist_n > nr - margin_n and (time_to_shrink < (dist_n - nr + margin_n) / speed + 25 or t > z["shrink_start"]):
            zone_goal = nc
            zone_urgent = dist_c > zr - 25 or (t > z["shrink_start"])
        if nr <= 1 and t > z["shrink_start"] - 20:
            zone_goal = nc
            zone_urgent = dist_n > 3

        def inzone_pt(q, slack=8.0):
            # point is safe enough to visit
            if d2(q, zc) > zr - slack:
                return False
            if t > z["shrink_start"] - 15 and d2(q, nc) > nr - 2:
                return False
            return True

        # ---------------- threats
        enemies = []
        for tr in self.tracks.values():
            age = t - tr.seen_t
            if age > 4.5:
                continue
            ppos = [tr.pos[0] + tr.vel[0] * age, tr.pos[1] + tr.vel[1] * age, tr.pos[2]]
            enemies.append((d2(ppos, p), tr, age))
        enemies.sort(key=lambda e: e[0])
        if enemies:
            self.last_threat_pos = enemies[0][1].pos
            self.last_threat_t = t
        visible = [e for e in enemies if e[2] < 0.01]
        near_threats = [e for e in enemies if e[0] < 230]

        # ---------------- weapon management (take pickups)
        held = [w_["kind"] if w_ else None for w_ in me["weapons"]]
        reach = self.cfg["loot"]["pickup_radius"]
        want_pick = None
        for pk in obs["pickups"]:
            if self.bad_targets.get(pk["id"], -1) > t:
                continue
            k = pk["kind"]
            if k in held:
                continue
            sc = WEAPON_SCORE.get(k, 0)
            # what would it replace
            if None in held:
                gain = sc
                drop = None
            else:
                worst = min(range(len(held)), key=lambda i: WEAPON_SCORE.get(held[i], 0))
                if WEAPON_SCORE.get(held[worst], 0) >= sc:
                    continue
                # cannot swap a charging laser
                ww = me["weapons"][worst]
                if ww and ww.get("charge", 0) > 0:
                    continue
                gain = sc - WEAPON_SCORE.get(held[worst], 0)
                drop = worst
            dh = d2(pk["pos"], p)
            dz = abs(pk["pos"][2] - p[2])
            if dh < reach - 0.3 and dz < 3.8:
                act["take"] = pk["id"]
                if drop is not None:
                    act["drop"] = drop
                continue
            if self.map and abs(pk["pos"][2] - self.map.floor_at(pk["pos"][0], pk["pos"][1])) > 2.5:
                continue  # on a roof
            if self.map and not self.map.walkable(pk["pos"][0], pk["pos"][1]):
                # maybe just next to a wall: still try if close
                if dh > 30:
                    continue
            if not inzone_pt(pk["pos"]):
                continue
            score = gain * 90 - dh * (0.6 if t < 150 else 1.0)
            if want_pick is None or score > want_pick[0]:
                want_pick = (score, pk)

        # ---------------- choose combat target
        target = None
        if near_threats:
            def tscore(e):
                d, tr, age = e
                s = d + tr.hp / 8.0 + age * 40
                if tr.id in self.attackers and t - self.attackers[tr.id] < 4:
                    s -= 60
                return s
            target = min(near_threats, key=tscore)

        npc_target = None
        if not target:
            best = None
            for n in self.npcs.values():
                if t - n.seen_t > 0.01:
                    continue
                d = d2(n.pos, p)
                if d < 160 and inzone_pt(n.pos, 0):
                    if best is None or d < best[0]:
                        best = (d, n)
            if best and (hp_frac > 0.7 or (best[1].id in self.attackers and t - self.attackers[best[1].id] < 3)):
                npc_target = best[1]

        # ---------------- decide mode
        n_close = sum(1 for e in enemies if e[0] < 150)
        losing = False
        if target:
            my_mult = 1 + 0.04 * min(me.get("cubes", 0) or 0, 10)
            d0 = target[0]
            our_dps = 0.0
            for k in held:
                our_dps += {"gun": 50, "laser": 110, "mortar": 35}.get(k, 0)
                if k == "lightning" and d0 < 50:
                    our_dps += 160
            our_dps = max(30.0, our_dps * my_mult)
            their_dps = 0.0
            their_hp = 0.0
            for d, tr, age in enemies:
                if d > 200:
                    continue
                m = 1 + 0.04 * min(tr.cubes, 10)
                dps = 0.0
                for k in tr.weapons:
                    dps += {"gun": 50, "laser": 110, "mortar": 35}.get(k, 0)
                    if k == "lightning" and d < 50:
                        dps += 160
                their_dps += dps * m * (1.0 if d < 150 else 0.6)
                their_hp += tr.hp
            their_dps = max(30.0, their_dps)
            t_me_die = me["hp"] / their_dps
            t_them_die = target[1].hp / our_dps + (their_hp - target[1].hp) / our_dps * 0.8
            losing = (t_me_die < t_them_die * 0.85 and hp_frac < 0.7) or (hp_frac < 0.3 and t_me_die < t_them_die * 1.3)
            if target[1].hp < 160 and hp_frac > 0.15:
                losing = False  # finish it
        if losing:
            self.retreat_until = max(self.retreat_until, t + 2.5)
        # keep hiding until healed while nobody is right on top of us
        if self.mode == "retreat" and hp_frac < 0.8 and not (target and target[0] < 25):
            self.retreat_until = max(self.retreat_until, t + 0.2)
        retreating = t < self.retreat_until and not zone_urgent and target is not None
        self.mode = "retreat" if retreating else "normal"

        # ---------------- movement
        move = (0.0, 0.0)
        boost = False
        jump = False
        goal_desc = ""
        nav_goal = None

        if zone_urgent and zone_goal is not None:
            nav_goal = zone_goal
            goal_desc = "zone!"
            boost = me["energy"] > 25 and not me["in_zone"] or d2(p, zc) > zr - 2
        elif retreating:
            if t - self.cover_t > 0.6 or self.cover is None:
                self.cover = self.find_cover(me, [e[1] for e in enemies if e[0] < 250], inzone_pt)
                self.cover_t = t
            if self.cover:
                nav_goal = self.cover
                goal_desc = "cover"
                boost = me["energy"] > 20 and d2(self.cover, p) > 4
            elif target is None:
                goal_desc = "heal"
                nav_goal = zone_goal
            else:
                retreating = False
                self.mode = "normal"
        if target and not retreating and not (zone_urgent and zone_goal is not None):
            tr = target[1]
            d = target[0]
            has_light = "lightning" in held
            has_laser = "laser" in held
            if has_light and not has_laser:
                pref = 28.0
            elif has_light:
                pref = 40.0
            elif "laser" in tr.weapons and not has_laser:
                pref = 50.0
            else:
                pref = 85.0
            if tr.hp < me["hp"] * 0.5:
                pref *= 0.75
            # chase with path if no LOS / far
            tpos = tr.pos
            myeye = (p[0], p[1], p[2] + st.get("eye_height", 8))
            tcen = (tpos[0], tpos[1], tpos[2] + 4.5)
            has_los = self.los(myeye, tcen)
            if not has_los or d > 200:
                if hp_frac > 0.55 or d < 60:
                    nav_goal = (tpos[0], tpos[1])
                    goal_desc = "hunt"
                else:
                    nav_goal = zone_goal if zone_goal else None
                    goal_desc = "wait"
            else:
                # strafe + range keeping
                b = math.atan2(tpos[1] - p[1], tpos[0] - p[0])
                if t > self.strafe_until:
                    self.strafe_dir = -self.strafe_dir if random.random() < 0.7 else self.strafe_dir
                    self.strafe_until = t + random.uniform(0.5, 1.4)
                if tr.charging and t - (tr.charge_since or t) > 0.35:
                    # laser incoming: jink
                    if random.random() < 0.5:
                        self.strafe_dir = -self.strafe_dir
                    boost = me["energy"] > 15
                perp = (-math.sin(b) * self.strafe_dir, math.cos(b) * self.strafe_dir)
                radial = 0.0
                if d > pref * 1.25:
                    radial = 0.8
                elif d < pref * 0.7:
                    radial = -0.8
                mv = (perp[0] + math.cos(b) * radial, perp[1] + math.sin(b) * radial)
                move = self.unit(*mv)
                # avoid walking out of zone or into walls
                ahead = (p[0] + move[0] * 6, p[1] + move[1] * 6)
                if (self.map and not self.map.walkable(*ahead)) or not inzone_pt(ahead, 4):
                    self.strafe_dir = -self.strafe_dir
                    self.strafe_until = t + random.uniform(0.6, 1.2)
                    perp = (-perp[0], -perp[1])
                    move = self.unit(perp[0] + math.cos(b) * radial, perp[1] + math.sin(b) * radial)
                    ahead = (p[0] + move[0] * 6, p[1] + move[1] * 6)
                    if (self.map and not self.map.walkable(*ahead)) or not inzone_pt(ahead, 4):
                        mv2 = self.path_to(me, nc, t)
                        move = mv2
                goal_desc = f"fight {tr.id}"
        elif not target and not retreating and not (zone_urgent and zone_goal is not None) and \
                hp_frac < 0.75 and t - self.last_damage_t < 25 and t - self.last_damage_t > 0.0 and \
                (zone_goal is None or d2(p, zone_goal) < 40) and dist_c < zr - 15:
            goal_desc = "heal"  # stand still (silent) and regenerate
            nav_goal = None
        elif not target and not retreating and not (zone_urgent and zone_goal is not None):
            # roam: loot / cubes / crates / drops / zone
            cands = []
            for c in obs["cubes"]:
                if self.bad_targets.get(c["id"], -1) > t:
                    continue
                if self.map and abs(c["pos"][2] - self.map.floor_at(c["pos"][0], c["pos"][1])) > 2.5:
                    continue
                if not inzone_pt(c["pos"]):
                    continue
                cands.append((d2(c["pos"], p) - 40, (c["pos"][0], c["pos"][1]), "cube"))
            if hp_frac > 0.85:
                for c in obs["crates"]:
                    if self.map and abs(c["pos"][2] - self.map.floor_at(c["pos"][0], c["pos"][1])) > 2.5:
                        continue
                    if not inzone_pt(c["pos"]):
                        continue
                    cands.append((d2(c["pos"], p) - 15, (c["pos"][0], c["pos"][1]), "crate"))
            if want_pick:
                pk = want_pick[1]
                cands.append((-want_pick[0] + 10, (pk["pos"][0], pk["pos"][1]), "loot"))
            for dr in obs.get("drops", []):
                dd = d2(dr["pos"], p)
                if dd < 220 and dr["lands_in"] < dd / speed + 6 and inzone_pt(dr["pos"], 4) and not near_threats:
                    if dr.get("holder") is not None and dr.get("holder") != me["id"]:
                        continue
                    cands.append((dd - 70, (dr["pos"][0], dr["pos"][1]), "drop"))
            if cands:
                cands.sort(key=lambda c: c[0])
                nav_goal = cands[0][1]
                goal_desc = cands[0][2]
            elif zone_goal is not None:
                nav_goal = zone_goal
                goal_desc = "zone"
            else:
                # explore inside the next circle to find loot, crates and drones
                if self.explore is None or d2(self.explore, p) < 6 or t > self.explore_until:
                    self.explore = self.explore_point(me, nc, nr, zc, zr)
                    self.explore_until = t + 25
                if self.explore is not None and t < z["shrink_start"] + 300:
                    nav_goal = self.explore
                    goal_desc = "explore"
                elif dist_n > nr * 0.5:
                    nav_goal = nc
                    goal_desc = "center"
                else:
                    goal_desc = "hold"

        if nav_goal is not None and move == (0.0, 0.0):
            move = self.path_to(me, nav_goal, t)
            if goal_desc in ("hold",) or d2(nav_goal, p) < 1.5:
                move = (0.0, 0.0)

        # ---------------- projectile dodge
        dodge = self.dodge(me, obs["projectiles"])
        if dodge is not None:
            move = self.unit(move[0] * 0.5 + dodge[0], move[1] * 0.5 + dodge[1])

        # ---------------- stuck detection
        moving_cmd = math.hypot(*move) > 0.5
        self.stuck_hist.append((t, p[0], p[1], moving_cmd))
        self.stuck_hist = [h for h in self.stuck_hist if t - h[0] <= 1.5]
        if len(self.stuck_hist) > 10 and all(h[3] for h in self.stuck_hist):
            h0 = self.stuck_hist[0]
            if math.hypot(p[0] - h0[1], p[1] - h0[2]) < 2.0 and t > self.stuck_until + 1.0:
                self.stuck_until = t + 0.8
                self.path = None
                self.strafe_dir = -self.strafe_dir
        if t < self.stuck_until:
            jump = me["grounded"]
            boost = me["energy"] > 10
            if self.path:
                pass

        # ---------------- aim and fire
        fire = [False, False]
        aim_yaw, aim_pitch = me["aim"][0], 0.0
        aimed = False
        tgt_track = target[1] if target else npc_target
        if tgt_track is not None:
            tr = tgt_track
            age = t - tr.seen_t
            tpos = [tr.pos[0] + tr.vel[0] * age, tr.pos[1] + tr.vel[1] * age, tr.pos[2]]
            th = 9.0 * (1 + 0.05 * tr.cubes) if tr.kind == "mech" else 3.5
            fire, aim_yaw, aim_pitch = self.weapons_fire(me, tr, tpos, th, held, obs, act)
            aimed = True
        else:
            # crates if safe
            ctarget = None
            if not near_threats and hp_frac > 0.85:
                for c in obs["crates"]:
                    dd = d2(c["pos"], p)
                    if dd > 120:
                        continue
                    if self.map and abs(c["pos"][2] - self.map.floor_at(c["pos"][0], c["pos"][1])) > 2.5:
                        continue
                    if not inzone_pt(c["pos"], 0):
                        continue
                    if ctarget is None or dd < ctarget[0]:
                        ctarget = (dd, c)
            if ctarget:
                c = ctarget[1]
                yaw, pitch, _, dh = self.aim_solution(me, c["pos"], (0, 0, 0), self.w["gun"]["speed"], 2.4)
                aim_yaw, aim_pitch = yaw, pitch
                aimed = True
                gi, gw = self.weapon(me, "gun")
                mye = (p[0], p[1], p[2] + st.get("muzzle_height", 7))
                if gi is not None and self.aim_err(me, yaw, pitch) < max(0.006, math.atan2(1.0, dh)) and \
                        self.los(mye, (c["pos"][0], c["pos"][1], c["pos"][2] + 1.2)):
                    if gw.get("ammo", 1) > 0 and gw.get("reload", 0) == 0:
                        fire[gi] = True
                li, lw = self.weapon(me, "lightning")
                if li is not None and dh < self.w["lightning"]["range"] * 0.9 and me["energy"] > 40 and \
                        abs(wrap(me["aim"][0] - yaw)) < 0.2:
                    fire[li] = True
                # walk toward crate a bit so the drop is collected afterwards
            else:
                # look toward threats we heard/were hit by, else along movement
                look = None
                if t - self.last_damage_t < 3 and self.last_damage_bearing is not None:
                    look = self.last_damage_bearing
                elif t - self.sound_t < 2 and self.sound_dir is not None:
                    look = self.sound_dir
                elif enemies:
                    tp = enemies[0][1].pos
                    look = math.atan2(tp[1] - p[1], tp[0] - p[0])
                elif self.last_threat_pos is not None and t - self.last_threat_t < 12:
                    lp = self.last_threat_pos
                    look = math.atan2(lp[1] - p[1], lp[0] - p[0]) + 0.5 * math.sin(t * 2.0)
                elif math.hypot(*move) > 0.3:
                    # cycle through three 120-degree sectors so nothing sneaks up behind us
                    phase = int(t / 0.8) % 3
                    look = math.atan2(move[1], move[0]) + (0.0, 2.1, -2.1)[phase]
                else:
                    self.scan_yaw = wrap(self.scan_yaw + 0.25)
                    look = self.scan_yaw
                aim_yaw, aim_pitch = look, 0.0
                # reload when idle
                gi, gw = self.weapon(me, "gun")
                if gi is not None and gw.get("ammo", 99) < gw["stats"].get("mag", 12) * 0.6 and gw.get("reload", 0) == 0:
                    act["reload"] = True
            # release a charging laser if we lost the target
            li, lw = self.weapon(me, "laser")
            if li is not None and lw.get("charge", 0) > 0:
                fire[li] = False

        # never walk backwards into the zone wall while fighting: done above
        act["move"] = [round(move[0], 3), round(move[1], 3)]
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)
        act["aim"] = [round(aim_yaw, 4), round(max(-0.69, min(1.29, aim_pitch)), 4)]
        act["fire"] = fire
        self.goal_desc = goal_desc
        mf.draw.label(f"{goal_desc} hp{me['hp']:.0f} e{me['energy']:.0f} {int((time.time()-t0)*1000)}ms")
        return act

    # ------------------------------------------------------------------ weapons
    def weapons_fire(self, me, tr, tpos, th, held, obs, act):
        p = me["pos"]
        st = me["stats"]
        fire = [False, False]
        mz = (p[0], p[1], p[2] + st.get("muzzle_height", 7))
        tc = (tpos[0], tpos[1], tpos[2] + th * 0.5)
        d = math.hypot(tc[0] - p[0], tc[1] - p[1])
        los = self.los(mz, tc) or self.los(mz, (tpos[0], tpos[1], tpos[2] + th * 0.85))
        rad = (2.5 if tr.kind == "mech" else 1.8) * (1 + 0.05 * tr.cubes)
        # default aim: gun lead
        gi, gw = self.weapon(me, "gun")
        li, lw = self.weapon(me, "laser")
        ei, ew = self.weapon(me, "lightning")
        mi, mw = self.weapon(me, "mortar")
        gspd = (gw["stats"]["speed"] if gw else self.w["gun"]["speed"])
        flight = d / gspd
        yaw, pitch, aimpt, dh = self.aim_solution(me, tpos, tr.pred_vel(flight), gspd, th)
        aim = (yaw, pitch)
        laser_active = li is not None and lw.get("charge", 0) > 0
        if laser_active or (li is not None and lw.get("cooldown", 0) <= 0.1 and los and d < lw["stats"].get("range", 280) * 0.95):
            full = lw["stats"].get("charge", 0.75)
            ch = lw.get("charge", 0)
            ly, lp, _, _ = self.aim_solution(me, tpos, tr.vel, None, th)
            err = self.aim_err(me, ly, lp)
            tol = math.atan2(rad * 0.7, max(d, 1))
            if ch >= full * 0.75:
                aim = (ly, lp)  # swing onto the hitscan line just before release
            if ch <= 0:
                fire[li] = los and lw.get("cooldown", 0) <= 0.1
            elif ch < full * 0.97:
                fire[li] = True
            else:
                # full: release when on target and visible, else keep holding (auto-fires after hold_s)
                fire[li] = not (err < tol and los)
        err = self.aim_err(me, aim[0], aim[1])
        if gi is not None and los and d < gw["stats"].get("range", 230) * 0.98:
            gyaw, gpitch = yaw, pitch
            gerr = self.aim_err(me, gyaw, gpitch)
            tol = max(0.004, math.atan2(rad * 0.85, max(d, 1)))
            if gerr < tol and gw.get("ammo", 1) > 0 and gw.get("reload", 0) == 0:
                fire[gi] = True
            if gw.get("ammo", 1) == 0 and gw.get("reload", 0) == 0:
                act["reload"] = True
        if ei is not None:
            rng = ew["stats"].get("range", 45)
            cone = math.radians(ew["stats"].get("cone_deg", 30)) * 0.5
            b = math.atan2(tc[1] - p[1], tc[0] - p[0])
            if d < rng * 0.95 and los and abs(wrap(me["aim"][0] - b)) < cone * 0.85 and me["energy"] >= ew["stats"].get("energy", 3) + 4:
                fire[ei] = True
        if mi is not None and 25 < d < 220 and mw.get("cooldown", 0) <= 0 and not laser_active:
            sol = self.mortar_solution(me, tpos, tr.vel)
            if sol and (ei is None or d > 50):
                myaw, mpitch, _ = sol
                # only steer aim to mortar when gun can't usefully fire
                if gi is None or not los or (gw and gw.get("reload", 0) > 0):
                    aim = (myaw, mpitch)
                if self.aim_err(me, myaw, mpitch) < 0.03:
                    fire[mi] = True
        return fire, aim[0], aim[1]

    # ------------------------------------------------------------------ evasion
    def dodge(self, me, projectiles):
        p = me["pos"]
        best = None
        for pr in projectiles:
            pp, v = pr["pos"], pr["vel"]
            sp2 = v[0] ** 2 + v[1] ** 2
            if sp2 < 1:
                continue
            rx, ry = p[0] - pp[0], p[1] - pp[1]
            tca = (rx * v[0] + ry * v[1]) / sp2
            if tca < 0 or tca > 1.2:
                continue
            cx, cy = pp[0] + v[0] * tca - p[0], pp[1] + v[1] * tca - p[1]
            miss = math.hypot(cx, cy)
            if pr["kind"] == "mortar":
                if miss < 9:
                    away = self.unit(-cx, -cy) if miss > 0.3 else self.unit(-v[1], v[0])
                    best = (away, 0) if best is None else best
                continue
            if miss < 3.5:
                # step perpendicular to the bullet path, toward the side we're already on
                perp = self.unit(-v[1], v[0])
                side = -(cx * perp[0] + cy * perp[1])
                s = 1 if side >= 0 else -1
                if best is None or tca < best[1]:
                    best = ((perp[0] * s, perp[1] * s), tca)
        return best[0] if best else None

    def find_cover(self, me, threats, ok):
        """Nearest walkable in-zone spot within ~32 m hidden from every threat, or None."""
        if not threats or self.map is None:
            return None
        p = me["pos"]
        best = None
        deadline = time.perf_counter() + 0.006
        for r in (7, 13, 20, 27, 34):
            if time.perf_counter() > deadline:
                break
            for k in range(16):
                if time.perf_counter() > deadline:
                    break
                a = k * TAU / 16
                q = (p[0] + math.cos(a) * r, p[1] + math.sin(a) * r)
                if not self.map.walkable(*q) or not ok(q, 10):
                    continue
                # straight-ish route must be walkable
                mid = (p[0] + math.cos(a) * r * 0.5, p[1] + math.sin(a) * r * 0.5)
                if not self.map.walkable(*mid):
                    continue
                fz = self.map.floor_at(*q)
                hidden = True
                for tr in threats:
                    e = (tr.pos[0], tr.pos[1], tr.pos[2] + 8)
                    if self.map.los(e, (q[0], q[1], fz + 4.5)) or self.map.los(e, (q[0], q[1], fz + 8.5)):
                        hidden = False
                        break
                if not hidden:
                    continue
                # prefer spots that do not move toward threats
                toward = sum(d2(p, tr.pos) - d2(q, tr.pos) for tr in threats) / len(threats)
                score = r + max(0.0, toward) * 1.5
                if best is None or score < best[0]:
                    best = (score, q)
            if best:
                return best[1]
        return None

    def explore_point(self, me, nc, nr, zc, zr):
        p = me["pos"]
        for _ in range(12):
            a = random.uniform(0, TAU)
            r = random.uniform(40, 140)
            q = (p[0] + math.cos(a) * r, p[1] + math.sin(a) * r)
            if self.map is not None and not self.map.walkable(*q):
                continue
            if d2(q, zc) > zr - 20:
                continue
            if nr > 30 and d2(q, nc) > max(nr * 0.9, 30):
                continue
            return q
        return None


if __name__ == "__main__":
    mf.run(Bot)
