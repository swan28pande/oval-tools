"""Parallel sim runner: python3 sim/par.py N [hunters]. Env LASER_P, ME_LASER as in sim.py."""
import os, sys
from multiprocessing import Pool
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def one(seed):
    import io, contextlib
    import sim
    with contextlib.redirect_stderr(io.StringIO()):
        return sim.run_match(seed, NH)

NH = int(sys.argv[2]) if len(sys.argv) > 2 else 4
if __name__ == "__main__":
    n = int(sys.argv[1])
    base = int(os.environ.get("BASE", "2000"))
    with Pool(int(os.environ.get("PROCS", "8"))) as p:
        res = p.map(one, range(base, base + n))
    k = [r["kills"] for r in res]
    print(f"n={n} kills={sum(k)/n:.2f} survive={sum(r['survived_t'] for r in res)/n:.0f}s worst_ms={max(r['worst_ms'] for r in res)}")
    S = {k: sum(r['stats'][k] for r in res) for k in res[0]['stats']}
    from collections import Counter
    print("attackers within 150 m at death:", sorted(Counter(r['near'] for r in res).items()), " kills hist:", sorted(Counter(r['kills'] for r in res).items()))
    print(f"our gun hit rate {S['me_hits']/max(1,S['me_shots']):.2f}  hunters' {S['h_hits']/max(1,S['h_shots']):.2f}")
    print(sorted(((r['worst_ms'], r['worst_at']) for r in res), key=lambda x: -x[0])[:5])
