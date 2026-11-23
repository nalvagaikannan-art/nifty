import sqlite3, sys, os
db = sys.argv[1] if len(sys.argv) > 1 else "./data/analyzer.db"
if not os.path.exists(db):
    sys.exit(f"DB not found: {db}")
c = sqlite3.connect(f"file:{os.path.abspath(db)}?mode=ro", uri=True)  # read-only
q = lambda sql: c.execute(sql).fetchall()
def show(title, rows, hdr):
    print(f"\n== {title} ==")
    print(" | ".join(hdr))
    for r in rows: print(" | ".join("-" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v)) for v in r))
    if not rows: print("(empty)")

show("Ledger signals per day/action", q("""
  SELECT date(timestamp), action, COUNT(*), ROUND(AVG(signal_strength),1)
  FROM daily_signal_ledger GROUP BY 1,2 ORDER BY 1 DESC LIMIT 30"""),
  ["day","action","signals","avg_strength"])

show("Forward outcomes by horizon/status", q("""
  SELECT horizon_minutes, status, COUNT(*) FROM forward_paper_outcomes
  GROUP BY 1,2 ORDER BY 1,3 DESC"""), ["horizon_min","status","rows"])

show("Why not graded (top reasons)", q("""
  SELECT status, substr(COALESCE(status_reason,''),1,70), COUNT(*)
  FROM forward_paper_outcomes WHERE status NOT IN ('matured','pending')
  GROUP BY 1,2 ORDER BY 3 DESC LIMIT 12"""), ["status","reason","rows"])

show("Matured results by horizon (spot direction hit-rate, avg returns)", q("""
  SELECT horizon_minutes, COUNT(*),
         ROUND(100.0*SUM(CASE WHEN spot_directional_return_pct>0 THEN 1 ELSE 0 END)/COUNT(*),1),
         ROUND(AVG(spot_directional_return_pct),3),
         ROUND(AVG(premium_action_adjusted_return_pct),2),
         ROUND(AVG(execution_aware_return_pct),2)
  FROM forward_paper_outcomes
  WHERE status IN ('matured','matured_partial') AND spot_directional_return_pct IS NOT NULL
  GROUP BY 1 ORDER BY 1"""),
  ["horizon_min","n","spot_hit_%","avg_spot_dir_%","avg_premium_%","avg_exec_aware_%"])

show("By side (CALL vs PUT), matured", q("""
  SELECT l.action, o.horizon_minutes, COUNT(*),
         ROUND(100.0*SUM(CASE WHEN o.spot_directional_return_pct>0 THEN 1 ELSE 0 END)/COUNT(*),1),
         ROUND(AVG(o.execution_aware_return_pct),2)
  FROM forward_paper_outcomes o JOIN daily_signal_ledger l ON l.id=o.ledger_id
  WHERE o.status IN ('matured','matured_partial') AND o.spot_directional_return_pct IS NOT NULL
  GROUP BY 1,2 ORDER BY 1,2"""), ["action","horizon_min","n","spot_hit_%","avg_exec_aware_%"])

show("Signals by hour (IST as stored)", q("""
  SELECT strftime('%H',timestamp), COUNT(*) FROM daily_signal_ledger GROUP BY 1 ORDER BY 1"""),
  ["hour","signals"])
