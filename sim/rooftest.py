"""3D movement check for rooftop loot: gravity, jump, jets, roofs from the map's solid heights."""
import gzip, json, math, os, random, sys, copy
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bot as botmod
botmod.mf.draw.caps = {}
with gzip.open(os.path.join(ROOT, "check", "stream.jsonl.gz"), "rt") as f:
    lines = f.read().splitlines()
hello = json.loads(lines[0]); hello["map"]["dir"] = os.path.join(ROOT, "arena")
base = json.loads(lines[1])
b = botmod.Bot(copy.deepcopy(hello)); M = b.map
def surf(x, y):
    i, j = M._ij(x, y); return float(M.top[j, i])
def run(start, item, secs=40):
    b2 = botmod.Bot(copy.deepcopy(hello))
    x, y = start; z = surf(x, y); vx = vy = vz = 0.0; e = 100.0; grounded = True
    for k in range(int(secs * 10)):
        o = copy.deepcopy(base); o["tick"] = k * 2; o["t"] = k / 10
        me = o["self"]; me.update(pos=[x, y, z], vel=[vx, vy, vz], energy=e, grounded=grounded)
        o["pickups"] = [{"id": 8001, "kind": "laser", "pos": list(item)}] if math.dist((x, y), item[:2]) < 150 else []
        o["zone"] = {"center": [x, y], "radius": 560, "next_center": [x, y], "next_radius": 380, "shrink_start": 999, "shrink_end": 1100, "dps": 20}
        a = b2.act(o); botmod.mf.draw.clear()
        if a.get("take") == 8001 and math.dist((x, y), item[:2]) < 4 and abs(z - item[2]) < 4:
            return True, k / 10, e
        mv = a.get("move", [0, 0]); boost = a.get("boost"); jump = a.get("jump")
        for _ in range(2):
            dt = 0.05
            sp = 19.5 if boost and grounded else 11.7
            acc = 58.5 if grounded else 12
            for c, m_ in ((0, mv[0]), (1, mv[1])):
                cur = vx if c == 0 else vy
                cur += max(-acc * dt, min(acc * dt, m_ * sp - cur))
                if c == 0: vx = cur
                else: vy = cur
            if jump and grounded: vz = 11; grounded = False; jump = False
            if not grounded and boost and e > 0:
                vz = min(10, vz + 34 * dt); e -= 15 * dt
            elif grounded and boost: e -= 17 * dt
            else: e = min(100, e + 18 * dt)
            vz -= 22 * dt
            nx, ny = x + vx * dt, y + vy * dt
            if surf(nx, ny) <= z + 1.2: x, y = nx, ny
            elif surf(nx, y) <= z + 1.2: x = nx; vy = 0
            elif surf(x, ny) <= z + 1.2: y = ny; vx = 0
            else: vx = vy = 0
            z += vz * dt
            g = surf(x, y)
            if z <= g: z, vz, grounded = g, 0.0, True
            else: grounded = False
    return False, secs, e
# pick roof spots: cells whose solid top is 6..35 m above terrain
R = random.Random(3); ok = 0; n = 0
while n < 30:
    i = R.randrange(50, M.nx - 50); j = R.randrange(50, M.ny - 50)
    h = float(M.top[j, i] - M.terrain[j, i])
    if not (6 < h < 35): continue
    ix, iy = M.minX + i, M.minY + j
    if any(float(M.top[j + dj, i + di]) < M.top[j, i] - 0.5 for di in (-3, 0, 3) for dj in (-3, 0, 3)): continue  # flat roof patch
    nf = M.nearest_free(ix, iy, maxr=20)
    if nf is None: continue
    sx, sy = M.gpos(*nf)
    a = R.uniform(0, 6.28); st = (sx + 40 * math.cos(a), sy + 40 * math.sin(a))
    if not M.walkable(*st): continue
    n += 1
    res = run(st, (ix, iy, float(M.top[j, i]) + 0.1))
    ok += res[0]
    print(f"roof h={h:5.1f}  got={res[0]}  t={res[1]:5.1f}s  energy_left={res[2]:.0f}")
print("success", ok, "/", n)
