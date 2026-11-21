"""Feed randomized observations through Bot.act and fail on any exception."""
import gzip, json, os, random, sys, time, traceback, copy
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bot as botmod
botmod.mf.draw.caps = {}
with gzip.open(os.path.join(ROOT, "check", "stream.jsonl.gz"), "rt") as f:
    lines = f.read().splitlines()
hello = json.loads(lines[0]); hello["map"]["dir"] = os.path.join(ROOT, "arena")
obs0 = [json.loads(l) for l in lines[1:] if '"obs"' in l[:20]]
W = hello["config"]["weapons"]
def wobj(k):
    o = {"kind": k, "cooldown": random.choice([0, 0.5]), "stats": dict(W[k])}
    if k == "gun": o.update(ammo=random.randint(0, 12), reload=random.choice([0, 0, 1.2]))
    if k == "laser": o.update(charge=random.choice([0, 0, 0.3, 0.75]))
    return o
R = random.Random(5)
random.seed(5)
errs = 0; worst = 0
for trial in range(6):
    b = botmod.Bot(copy.deepcopy(hello))
    for i, o in enumerate(obs0):
        o = copy.deepcopy(o)
        me = o["self"]; p = me["pos"]
        kinds = R.sample(["gun", "laser", "lightning", "mortar", None, None], 2)
        me["weapons"] = [wobj(k) if k else None for k in kinds]
        me["hp"] = R.uniform(50, 1400); me["energy"] = R.uniform(0, 100); me["cubes"] = R.randint(0, 12)
        me["in_zone"] = R.random() < 0.8
        def rp(r=120): return [p[0] + R.uniform(-r, r), p[1] + R.uniform(-r, r), p[2] + R.uniform(-2, 10)]
        if R.random() < 0.5:
            o["mechs"].append({"id": 100 + R.randint(0, 11), "kind": "npc", "pos": rp(), "vel": [1, 0, 0], "aim": [0, 0], "hp": 350, "max_hp": 350})
        if R.random() < 0.3:
            o["mechs"].append({"id": R.randint(4, 15), "kind": "mech", "pos": rp(60), "vel": [R.uniform(-12, 12), R.uniform(-12, 12), 0], "aim": [R.uniform(-3, 3), 0], "hp": R.uniform(10, 1400), "max_hp": 1400, "cubes": R.randint(0, 5), "weapons": R.sample(["gun", "laser", "lightning", "mortar"], 2), "charging": R.random() < 0.3})
        o["cubes"] = [{"id": 5000 + k, "pos": rp(80)} for k in range(R.randint(0, 3))]
        o["crates"] = [{"id": 1000 + k, "pos": rp(100), "hp": 300} for k in range(R.randint(0, 3))]
        o["pickups"] = [{"id": 8000 + k, "kind": R.choice(["laser", "lightning", "mortar", "gun"]), "pos": rp(R.choice([3, 50]))} for k in range(R.randint(0, 3))]
        o["projectiles"] = [{"id": k, "kind": R.choice(["gun", "mortar", "npc"]), "pos": rp(50), "vel": [R.uniform(-200, 200), R.uniform(-200, 200), R.uniform(-20, 20)]} for k in range(R.randint(0, 4))]
        o["sounds"] = [{"kind": R.choice(["gun", "step", "laser", "death"]), "bearing": R.uniform(-3, 3), "dist": R.choice(["near", "mid", "far"]), "age": 0.1}]
        if R.random() < 0.3:
            o["drops"] = [{"id": 20000, "pos": rp(150), "radius": 8, "lands_in": R.choice([0, 10]), "capture_s": 4, "progress": 0, "holder": R.choice([None, 3, 5])}]
        if R.random() < 0.1:
            ups = hello["config"]["upgrades"]
            o["offer"] = {"id": 1, "expires": o["t"] + 5, "choices": [{"id": u["id"], "name": "", "desc": ""} for u in R.sample(ups, 3)]}
        if R.random() < 0.2:
            o["events"].append({"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 1.0, "by": 7, "rear": True})
        if R.random() < 0.05:
            o["events"].append({"type": "take_failed", "id": 8000, "reason": "gone"})
        if R.random() < 0.1:
            o["zone"]["radius"] = R.choice([0, 25, 70]); o["zone"]["next_radius"] = 0; o["t"] = 420
        try:
            t = time.perf_counter(); a = b.act(o); worst = max(worst, time.perf_counter() - t)
            json.dumps(a)
            assert isinstance(a["fire"], list) and len(a["fire"]) == 2
            if "take" in a: assert isinstance(a["take"], (int, float))
        except Exception:
            errs += 1
            if errs < 4: traceback.print_exc()
        botmod.mf.draw.clear()
print("errors", errs, "worst_ms", round(worst * 1000, 1))
