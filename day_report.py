import sqlite3, sys, os
db = sys.argv[1] if len(sys.argv) > 1 else "./data/analyzer.db"
c = sqlite3.connect(f"file:{os.path.abspath(db)}?mode=ro", uri=True)  # read-only
sym = "NIFTY"
print("OHLC timestamp range:", c.execute("select min(timestamp), max(timestamp), count(*) from intraday_ohlc where symbol=?", (sym,)).fetchone())
days = c.execute("""select date(timestamp) d, min(timestamp), max(timestamp), max(high), min(low), count(*)
                    from intraday_ohlc where symbol=? group by d order by d desc limit 25""", (sym,)).fetchall()
print("\nday | bars | open->close % | range % | bull_sig | bear_sig | first_sig(UTC) | verdict")
for d, t0, t1, hi, lo, n in days:
    o = c.execute("select coalesce(open, close) from intraday_ohlc where symbol=? and timestamp=?", (sym, t0)).fetchone()[0]
    cl = c.execute("select close from intraday_ohlc where symbol=? and timestamp=?", (sym, t1)).fetchone()[0]
    mv = (cl - o) / o * 100; rg = (hi - lo) / o * 100
    r = c.execute("""select
        sum(case when action in ('BUY CE','SELL PE') then 1 else 0 end),
        sum(case when action in ('BUY PE','SELL CE') then 1 else 0 end),
        min(time(timestamp)) from daily_signal_ledger
        where symbol=? and date(timestamp)=?""", (sym, d)).fetchone()
    bull, bear, first = (r[0] or 0), (r[1] or 0), r[2] or "-"
    if abs(mv) >= 0.5 and bull + bear == 0: v = "MISSED big move"
    elif mv >= 0.3 and bear > bull: v = "WRONG side (up day, more bearish sigs)"
    elif mv <= -0.3 and bull > bear: v = "WRONG side (down day, more bullish sigs)"
    elif abs(mv) >= 0.3: v = "aligned"
    else: v = "flat day"
    print(f"{d} | {n} | {mv:+.2f} | {rg:.2f} | {bull} | {bear} | {first} | {v}")
