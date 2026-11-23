#!/usr/bin/env python3
"""
episode_tracker.py - standalone, READ-ONLY signal outcome tracker.

Touches nothing in app/. Opens analyzer.db read-only, groups repeated BUY CE /
BUY PE rows into ONE episode, then replays the real option premium path from
option_data to see what happened FIRST: T1, SL, or neither by market close.

Usage (from ~/nifty):
    venv/bin/python3 tools/episode_tracker.py --since 2026-09-16
    venv/bin/python3 tools/episode_tracker.py --db /path/analyzer.db --out /tmp/ep.csv

Timestamps in the DB are UTC-naive. NSE session = 03:45-10:00 UTC (09:15-15:30 IST).
"""
import argparse, csv, json, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta

SESSION_OPEN_UTC = (3, 45)
SESSION_CLOSE_UTC = (10, 0)
EPISODE_GAP_MIN = 30          # rows further apart than this start a new episode
ACTIONS = ("BUY CE", "BUY PE", "SELL CE", "SELL PE")


def ts(s):
    return datetime.fromisoformat(str(s).replace("T", " ")[:26])


def in_session(t):
    if t.weekday() >= 5:
        return False
    hm = (t.hour, t.minute)
    return SESSION_OPEN_UTC <= hm <= SESSION_CLOSE_UTC


def load_signal_rows(con, since):
    rows = con.execute(
        "SELECT id, timestamp, result FROM analysis_results "
        "WHERE analysis_type='ai' AND symbol='NIFTY' AND timestamp>=? ORDER BY timestamp",
        (since,),
    ).fetchall()
    out = []
    for id_, t, res in rows:
        t = ts(t)
        if not in_session(t):
            continue
        d = json.loads(res) if isinstance(res, str) else (res or {})
        action = str(d.get("signal_action") or "").upper()
        if action not in ACTIONS:
            continue
        ro = d.get("recommended_option") or {}
        mtf = d.get("multi_timeframe") or {}
        tt = d.get("timeframe_trend") or {}
        trend = lambda k: (mtf.get(k) or {}).get("trend") or tt.get(k)
        if not (ro.get("strike") and ro.get("entry_price") and ro.get("sl")):
            continue
        out.append(dict(
            id=id_, ts=t, action=action, regime=d.get("market_regime"),
            margin=d.get("margin"), t5=trend("5min"), t15=trend("15min"), t1h=trend("1hr"),
            strike=float(ro["strike"]), otype=ro.get("type"), expiry=ro.get("expiry"),
            entry=float(ro["entry_price"]), sl=float(ro["sl"]),
            t1=float(ro["t1"]) if ro.get("t1") else None,
            t2=float(ro["t2"]) if ro.get("t2") else None,
        ))
    return out


def group_episodes(rows):
    eps, cur = [], None
    for r in rows:
        key = (r["action"], r["strike"], r["otype"], r["expiry"], r["ts"].date())
        if cur and cur["key"] == key and (r["ts"] - cur["last"]).total_seconds() <= EPISODE_GAP_MIN * 60:
            cur["rows"] += 1
            cur["last"] = r["ts"]
        else:
            cur = dict(key=key, first=r, last=r["ts"], rows=1)
            eps.append(cur)
    return eps


