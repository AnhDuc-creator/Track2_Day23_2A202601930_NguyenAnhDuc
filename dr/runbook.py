"""BƯỚC 3c — Tu dong hoa runbook "Region Chinh Down". 7 buoc, ban tu dong.

  1 xac_nhan_outage        3 scale_gpu_pool (goi failover MOT LAN)
  2 thong_bao_incident     4 verify_state_replica (chi DOC ket qua buoc 3)
  6 verify_golden_signals  5 dns_cutover (chi DOC lai)
  7 post_incident

Buoc 1 CHO dr/health_checker.py xac nhan UNHEALTHY roi moi failover. Neu runbook
tu probe roi cutover ngay, t_cutover se nho hon t_detect va tools/measure_rto.py
danh dau: con so RTO do duoc la do tay nguoi, khong do automation.

Mac dinh hoi y/N truoc khi failover (chong flapping 2 chieu). --auto chi dung
cho CI / drill cham diem.

Chay:  python3 dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
EDGE = "http://127.0.0.1:8080/v1/infer"
CHAOS = pathlib.Path("chaos/chaos-events.jsonl")
HEALTH_LOG = pathlib.Path("reports/health-events.jsonl")


def step(n, name, **kw):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(),
           "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("RUNBOOK", json.dumps(rec, ensure_ascii=False))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    if auto:
        print(f"[--auto] {msg} -> YES")
        return True
    try:
        return input(f"{msg} [y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _ready(region: str) -> bool:
    try:
        return httpx.get(f"{URL[region]}/readyz", timeout=2.0).status_code == 200
    except Exception:
        return False


def _last_kill_ts():
    if not CHAOS.exists():
        return None
    kills = [json.loads(l) for l in CHAOS.read_text().splitlines()
             if l.strip() and json.loads(l).get("action") == "kill"]
    return kills[-1]["ts"] if kills else None


def _health_says_unhealthy(region: str, after_ts):
    """Tim dong UNHEALTHY cua health checker cho region nay, sau moc after_ts.

    Runbook KHONG duoc tu quyet dinh outage roi cutover — phai doi he thong
    giam sat xac nhan truoc. Cutover som hon t_detect nghia la con so RTO do
    duoc la do tay nguoi, khong do automation.
    """
    if not HEALTH_LOG.exists():
        return None
    for line in HEALTH_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if (e.get("event") == "state_change" and e.get("to") == "UNHEALTHY"
                and e.get("region") == region
                and (after_ts is None or e.get("ts", 0) >= after_ts)):
            return e
    return None


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    t_start = time.time()
    t_outage = _last_kill_ts()

    # --- 1: doi HEALTH CHECKER xac nhan, khong tu probe roi cutover ---------
    detect, waited = None, 0.0
    while waited < 90.0:
        detect = _health_says_unhealthy(primary, t_outage)
        if detect:
            break
        time.sleep(0.5)
        waited = time.time() - t_start

    probes = [{"primary_ready": _ready(primary), "target_ready": _ready(target)}]
    primary_down = not probes[0]["primary_ready"]

    step(1, "xac_nhan_outage", primary=primary, target=target,
         nguon_xac_nhan="dr/health_checker.py" if detect else "probe truc tiep",
         t_detect=detect.get("ts") if detect else None,
         detect_reason=detect.get("reason") if detect else None,
         interval_s=detect.get("interval_s") if detect else None,
         threshold=detect.get("threshold") if detect else None,
         detect_floor_s=detect.get("detect_floor_s") if detect else None,
         cho_health_checker_s=round(waited, 2),
         probes=probes, primary_down=primary_down)

    if not detect and not primary_down:
        step(7, "post_incident", aborted=True,
             reason=f"region-{primary} van tra /readyz 200 -> khong failover")
        return {"ok": False, "reason": "primary_still_healthy"}

    # --- 2: operator biet tin LUC NAO (luon sau t_outage va sau t_detect) ---
    t_known = time.time()
    step(2, "thong_bao_incident", severity="SEV1",
         t_outage=t_outage, t_detect=detect.get("ts") if detect else None,
         t_operator_biet=t_known,
         do_tre_thong_bao_s=None if t_outage is None else round(t_known - t_outage, 2),
         do_tre_sau_detect_s=None if not detect else round(t_known - detect["ts"], 2),
         kenh="oncall + #incident")

    if not confirm(auto, f"Failover region-{primary} -> region-{target}?"):
        step(7, "post_incident", aborted=True, reason="operator khong confirm")
        return {"ok": False, "reason": "not_confirmed"}

    # --- 3: goi failover DUNG MOT LAN ---------------------------------------
    res = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", failover_ok=res.get("ok"),
         aborted_at=res.get("aborted_at"),
         warmup_seconds=res.get("warmup_seconds"),
         elapsed_s=res.get("elapsed_s"))

    # --- 4: CHI DOC lai ket qua, khong goi lai failover ----------------------
    st = res.get("target_state_after") or {}
    step(4, "verify_state_replica", target=target,
         vector_count=st.get("count"), weights=st.get("weights"),
         pool_state=st.get("pool_state"),
         rpo_seconds=res.get("rpo_seconds"), docs_lost=res.get("docs_lost"),
         embed_model_version=res.get("embed_model_version"))

    # --- 5: cung chi doc lai -------------------------------------------------
    active = pathlib.Path("edge/active_region")
    step(5, "dns_cutover", cutover_ok=bool(res.get("cutover_ok")),
         active_region=active.read_text().strip() if active.exists() else None)

    if not res.get("ok"):
        step(7, "post_incident", ok=False, aborted_at=res.get("aborted_at"),
             elapsed_s=round(time.time() - t_start, 2))
        return {"ok": False, "reason": res.get("aborted_at"), "failover": res}

    # --- 6: golden signals bang 10 request THAT qua edge --------------------
    lat, errs, served = [], 0, {}
    for i in range(10):
        t = time.time()
        try:
            r = httpx.get(EDGE, params={"q": f"hoa don thang {i % 12 + 1}"}, timeout=5.0)
            body = r.json()
            if r.status_code != 200 or body.get("error"):
                errs += 1
            else:
                served[body.get("region")] = served.get(body.get("region"), 0) + 1
        except Exception:
            errs += 1
        lat.append(round((time.time() - t) * 1000, 1))
        time.sleep(0.2)
    s = sorted(lat)
    p95 = s[min(int(0.95 * (len(s) - 1)), len(s) - 1)]
    step(6, "verify_golden_signals", requests=10, errors=errs,
         error_rate=round(errs / 10, 2), p95_latency_ms=p95,
         p50_latency_ms=s[len(s) // 2], served_by=served)

    # --- 7 ------------------------------------------------------------------
    elapsed = round(time.time() - t_start, 2)
    step(7, "post_incident", ok=True, elapsed_s=elapsed,
         rpo_seconds=res.get("rpo_seconds"), docs_lost=res.get("docs_lost"),
         lenh_do_rto="python3 tools/measure_rto.py --loadgen "
                     "reports/drill-2-withdr.jsonl --target-rto 300")

    return {"ok": True, "primary": primary, "target": target,
            "runbook_elapsed_s": elapsed, "error_rate": round(errs / 10, 2),
            "p95_latency_ms": p95, "failover": res}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2, ensure_ascii=False))