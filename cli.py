#!/usr/bin/env python3
"""Command-line use of the same index the website uses (handy on a headless server).
  python cli.py add ~/Pictures /media/usb        # add folders and index them (shows progress)
  python cli.py find --face me.jpg --out ./me    # copy every photo containing that face into ./me
  python cli.py people                           # list people found in the library
"""
import argparse
import shutil
import sys
import time
from pathlib import Path

import numpy as np

from facematch import db
from facematch.core import FaceMatcher, MODELS, load_image
from facematch.indexer import Indexer
from facematch.searchindex import Filter, SearchIndex


def cmd_add(a):
    db.init()
    c = db.conn()
    ids = []
    for p in a.folders:
        p = str(Path(p).expanduser().resolve())
        c.execute("INSERT OR IGNORE INTO sources(path, added_at) VALUES(?,?)", (p, time.time()))
        ids.append(c.execute("SELECT id FROM sources WHERE path=?", (p,)).fetchone()[0])
    c.commit()
    ix = Indexer()
    ix.start(ids)
    while ix.thread.is_alive():
        s = ix.status()
        print(f"\r{s['state']:9} {s['done']}/{s['total']}  {s['rate']}/s  {s['message'][:50]:<50}", end="", file=sys.stderr, flush=True)
        time.sleep(1)
    print(file=sys.stderr)
    print("Done. Run `python cli.py people` or `python cli.py find --face photo.jpg`.")


def cmd_find(a):
    db.init()
    m = db.get_setting("model", "sface")
    fm = FaceMatcher(m)
    embs = []
    for f in a.face:
        img = load_image(f)
        faces = fm.embed_faces(img, 1600) if img is not None else []
        if not faces:
            print("no face found in", f)
            continue
        embs.append(max(faces, key=lambda x: x.box[2] * x.box[3]).emb)
    if not embs:
        sys.exit("No usable reference face.")
    q = np.mean(embs, axis=0)
    ix = SearchIndex()
    r = ix.search([(q, None)], a.threshold or MODELS[m]["thr"], Filter(), pen=MODELS[m]["pen"])
    if r is None:
        print("0 matching photos")
        return
    c = db.conn()
    paths = [c.execute("SELECT path FROM photos WHERE id=?", (int(p),)).fetchone()[0] for p in r["photo"]]
    print(f"{len(paths)} matching photos")
    for p, s in zip(paths, r["s"]):
        print(f"{s:.3f}  {p}")
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        for p in paths:
            dst = out / Path(p).name
            k = 1
            while dst.exists():
                dst = out / f"{Path(p).stem}_{k}{Path(p).suffix}"
                k += 1
            shutil.copy2(p, dst)
        print(f"Copied to {out}")


def cmd_people(a):
    db.init()
    from facematch import cluster
    ix = SearchIndex()
    if a.scan:
        print("found", cluster.discover(ix, lambda m: print("\r" + m, end="", file=sys.stderr)), "new people")
    for r in db.conn().execute("SELECT id,name,nphotos FROM people WHERE hidden=0 ORDER BY nphotos DESC"):
        print(f"#{r['id']:<5} {r['name'] or '(unnamed)':<24} {r['nphotos']} photos")


ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
sp = ap.add_subparsers(dest="cmd", required=True)
p = sp.add_parser("add"); p.add_argument("folders", nargs="+"); p.set_defaults(fn=cmd_add)
p = sp.add_parser("find"); p.add_argument("--face", "-f", nargs="+", required=True); p.add_argument("--out", "-o")
p.add_argument("--threshold", "-t", type=float); p.set_defaults(fn=cmd_find)
p = sp.add_parser("people"); p.add_argument("--scan", action="store_true", help="(re)group faces into people first"); p.set_defaults(fn=cmd_people)
a = ap.parse_args()
a.fn(a)
