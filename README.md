# oval-survivor: a Mech Royale bot (mechfight/1)

Python bot for the Mech Royale buildathon. The `Dockerfile` at the top level builds the image the scorer runs.

## Files

| Path | What it is |
| --- | --- |
| `bot.py` | The bot: zone handling, looting, target selection, led gun shots, laser charge/release, lightning, mortar, projectile dodging, cover-seeking retreat, upgrade picks. |
| `navmap.py` | Static map model built from `/arena` at `hello`: floor heights, a walkability grid (buildings inflated by the mech radius, steep slopes), line-of-sight, and a time-budgeted A*. |
| `mechfight.py` | The kit's protocol wrapper (unchanged). |
| `sim/` | A rough local simulator (`sim.py`, `par.py`) and an observation fuzzer (`fuzz.py`). It is not the real game. |
| `check/`, `arena/`, `protocol.md`, `mechfight.schema.json` | From the official kit, used for local testing. |

## Test

```sh
python3 check/check.py -- python3 -u bot.py
docker build -t my-bot .
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" my-bot
python3 sim/fuzz.py            # random observations, must report 0 errors
LASER_P=0.5 python3 sim/par.py 64 4   # crude sim: mean kills vs hunters
```
