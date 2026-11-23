import sqlite3, sys, os
db = sys.argv[1] if len(sys.argv) > 1 else "./data/analyzer.db"
c = sqlite3.connect(f"file:{os.path.abspath(db)}?mode=ro", uri=True)  # read-only
sym = "NIFTY"
days = [r[0] for r in c.execute("select distinct date(timestamp) d from intraday_ohlc where symbol=? order by d desc limit 25", (sym,))]
print("day | dir | open->close% | first30m% | pre-signal% | post-signal% | first_sig(UTC) | captured_of_total")
tot_pre = tot_post = n = 0
for d in sorted(days):
    bars = c.execute("select timestamp, coalesce(open, close), close from intraday_ohlc where symbol=? and date(timestamp)=? order by timestamp", (sym, d)).fetchall()
    if len(bars) < 30: continue
    o, cl = bars[0][1], bars[-1][2]
    r = c.execute("""select sum(case when action in ('BUY CE','SELL PE') then 1 else 0 end),
                            sum(case when action in ('BUY PE','SELL CE') then 1 else 0 end),
                            min(timestamp) from daily_signal_ledger where symbol=? and date(timestamp)=?""", (sym, d)).fetchone()
    bull, bear, first = (r[0] or 0), (r[1] or 0), r[2]
    if not first or bull == bear: continue
    sgn = 1 if bull > bear else -1
    ft = first.replace("T", " ")[:19]
    pt = [b for b in bars if b[0][:19] <= ft]
    p_sig = pt[-1][2] if pt else o
    f30 = [b for b in bars if b[0][11:16] <= "04:10"]  # bars up to ~09:45 IST
    p30 = f30[-1][2] if f30 else o
    pre = sgn * (p_sig - o) / o * 100
    post = sgn * (cl - p_sig) / p_sig * 100
    first30 = sgn * (p30 - o) / o * 100
    total = sgn * (cl - o) / o * 100
    cap = f"{post/total*100:.0f}%" if total > 0.2 else "-"
    print(f"{d} | {'UP' if sgn>0 else 'DOWN'} | {total:+.2f} | {first30:+.2f} | {pre:+.2f} | {post:+.2f} | {ft[11:]} | {cap}")
    tot_pre += pre; tot_post += post; n += 1
if n: print(f"\nDays: {n} | avg pre-signal move {tot_pre/n:+.2f}% | avg post-signal move {tot_post/n:+.2f}% (signed by majority signal direction)")
