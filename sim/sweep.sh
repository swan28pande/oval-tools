#!/bin/bash
# usage: sim/sweep.sh N '{"k":v}' ...   (tiered 15-hunter sim, same seeds for each variant)
N=$1; shift
for p in "$@"; do
  r=$(BOT_PARAMS="$p" LASER_P=0.6 ME_LASER=rand python3 sim/par.py $N 15 2>&1 | head -2 | tr '\n' ' ')
  echo "$p => $r"
done