def replay(con, ep):
    r = ep["first"]
    t0 = r["ts"]
    close = datetime(t0.year, t0.month, t0.day, *SESSION_CLOSE_UTC)
    q = con.execute(
        "SELECT timestamp, last_price FROM option_data "
        "WHERE symbol='NIFTY' AND strike=? AND option_type=? AND expiry=? "
        "AND timestamp>? AND timestamp<=? ORDER BY timestamp",
        (r["strike"], r["otype"], r["expiry"], str(t0), str(close)),
    ).fetchall()
    path = [(ts(a), b) for a, b in q if b and b > 0]

    action = r["action"]
    is_sell = action in ("SELL CE", "SELL PE")

    risk = abs(r["entry"] - r["sl"])
    if r["t1"] is not None:
        t1 = r["t1"]
    else:
        t1 = r["entry"] - risk if is_sell else r["entry"] + risk

    res = dict(
        start_utc=t0.strftime("%Y-%m-%d %H:%M"), action=action, rows_in_episode=ep["rows"],
        regime=r["regime"], t5=r["t5"], t15=r["t15"], t1h=r["t1h"], margin=r["margin"],
        strike=r["strike"], expiry=r["expiry"], entry=r["entry"], sl=r["sl"], t1=round(t1, 2),
        t1_derived=int(r["t1"] is None),
        sl_pct=round((r["sl"] / r["entry"] - 1) * 100, 1),
        mins_to_close=round((close - t0).total_seconds() / 60), ticks=len(path),
    )

    if len(path) < 3:
        res.update(
            first="NO_DATA", mfe_pct="", mae_pct="", eod_pct="", mins_to_first="",
            favourable_eod_pct=""
        )
        return res

    first, mins = "NONE", ""
    for t, p in path:
        hit_sl = p >= r["sl"] if is_sell else p <= r["sl"]
        hit_t1 = p <= t1 if is_sell else p >= t1

        if hit_sl and hit_t1:
            first = "AMBIGUOUS"
            mins = round((t - t0).total_seconds() / 60)
            break
        if hit_sl:
            first = "SL"
            mins = round((t - t0).total_seconds() / 60)
            break
        if hit_t1:
            first = "T1"
            mins = round((t - t0).total_seconds() / 60)
            break

    px = [p for _, p in path]
    entry = r["entry"]

    if is_sell:
        mfe_pct = (entry - min(px)) / entry * 100
        mae_pct = (max(px) - entry) / entry * 100
        favourable_eod_pct = (entry - px[-1]) / entry * 100
    else:
        mfe_pct = (max(px) - entry) / entry * 100
        mae_pct = (entry - min(px)) / entry * 100
        favourable_eod_pct = (px[-1] - entry) / entry * 100

    res.update(
        first=first,
        mins_to_first=mins,
        mfe_pct=round(mfe_pct, 1),
        mae_pct=round(mae_pct, 1),
        eod_pct=round((px[-1] / entry - 1) * 100, 1),
        favourable_eod_pct=round(favourable_eod_pct, 1),
    )
    return res


def summarise(results):
    ok = [r for r in results if r["first"] != "NO_DATA"]
    print(f"\nEpisodes: {len(results)}  (with price data: {len(ok)})")

    def table(title, keyfn):
        g = defaultdict(lambda: defaultdict(int))
        for r in ok:
            g[keyfn(r)][r["first"]] += 1
        print(f"\n-- by {title} --")
        print(f"{'':16}{'n':>4}{'T1':>5}{'SL':>5}{'NONE':>6}  T1-rate")
        for k in sorted(g, key=str):
            c = g[k]; n = sum(c.values())
            print(f"{str(k):16}{n:4}{c['T1']:5}{c['SL']:5}{c['AMBIGUOUS']:5}{c['NONE']:6}  {c['T1']*100/n:5.0f}%")

    table("action", lambda r: r["action"])
    table("1h trend", lambda r: r["t1h"])
    table("time left", lambda r: "<60m" if r["mins_to_close"] < 60 else ("60-120m" if r["mins_to_close"] < 120 else ">120m"))
    table("regime", lambda r: r["regime"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/analyzer.db")
    ap.add_argument("--since", default=(datetime.now().astimezone().astimezone(__import__("datetime").timezone.utc).replace(tzinfo=None) - timedelta(days=30)).strftime("%Y-%m-%d"))
    ap.add_argument("--out", default="data/episode_report.csv")
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)   # read-only: cannot modify the live DB
    rows = load_signal_rows(con, a.since)
    eps = group_episodes(rows)
    results = [replay(con, e) for e in eps]
    if not results:
        print("No actionable CE/PE episodes found since", a.since); return 0
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader(); w.writerows(results)
    print(f"{len(rows)} signal rows -> {len(eps)} episodes -> {a.out}")
    summarise(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
