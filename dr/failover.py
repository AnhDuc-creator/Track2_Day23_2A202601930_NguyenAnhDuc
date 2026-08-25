"""BƯỚC 3b — Cutover sang region phu. 5 buoc, THU TU BAT BUOC.

  1_verify_target    — doc /v1/state cua region dich
  2_restore_snapshot — snapshot.get + snapshot.rpo -> rpo_seconds, docs_lost,
                       embed_model_version
  3_scale_pool       — ghi "full" vao state/region-<t>/pool_state
  4_wait_ready       — poll /readyz toi khi 200 (GPU pool warm-up nam o day)
  5_dns_cutover      — CHI DEN LUC NAY moi ghi edge/active_region

Buoc 4 timeout -> ABORT, KHONG cutover. Doi DNS truoc khi target ready = user
nhan 503 tu CA HAI region -> RTO dai hon chu khong ngan hon.

Chay:  python3 dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")
ACTIVE = pathlib.Path("edge/active_region")


def emit(**kw):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(),
           "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("FAILOVER", json.dumps(rec, ensure_ascii=False))
    return rec


def state_of(region: str) -> dict:
    """Doc /v1/state cua 1 region. Khong duoc nem exception ra ngoai."""
    try:
        return httpx.get(f"{URL[region]}/v1/state", timeout=3.0).json()
    except Exception as e:
        return {"region": region, "error": type(e).__name__}


def failover(target: str, backend: str, wait: float) -> dict:
    t0 = time.time()
    primary = "a" if target == "b" else "b"

    # --- 1_verify_target -----------------------------------------------------
    before = state_of(target)
    emit(step="1_verify_target", target=target, state=before,
         weights=before.get("weights"), count=before.get("count"),
         pool_state=before.get("pool_state"))

    # --- 2_restore_snapshot --------------------------------------------------
    try:
        meta = snapshot.get(target, backend)
    except (Exception, SystemExit) as e:
        emit(step="2_restore_snapshot", ok=False, error=str(e),
             hint="chua co snapshot nao duoc put -> chay state/replicate.py truoc")
        return {"ok": False, "aborted_at": "2_restore_snapshot", "error": str(e)}

    prim_db = pathlib.Path(f"state/region-{primary}/vectors.sqlite")
    rest_db = pathlib.Path(f"state/region-{target}/vectors.sqlite")
    try:
        r = snapshot.rpo(prim_db, rest_db)
    except Exception as e:
        r = {"rpo_seconds": None, "docs_lost": None, "error": type(e).__name__}
    emit(step="2_restore_snapshot", ok=True,
         rpo_seconds=r.get("rpo_seconds"), docs_lost=r.get("docs_lost"),
         embed_model_version=meta.get("embed_model_version"),
         snapshot_at=meta.get("snapshot_at"),
         primary_latest_doc_ts=r.get("primary_latest_doc_ts"),
         restored_latest_doc_ts=r.get("restored_latest_doc_ts"))

    # --- 3_scale_pool --------------------------------------------------------
    pool = pathlib.Path(f"state/region-{target}/pool_state")
    pool.parent.mkdir(parents=True, exist_ok=True)
    was = pool.read_text().strip() if pool.exists() else "cold"
    pool.write_text("full")
    t_scale = time.time()
    emit(step="3_scale_pool", target=target, **{"from": was, "to": "full"})

    # --- 4_wait_ready --------------------------------------------------------
    deadline = time.time() + wait
    ready, last = False, None
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            last = f"http_{resp.status_code}"
            if resp.status_code == 200:
                ready = True
                break
        except Exception as e:
            last = type(e).__name__
        time.sleep(0.5)
    warm_s = round(time.time() - t_scale, 2)
    emit(step="4_wait_ready", target=target, ready=ready,
         warmup_seconds=warm_s, waited_s=round(time.time() - t_scale, 2),
         last_probe=last)

    if not ready:
        emit(step="abort", reason="4_wait_ready timeout -> KHONG cutover",
             target=target, note="doi DNS luc nay se cho user 503 tu ca hai region")
        return {"ok": False, "aborted_at": "4_wait_ready",
                "rpo_seconds": r.get("rpo_seconds"), "docs_lost": r.get("docs_lost"),
                "warmup_seconds": warm_s, "target_state_after": state_of(target)}

    # --- 5_dns_cutover -------------------------------------------------------
    ACTIVE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE.write_text(target)
    emit(step="5_dns_cutover", active_region=target,
         file=str(ACTIVE), elapsed_s=round(time.time() - t0, 2))

    after = state_of(target)
    return {"ok": True, "target": target, "backend": backend,
            "rpo_seconds": r.get("rpo_seconds"), "docs_lost": r.get("docs_lost"),
            "embed_model_version": meta.get("embed_model_version"),
            "warmup_seconds": warm_s, "cutover_ok": True,
            "target_state_after": after,
            "elapsed_s": round(time.time() - t0, 2)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2, ensure_ascii=False))