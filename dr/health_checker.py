"""BƯỚC 3a — Health checker cho 2 region (chống flapping).

Poll /readyz cua CA HAI region moi `interval` giay. Chi doi trang thai sau
`threshold` lan fail LIEN TIEP (va `threshold` lan ok lien tiep de quay lai
HEALTHY). Chi ghi JSONL khi trang thai THUC SU doi.

Detection floor = interval * threshold. Voi interval=5, threshold=3 -> 15s.
Con so do nam TRONG RTO. Muon RTO 300s thi interval*threshold phai du nho de
phan con lai (restore + warmup + DNS TTL) van lot duoi 300s.

Chay:  python3 dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
REGIONS = ["a", "b"]


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Tra ve (ready, reason). Timeout BAT BUOC: netblock lam request treo mai."""
    try:
        r = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
    except Exception as e:
        return False, type(e).__name__
    if r.status_code == 200:
        return True, "ready"
    try:
        reasons = r.json().get("reasons", [])
    except Exception:
        reasons = []
    return False, f"http_{r.status_code}" + (":" + ",".join(reasons) if reasons else "")


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Gia dinh khoi dau la HEALTHY -> khong ghi dong "HEALTHY" gia o cycle dau.
    state = {r: "HEALTHY" for r in REGIONS}
    fails = {r: 0 for r in REGIONS}
    oks = {r: 0 for r in REGIONS}
    end = time.time() + duration

    with out.open("a") as f:
        def emit(region, to, reason, consec_fail, consec_ok):
            rec = {
                "ts": time.time(),
                "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                "event": "state_change",
                "region": region,
                "from": state[region],
                "to": to,
                "reason": reason,
                "consecutive_fails": consec_fail,
                "consecutive_oks": consec_ok,
                "interval_s": interval,
                "threshold": threshold,
                "detect_floor_s": round(interval * threshold, 2),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            print("HEALTH", json.dumps(rec, ensure_ascii=False))

        while time.time() < end:
            t0 = time.time()
            for r in REGIONS:
                ready, reason = probe(r, timeout)
                if ready:
                    oks[r] += 1
                    fails[r] = 0
                    if state[r] != "HEALTHY" and oks[r] >= threshold:
                        emit(r, "HEALTHY", reason, 0, oks[r])
                        state[r] = "HEALTHY"
                else:
                    fails[r] += 1
                    oks[r] = 0
                    if state[r] != "UNHEALTHY" and fails[r] >= threshold:
                        emit(r, "UNHEALTHY", reason, fails[r], 0)
                        state[r] = "UNHEALTHY"
            time.sleep(max(0.0, interval - (time.time() - t0)))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))