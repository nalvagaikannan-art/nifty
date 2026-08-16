import React, { useState, useEffect, useRef, useMemo, useCallback } from "react";

/* ---------------------------------------------------------
   AI MARKET ANALYSIS TERMINAL — prototype, mock data only
   No order placement. Analysis surfaces only.
--------------------------------------------------------- */

const FONT_IMPORT = `@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap');`;

const rnd = (min, max) => Math.random() * (max - min) + min;
const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const fmt = (n, d = 2) => n.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtInt = (n) => Math.round(n).toLocaleString("en-IN");

/* ---------------- mock data engines ---------------- */

function seedIndices() {
  return [
    { key: "nifty", name: "NIFTY 50", value: 24812.35, base: 24812.35, spark: [] },
    { key: "bank", name: "BANK NIFTY", value: 52340.10, base: 52340.10, spark: [] },
    { key: "sensex", name: "SENSEX", value: 81456.70, base: 81456.70, spark: [] },
    { key: "vix", name: "INDIA VIX", value: 13.24, base: 13.24, spark: [], isVix: true },
    { key: "gift", name: "GIFT NIFTY", value: 24865.00, base: 24865.00, spark: [] },
  ].map((i) => ({ ...i, spark: Array.from({ length: 24 }, () => i.base + rnd(-0.3, 0.3) * (i.isVix ? 3 : 40)) }));
}

const SECTORS = ["Bank", "IT", "Auto", "Pharma", "FMCG", "Energy", "Metal", "Realty", "Infra", "PSU Bank"];
function seedSectors() {
  return SECTORS.map((s) => ({ name: s, pct: rnd(-2.4, 2.4) }));
}

const NAMES = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "TATAMOTORS", "AXISBANK", "ITC", "LT",
  "BAJFINANCE", "MARUTI", "SUNPHARMA", "WIPRO", "ADANIENT", "HCLTECH", "ONGC", "NTPC", "TATASTEEL", "JSWSTEEL"];
function seedMovers() {
  const shuffled = [...NAMES].sort(() => Math.random() - 0.5);
  const gainers = shuffled.slice(0, 6).map((s) => ({ symbol: s, price: rnd(200, 4200), pct: rnd(0.8, 6.5) }));
  const losers = shuffled.slice(6, 12).map((s) => ({ symbol: s, price: rnd(200, 4200), pct: -rnd(0.8, 5.2) }));
  return { gainers, losers };
}

function seedCandles(n, base, vol) {
  let last = base;
  const out = [];
  const now = Date.now();
  for (let i = 0; i < n; i++) {
    const o = last;
    const drift = rnd(-1, 1) * vol;
    const c = clamp(o + drift, base * 0.9, base * 1.1);
    const h = Math.max(o, c) + rnd(0, vol * 0.6);
    const l = Math.min(o, c) - rnd(0, vol * 0.6);
    const v = rnd(40000, 320000);
    out.push({ t: now - (n - i) * 60000, o, h, l, c, v });
    last = c;
  }
  return out;
}

function ema(values, period) {
  const k = 2 / (period + 1);
  let prev = values[0];
  return values.map((v, i) => (i === 0 ? v : (prev = v * k + prev * (1 - k))));
}

function rsiSeries(values, period = 14) {
  const out = [];
  let gainSum = 0, lossSum = 0;
  for (let i = 1; i < values.length; i++) {
    const diff = values[i] - values[i - 1];
    const gain = Math.max(diff, 0), loss = Math.max(-diff, 0);
    if (i <= period) { gainSum += gain; lossSum += loss; out.push(50); continue; }
    gainSum = (gainSum * (period - 1) + gain) / period;
    lossSum = (lossSum * (period - 1) + loss) / period;
    const rs = lossSum === 0 ? 100 : gainSum / lossSum;
    out.push(100 - 100 / (1 + rs));
  }
  return [50, ...out];
}

function seedOptionChain(spot) {
  const step = 50;
  const atm = Math.round(spot / step) * step;
  const strikes = Array.from({ length: 13 }, (_, i) => atm - step * 6 + i * step);
  let totalCeOi = 0, totalPeOi = 0, maxPainScore = {};
  const rows = strikes.map((strike) => {
    const dist = Math.abs(strike - spot);
    const ceLtp = Math.max(1, spot - strike + rnd(20, 60) - dist * 0.05);
    const peLtp = Math.max(1, strike - spot + rnd(20, 60) - dist * 0.05);
    const ceOi = Math.round(rnd(80000, 900000) * (1 - dist / 900));
    const peOi = Math.round(rnd(80000, 900000) * (1 - dist / 900));
    totalCeOi += ceOi; totalPeOi += peOi;
    return {
      strike,
      ce: { ltp: Math.max(0.5, ceLtp), oi: Math.max(1000, ceOi), chOi: Math.round(rnd(-60000, 90000)), vol: fmtInt(rnd(5000, 400000)), iv: rnd(11, 22) },
      pe: { ltp: Math.max(0.5, peLtp), oi: Math.max(1000, peOi), chOi: Math.round(rnd(-60000, 90000)), vol: fmtInt(rnd(5000, 400000)), iv: rnd(11, 22) },
    };
  });
  const pcr = totalPeOi / totalCeOi;
  const maxPain = strikes[Math.floor(strikes.length / 2)];
  return { rows, atm, pcr, maxPain, totalCeOi, totalPeOi };
}

const AI_REASON_BANK = [
  "RSI 58-ஐ தாண்டி momentum bullish பக்கம் சாய்கிறது",
  "VWAP-க்கு மேலே தொடர்ந்து trade ஆகிறது — intraday strength",
  "9 EMA, 20 EMA-ஐ cross செய்தது — short-term trend மாற்றம்",
  "ATM strike-ல் Call OI build-up குறைந்து, Put writing அதிகரிக்கிறது",
  "PCR 1.1-க்கு மேல் நகர்கிறது — support base உறுதியாகிறது",
  "Volume சராசரியை விட 1.4x அதிகமாக இருக்கிறது",
  "MACD histogram positive zone-க்கு நகர்கிறது",
  "SuperTrend buy signal-ஐ உறுதிசெய்கிறது",
];

function seedAIDecision() {
  const bullish = Math.random() > 0.42;
  const probability = Math.round(bullish ? rnd(58, 88) : rnd(15, 42));
  const reasons = [...AI_REASON_BANK].sort(() => Math.random() - 0.5).slice(0, 4);
  return {
    trend: bullish ? "Bullish" : "Bearish",
    strength: pick(["Strong", "Moderate", "Building"]),
    momentum: pick(["High", "Medium", "Low"]),
    probability,
    view: bullish ? "Call-க்கு சாதகமான சூழல்" : "Put-க்கு சாதகமான சூழல்",
    risk: pick(["Low", "Medium", "Elevated"]),
    reasons,
    support: Math.round(24812 - rnd(60, 180)),
    resistance: Math.round(24812 + rnd(60, 180)),
    sl: Math.round(24812 - rnd(40, 90)),
    target: Math.round(24812 + rnd(70, 220)),
  };
}

const SCAN_CATS = [
  { key: "breakout", label: "Breakout Stocks" },
  { key: "volume", label: "High Volume Stocks" },
  { key: "oi", label: "OI Build-up" },
  { key: "gap", label: "Gap Up / Gap Down" },
  { key: "hl", label: "New High / New Low" },
];
function seedScanner() {
  const out = {};
  SCAN_CATS.forEach((c) => {
    out[c.key] = [...NAMES].sort(() => Math.random() - 0.5).slice(0, 5).map((s) => ({
      symbol: s, value: rnd(-4, 4), note: pick(["Vol spike", "OI +", "Range break", "New level", "Trend flip"]),
    }));
  });
  return out;
}

const NEWS = [
  { cat: "RBI", time: "09:12", text: "RBI repo rate unchanged at 6.5% — policy stance held neutral" },
  { cat: "Results", time: "08:40", text: "TCS Q1 net profit beats estimates, margin guidance steady" },
  { cat: "FII/DII", time: "08:05", text: "FII net sellers ₹1,240 Cr, DII net buyers ₹1,680 Cr yesterday" },
  { cat: "Economic", time: "07:30", text: "India manufacturing PMI eases slightly to 58.1 in latest reading" },
  { cat: "Results", time: "Yest", text: "HDFC Bank advances grow steady y/y, asset quality stable" },
  { cat: "RBI", time: "Yest", text: "RBI Governor comments on liquidity management ahead of policy" },
];

/* ---------------- small UI atoms ---------------- */

function Pill({ children, tone = "muted" }) {
  const tones = {
    bull: "pill bull", bear: "pill bear", muted: "pill muted", amber: "pill amber", cyan: "pill cyan",
  };
  return <span className={tones[tone]}>{children}</span>;
}

function Sparkline({ data, positive }) {
  const w = 90, h = 28;
  const min = Math.min(...data), max = Math.max(...data);
  const norm = (v) => h - ((v - min) / (max - min || 1)) * h;
  const pts = data.map((v, i) => `${(i / (data.length - 1)) * w},${norm(v)}`).join(" ");
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`}>
      <polyline points={pts} fill="none" stroke={positive ? "var(--bull)" : "var(--bear)"} strokeWidth="1.6" />
    </svg>
  );
}

function Panel({ title, right, children, className = "" }) {
  return (
    <div className={`panel ${className}`}>
      {(title || right) && (
        <div className="flex items-center justify-between panel-head">
          {title && <h3 className="panel-title">{title}</h3>}
          {right}
        </div>
      )}
      {children}
    </div>
  );
}

/* ---------------- candlestick chart ---------------- */

function CandleChart({ candles, showEma, indicatorToggles, width = 760, height = 300 }) {
  const closes = candles.map((c) => c.c);
  const emaLines = {
    9: indicatorToggles.ema9 ? ema(closes, 9) : null,
    20: indicatorToggles.ema20 ? ema(closes, 20) : null,
    50: indicatorToggles.ema50 ? ema(closes, 50) : null,
  };
  const highs = candles.map((c) => c.h), lows = candles.map((c) => c.l);
  const max = Math.max(...highs), min = Math.min(...lows);
  const pad = (max - min) * 0.08;
  const yMax = max + pad, yMin = min - pad;
  const n = candles.length;
  const cw = width / n;
  const y = (v) => height - ((v - yMin) / (yMax - yMin)) * height;

  const vwap = useMemo(() => {
    let cumPV = 0, cumV = 0;
    return candles.map((c) => { cumPV += ((c.h + c.l + c.c) / 3) * c.v; cumV += c.v; return cumPV / cumV; });
  }, [candles]);

  const bbands = useMemo(() => {
    const period = 20, mult = 2;
    return closes.map((_, i) => {
      const slice = closes.slice(Math.max(0, i - period + 1), i + 1);
      const mean = slice.reduce((a, b) => a + b, 0) / slice.length;
      const sd = Math.sqrt(slice.reduce((a, b) => a + (b - mean) ** 2, 0) / slice.length);
      return { mid: mean, upper: mean + mult * sd, lower: mean - mult * sd };
    });
  }, [closes]);

  const linePath = (arr) => arr.map((v, i) => `${i === 0 ? "M" : "L"} ${i * cw + cw / 2} ${y(v)}`).join(" ");

  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} height={height} preserveAspectRatio="none">
      {[0.2, 0.4, 0.6, 0.8].map((f) => (
        <line key={f} x1={0} x2={width} y1={height * f} y2={height * f} stroke="var(--border)" strokeWidth="1" />
      ))}
      {indicatorToggles.bb && (
        <>
          <path d={linePath(bbands.map((b) => b.upper))} stroke="var(--cyan)" strokeOpacity="0.5" strokeWidth="1" fill="none" strokeDasharray="3 3" />
          <path d={linePath(bbands.map((b) => b.lower))} stroke="var(--cyan)" strokeOpacity="0.5" strokeWidth="1" fill="none" strokeDasharray="3 3" />
        </>
      )}
      {candles.map((c, i) => {
        const bull = c.c >= c.o;
        const x = i * cw + cw / 2;
        const bodyTop = y(Math.max(c.o, c.c)), bodyBot = y(Math.min(c.o, c.c));
        return (
          <g key={i}>
            <line x1={x} x2={x} y1={y(c.h)} y2={y(c.l)} stroke={bull ? "var(--bull)" : "var(--bear)"} strokeWidth="1" />
            <rect x={x - cw * 0.32} y={bodyTop} width={cw * 0.64} height={Math.max(1.2, bodyBot - bodyTop)} fill={bull ? "var(--bull)" : "var(--bear)"} />
          </g>
        );
      })}
      {emaLines[9] && <path d={linePath(emaLines[9])} stroke="#F0B429" strokeWidth="1.4" fill="none" />}
      {emaLines[20] && <path d={linePath(emaLines[20])} stroke="#38BDF8" strokeWidth="1.4" fill="none" />}
      {emaLines[50] && <path d={linePath(emaLines[50])} stroke="#C084FC" strokeWidth="1.4" fill="none" />}
      {indicatorToggles.vwap && <path d={linePath(vwap)} stroke="#F87171" strokeWidth="1.2" strokeDasharray="2 2" fill="none" />}
    </svg>
  );
}

function SubIndicatorPanel({ candles, kind, width = 760, height = 90 }) {
  const closes = candles.map((c) => c.c);
  const n = candles.length, cw = width / n;
  if (kind === "rsi") {
    const rsi = rsiSeries(closes);
    const y = (v) => height - (v / 100) * height;
    const path = rsi.map((v, i) => `${i === 0 ? "M" : "L"} ${i * cw} ${y(v)}`).join(" ");
    return (
      <svg width="100%" viewBox={`0 0 ${width} ${height}`} height={height} preserveAspectRatio="none">
        <line x1={0} x2={width} y1={y(70)} y2={y(70)} stroke="var(--bear)" strokeOpacity="0.4" strokeDasharray="3 3" />
        <line x1={0} x2={width} y1={y(30)} y2={y(30)} stroke="var(--bull)" strokeOpacity="0.4" strokeDasharray="3 3" />
        <path d={path} stroke="var(--cyan)" strokeWidth="1.4" fill="none" />
      </svg>
    );
  }
  // MACD
  const emaFast = ema(closes, 12), emaSlow = ema(closes, 26);
  const macd = emaFast.map((v, i) => v - emaSlow[i]);
  const signal = ema(macd, 9);
  const hist = macd.map((v, i) => v - signal[i]);
  const all = [...macd, ...signal, ...hist];
  const max = Math.max(...all), min = Math.min(...all);
  const y = (v) => height - ((v - min) / (max - min || 1)) * height;
  const path = (arr, color) => <path d={arr.map((v, i) => `${i === 0 ? "M" : "L"} ${i * cw} ${y(v)}`).join(" ")} stroke={color} strokeWidth="1.3" fill="none" />;
  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} height={height} preserveAspectRatio="none">
      {hist.map((v, i) => (
        <rect key={i} x={i * cw} width={Math.max(1, cw * 0.6)} y={v >= 0 ? y(v) : y(0)} height={Math.abs(y(v) - y(0))} fill={v >= 0 ? "var(--bull)" : "var(--bear)"} opacity="0.55" />
      ))}
      {path(macd, "#38BDF8")}
      {path(signal, "#F0B429")}
    </svg>
  );
}

/* ---------------- AI probability gauge (signature element) ---------------- */

function ProbabilityGauge({ probability, bullish }) {
  const size = 168, stroke = 12, r = (size - stroke) / 2, c = r * Math.PI; // half circle
  const offset = c - (probability / 100) * c;
  const color = bullish ? "var(--bull)" : "var(--bear)";
  return (
    <div className="gauge-wrap">
      <svg width={size} height={size / 1.65} viewBox={`0 0 ${size} ${size / 2 + stroke}`}>
        <path d={`M ${stroke / 2} ${size / 2} A ${r} ${r} 0 0 1 ${size - stroke / 2} ${size / 2}`} fill="none" stroke="var(--border)" strokeWidth={stroke} strokeLinecap="round" />
        <path d={`M ${stroke / 2} ${size / 2} A ${r} ${r} 0 0 1 ${size - stroke / 2} ${size / 2}`} fill="none" stroke={color} strokeWidth={stroke} strokeLinecap="round" strokeDasharray={c} strokeDashoffset={offset} style={{ transition: "stroke-dashoffset .6s ease" }} />
      </svg>
      <div className="gauge-value" style={{ color }}>{probability}%</div>
      <div className="gauge-label">AI Probability</div>
    </div>
  );
}

/* ---------------- main tabs ---------------- */

function DashboardTab({ indices, sectors, movers, aiDecision, candles }) {
  return (
    <div className="grid gap-4" style={{ gridTemplateColumns: "1.3fr 1fr" }}>
      <div className="flex flex-col gap-4">
        <Panel title="NIFTY 50 · 5m" right={<Pill tone={candles[candles.length - 1].c >= candles[0].o ? "bull" : "bear"}>Intraday</Pill>}>
          <CandleChart candles={candles} indicatorToggles={{ ema9: true, ema20: true }} height={220} />
        </Panel>
        <Panel title="Sector-wise Performance">
          <div className="sector-grid">
            {sectors.map((s) => (
              <div key={s.name} className="sector-cell" style={{ background: s.pct >= 0 ? `rgba(52,211,153,${clamp(Math.abs(s.pct) / 3, 0.12, 0.55)})` : `rgba(248,113,113,${clamp(Math.abs(s.pct) / 3, 0.12, 0.55)})` }}>
                <div className="sector-name">{s.name}</div>
                <div className={`sector-pct ${s.pct >= 0 ? "up" : "down"}`}>{s.pct >= 0 ? "+" : ""}{fmt(s.pct)}%</div>
              </div>
            ))}
          </div>
        </Panel>
        <div className="grid gap-4" style={{ gridTemplateColumns: "1fr 1fr" }}>
          <Panel title="Top Gainers">
            {movers.gainers.map((m) => (
              <div key={m.symbol} className="mover-row">
                <span className="mono">{m.symbol}</span>
                <span className="mono">₹{fmt(m.price)}</span>
                <span className="mono up">+{fmt(m.pct)}%</span>
              </div>
            ))}
          </Panel>
          <Panel title="Top Losers">
            {movers.losers.map((m) => (
              <div key={m.symbol} className="mover-row">
                <span className="mono">{m.symbol}</span>
                <span className="mono">₹{fmt(m.price)}</span>
                <span className="mono down">{fmt(m.pct)}%</span>
              </div>
            ))}
          </Panel>
        </div>
      </div>

      <div className="flex flex-col gap-4">
        <Panel title="AI Decision Panel" className="ai-panel">
          <div className="flex items-center justify-between">
            <ProbabilityGauge probability={aiDecision.probability} bullish={aiDecision.trend === "Bullish"} />
            <div className="flex flex-col gap-2 items-end">
              <Pill tone={aiDecision.trend === "Bullish" ? "bull" : "bear"}>Trend: {aiDecision.trend}</Pill>
              <Pill tone="amber">Strength: {aiDecision.strength}</Pill>
              <Pill tone="cyan">Momentum: {aiDecision.momentum}</Pill>
              <Pill tone="muted">Risk: {aiDecision.risk}</Pill>
            </div>
          </div>
          <div className="ai-view">Suggested View: <b>{aiDecision.view}</b></div>
          <div className="ai-levels">
            <div><span className="muted">Support</span><span className="mono">{fmtInt(aiDecision.support)}</span></div>
            <div><span className="muted">Resistance</span><span className="mono">{fmtInt(aiDecision.resistance)}</span></div>
            <div><span className="muted">Stop Loss*</span><span className="mono down">{fmtInt(aiDecision.sl)}</span></div>
            <div><span className="muted">Target*</span><span className="mono up">{fmtInt(aiDecision.target)}</span></div>
          </div>
          <div className="ai-reasons">
            <div className="muted" style={{ marginBottom: 6 }}>காரணங்கள் (Reasoning)</div>
            <ul>{aiDecision.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
          </div>
          <div className="disclaimer">*Analysis மட்டும் — இது trade recommendation அல்ல, order place செய்யப்படாது.</div>
        </Panel>
        <Panel title="India VIX & GIFT Nifty">
          {indices.filter((i) => i.key === "vix" || i.key === "gift").map((i) => (
            <div key={i.key} className="mover-row">
              <span>{i.name}</span>
              <span className="mono">{fmt(i.value)}</span>
              <Sparkline data={i.spark} positive={i.value >= i.base} />
            </div>
          ))}
        </Panel>
      </div>
    </div>
  );
}

function OptionChainTab({ chain, spot }) {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex gap-4">
        <Panel className="flex-1"><div className="stat-label">Spot (NIFTY)</div><div className="stat-value mono">{fmt(spot, 2)}</div></Panel>
        <Panel className="flex-1"><div className="stat-label">PCR</div><div className={`stat-value mono ${chain.pcr >= 1 ? "up" : "down"}`}>{fmt(chain.pcr)}</div></Panel>
        <Panel className="flex-1"><div className="stat-label">Max Pain</div><div className="stat-value mono">{chain.maxPain}</div></Panel>
        <Panel className="flex-1"><div className="stat-label">ATM Strike</div><div className="stat-value mono">{chain.atm}</div></Panel>
      </div>
      <Panel title="Live Option Chain">
        <div className="oc-table-wrap">
          <table className="oc-table">
            <thead>
              <tr>
                <th colSpan={5} className="ce-head">CALLS</th>
                <th className="strike-head">STRIKE</th>
                <th colSpan={5} className="pe-head">PUTS</th>
              </tr>
              <tr>
                <th>OI Δ</th><th>OI</th><th>Vol</th><th>IV</th><th>LTP</th>
                <th className="strike-col">·</th>
                <th>LTP</th><th>IV</th><th>Vol</th><th>OI</th><th>OI Δ</th>
              </tr>
            </thead>
            <tbody>
              {chain.rows.map((r) => (
                <tr key={r.strike} className={r.strike === chain.atm ? "atm-row" : ""}>
                  <td className={`mono ${r.ce.chOi >= 0 ? "up" : "down"}`}>{fmtInt(r.ce.chOi)}</td>
                  <td className="mono">{fmtInt(r.ce.oi)}</td>
                  <td className="mono">{r.ce.vol}</td>
                  <td className="mono">{fmt(r.ce.iv, 1)}</td>
                  <td className="mono bold">{fmt(r.ce.ltp)}</td>
                  <td className="strike-col mono">{r.strike}</td>
                  <td className="mono bold">{fmt(r.pe.ltp)}</td>
                  <td className="mono">{fmt(r.pe.iv, 1)}</td>
                  <td className="mono">{r.pe.vol}</td>
                  <td className="mono">{fmtInt(r.pe.oi)}</td>
                  <td className={`mono ${r.pe.chOi >= 0 ? "up" : "down"}`}>{fmtInt(r.pe.chOi)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel title="OI Build-up Analysis">
        <div className="grid gap-3" style={{ gridTemplateColumns: "1fr 1fr" }}>
          <div className="buildup-card bull-card"><div className="buildup-title">Long Build-up</div><div className="muted">Price ↑ + OI ↑ — {chain.rows[7].strike} CE, {chain.rows[5].strike} CE</div></div>
          <div className="buildup-card bear-card"><div className="buildup-title">Short Build-up</div><div className="muted">Price ↓ + OI ↑ — {chain.rows[3].strike} PE, {chain.rows[9].strike} PE</div></div>
        </div>
      </Panel>
    </div>
  );
}

const TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1H", "1D"];
function ChartsTab({ candles, setTf, tf, toggles, setToggles }) {
  const toggle = (k) => setToggles((t) => ({ ...t, [k]: !t[k] }));
  return (
    <div className="flex flex-col gap-4">
      <Panel>
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div className="tf-row">
            {TIMEFRAMES.map((t) => (
              <button key={t} className={`tf-btn ${tf === t ? "active" : ""}`} onClick={() => setTf(t)}>{t}</button>
            ))}
          </div>
          <div className="ind-row">
            {[["ema9", "EMA 9"], ["ema20", "EMA 20"], ["ema50", "EMA 50"], ["vwap", "VWAP"], ["bb", "Bollinger"]].map(([k, l]) => (
              <label key={k} className={`ind-chip ${toggles[k] ? "on" : ""}`}>
                <input type="checkbox" checked={!!toggles[k]} onChange={() => toggle(k)} /> {l}
              </label>
            ))}
          </div>
        </div>
        <CandleChart candles={candles} indicatorToggles={toggles} height={320} />
        <div className="vol-bars">
          {candles.map((c, i) => {
            const maxV = Math.max(...candles.map((x) => x.v));
            return <div key={i} className="vol-bar" style={{ height: `${(c.v / maxV) * 100}%`, background: c.c >= c.o ? "var(--bull)" : "var(--bear)" }} />;
          })}
        </div>
      </Panel>
      <div className="grid gap-4" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <Panel title="RSI (14)"><SubIndicatorPanel candles={candles} kind="rsi" /></Panel>
        <Panel title="MACD (12,26,9)"><SubIndicatorPanel candles={candles} kind="macd" /></Panel>
      </div>
      <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
        {["ADX", "ATR", "SuperTrend", "VWAP"].map((k) => (
          <Panel key={k}><div className="stat-label">{k}</div><div className="stat-value mono">{fmt(rnd(k === "ADX" ? 15 : k === "ATR" ? 20 : 24700, k === "ADX" ? 45 : k === "ATR" ? 90 : 24950), k === "SuperTrend" || k === "VWAP" ? 1 : 1)}</div></Panel>
        ))}
      </div>
    </div>
  );
}

function ScannerTab({ scanner }) {
  return (
    <div className="grid gap-4" style={{ gridTemplateColumns: "1fr 1fr" }}>
      {SCAN_CATS.map((c) => (
        <Panel key={c.key} title={c.label}>
          {scanner[c.key].map((r) => (
            <div key={r.symbol} className="mover-row">
              <span className="mono">{r.symbol}</span>
              <span className={`mono ${r.value >= 0 ? "up" : "down"}`}>{r.value >= 0 ? "+" : ""}{fmt(r.value)}%</span>
              <Pill tone="muted">{r.note}</Pill>
            </div>
          ))}
        </Panel>
      ))}
    </div>
  );
}

function WatchlistTab({ watchlist, indices }) {
  return (
    <div className="grid gap-4" style={{ gridTemplateColumns: "1fr 1fr" }}>
      <Panel title="Index Watchlist">
        {indices.map((i) => (
          <div key={i.key} className="mover-row">
            <span>{i.name}</span>
            <span className="mono">{fmt(i.value)}</span>
            <span className={`mono ${i.value >= i.base ? "up" : "down"}`}>{i.value >= i.base ? "+" : ""}{fmt(((i.value - i.base) / i.base) * 100)}%</span>
          </div>
        ))}
      </Panel>
      <Panel title="Stock & Option Watchlist">
        {watchlist.map((w) => (
          <div key={w.symbol} className="mover-row">
            <span className="mono">{w.symbol}</span>
            <span className="mono">₹{fmt(w.price)}</span>
            <span className={`mono ${w.pct >= 0 ? "up" : "down"}`}>{w.pct >= 0 ? "+" : ""}{fmt(w.pct)}%</span>
          </div>
        ))}
      </Panel>
    </div>
  );
}

function NewsTab() {
  return (
    <Panel title="News & Events">
      {NEWS.map((n, i) => (
        <div key={i} className="news-row">
          <Pill tone="cyan">{n.cat}</Pill>
          <span className="news-text">{n.text}</span>
          <span className="muted mono">{n.time}</span>
        </div>
      ))}
    </Panel>
  );
}

function ReportsTab({ aiDecision }) {
  return (
    <div className="grid gap-4" style={{ gridTemplateColumns: "1fr 1fr" }}>
      <Panel title="தினசரி Market Summary">
        <p className="report-text">NIFTY 50 இன்று {aiDecision.trend === "Bullish" ? "positive" : "negative"} bias-ஓடு trade ஆனது. Support {fmtInt(aiDecision.support)}, Resistance {fmtInt(aiDecision.resistance)} அளவில் இருந்தது. Volume சராசரியை ஒட்டி இருந்தது.</p>
      </Panel>
      <Panel title="Weekly Analysis">
        <p className="report-text">இந்த வாரம் index range-bound ஆக நகர்ந்து, sector rotation Bank &amp; IT இடையே காணப்பட்டது. FII flows நிலையற்று இருந்தது.</p>
      </Panel>
      <Panel title="Monthly Trend">
        <p className="report-text">Broader trend medium-term EMA-க்கு மேல் தொடர்கிறது. Volatility (VIX) low-to-moderate zone-ல் உள்ளது.</p>
      </Panel>
      <Panel title="AI Observation Report">
        <p className="report-text">AI model current probability {aiDecision.probability}% — {aiDecision.view}. Reasoning: {aiDecision.reasons.slice(0, 2).join("; ")}.</p>
      </Panel>
    </div>
  );
}

/* ---------------- app shell ---------------- */

const TABS = [
  ["dashboard", "Dashboard"], ["options", "Option Chain"], ["charts", "Charts"],
  ["scanner", "Scanner"], ["watchlist", "Watchlist"], ["news", "News"], ["reports", "Reports"],
];

export default function App() {
  const [tab, setTab] = useState("dashboard");
  const [indices, setIndices] = useState(seedIndices);
  const [sectors, setSectors] = useState(seedSectors);
  const [movers, setMovers] = useState(seedMovers);
  const [aiDecision, setAiDecision] = useState(seedAIDecision);
  const [scanner, setScanner] = useState(seedScanner);
  const [watchlist] = useState(() => seedMovers().gainers.concat(seedMovers().losers).slice(0, 6).map((m) => ({ symbol: m.symbol, price: m.price, pct: m.pct })));
  const [tf, setTf] = useState("5m");
  const [candles, setCandles] = useState(() => seedCandles(60, 24812, 22));
  const [toggles, setToggles] = useState({ ema9: true, ema20: true, ema50: false, vwap: true, bb: false });
  const [clock, setClock] = useState(new Date());

  useEffect(() => { setCandles(seedCandles(60, 24812, tf === "1D" ? 220 : tf === "1H" || tf === "30m" ? 70 : 22)); }, [tf]);

  useEffect(() => {
    const id = setInterval(() => {
      setClock(new Date());
      setIndices((prev) => prev.map((i) => {
        const nv = i.value + rnd(-1, 1) * (i.isVix ? 0.08 : i.key === "sensex" ? 12 : 6);
        return { ...i, value: nv, spark: [...i.spark.slice(1), nv] };
      }));
    }, 2200);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const id = setInterval(() => {
      setAiDecision(seedAIDecision());
      setSectors(seedSectors());
    }, 15000);
    return () => clearInterval(id);
  }, []);

  const chain = useMemo(() => seedOptionChain(indices[0].value), [Math.round(indices[0].value / 10)]);

  return (
    <div className="app">
      <style>{`
        ${FONT_IMPORT}
        :root{
          --bg:#0B0F16; --panel:#121826; --panel-alt:#171F2E; --border:#232C3D;
          --text:#E7EBF3; --muted:#7C8798; --bull:#34D399; --bear:#F87171; --amber:#F0B429; --cyan:#38BDF8;
        }
        .app{ background:var(--bg); color:var(--text); min-height:100vh; font-family:'Inter',sans-serif; }
        .mono{ font-family:'IBM Plex Mono',monospace; }
        .bold{ font-weight:600; }
        .muted{ color:var(--muted); }
        .up{ color:var(--bull); } .down{ color:var(--bear); }

        .topbar{ display:flex; align-items:center; justify-content:space-between; padding:14px 22px; border-bottom:1px solid var(--border); background:var(--panel); position:sticky; top:0; z-index:10; }
        .brand{ font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:18px; letter-spacing:0.5px; display:flex; align-items:center; gap:10px; }
        .brand .dot{ width:8px; height:8px; border-radius:50%; background:var(--bull); box-shadow:0 0 8px var(--bull); animation:pulse 1.8s infinite; }
        @keyframes pulse{ 0%,100%{opacity:1;} 50%{opacity:0.35;} }
        .clock{ font-family:'IBM Plex Mono',monospace; color:var(--muted); font-size:13px; }

        .ticker-strip{ display:flex; gap:0; overflow-x:auto; border-bottom:1px solid var(--border); background:var(--panel-alt); }
        .ticker-item{ padding:10px 20px; border-right:1px solid var(--border); min-width:150px; }
        .ticker-name{ font-size:11px; color:var(--muted); letter-spacing:0.4px; }
        .ticker-value{ font-family:'IBM Plex Mono',monospace; font-size:16px; font-weight:600; }
        .ticker-chg{ font-family:'IBM Plex Mono',monospace; font-size:12px; }

        .tabs{ display:flex; gap:4px; padding:10px 22px; border-bottom:1px solid var(--border); background:var(--panel); overflow-x:auto; }
        .tab-btn{ font-family:'Space Grotesk',sans-serif; font-size:13px; padding:8px 14px; border-radius:8px; border:1px solid transparent; background:transparent; color:var(--muted); cursor:pointer; white-space:nowrap; }
        .tab-btn.active{ background:var(--panel-alt); color:var(--text); border-color:var(--border); }

        .content{ padding:20px 22px 60px; max-width:1360px; margin:0 auto; }

        .panel{ background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:16px; }
        .panel-head{ margin-bottom:12px; }
        .panel-title{ font-family:'Space Grotesk',sans-serif; font-size:13px; letter-spacing:0.3px; color:var(--muted); text-transform:uppercase; }

        .pill{ font-size:11px; padding:3px 9px; border-radius:20px; border:1px solid var(--border); }
        .pill.bull{ color:var(--bull); border-color:rgba(52,211,153,0.4); background:rgba(52,211,153,0.08); }
        .pill.bear{ color:var(--bear); border-color:rgba(248,113,113,0.4); background:rgba(248,113,113,0.08); }
        .pill.amber{ color:var(--amber); border-color:rgba(240,180,41,0.4); background:rgba(240,180,41,0.08); }
        .pill.cyan{ color:var(--cyan); border-color:rgba(56,189,248,0.4); background:rgba(56,189,248,0.08); }
        .pill.muted{ color:var(--muted); }

        .sector-grid{ display:grid; grid-template-columns:repeat(5,1fr); gap:8px; }
        .sector-cell{ border-radius:8px; padding:10px; }
        .sector-name{ font-size:11px; color:var(--muted); }
        .sector-pct{ font-family:'IBM Plex Mono',monospace; font-weight:600; font-size:14px; }

        .mover-row{ display:flex; align-items:center; justify-content:space-between; padding:7px 0; border-bottom:1px solid var(--border); font-size:13px; }
        .mover-row:last-child{ border-bottom:none; }

        .ai-panel{ background:linear-gradient(180deg, var(--panel), var(--panel-alt)); }
        .gauge-wrap{ position:relative; text-align:center; }
        .gauge-value{ font-family:'Space Grotesk',sans-serif; font-size:26px; font-weight:700; margin-top:-14px; }
        .gauge-label{ font-size:11px; color:var(--muted); }
        .ai-view{ margin-top:14px; font-size:14px; }
        .ai-levels{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-top:14px; }
        .ai-levels > div{ display:flex; flex-direction:column; gap:2px; background:var(--panel-alt); border:1px solid var(--border); border-radius:8px; padding:8px; }
        .ai-levels .muted{ font-size:10px; }
        .ai-reasons ul{ margin:0; padding-left:16px; font-size:12.5px; line-height:1.7; color:#C8D0DE; }
        .disclaimer{ margin-top:12px; font-size:11px; color:var(--muted); border-top:1px solid var(--border); padding-top:10px; }

        .oc-table-wrap{ overflow-x:auto; }
        .oc-table{ width:100%; border-collapse:collapse; font-size:12.5px; min-width:820px; }
        .oc-table th, .oc-table td{ padding:6px 8px; text-align:center; border-bottom:1px solid var(--border); }
        .ce-head{ color:var(--bull); } .pe-head{ color:var(--bear); }
        .strike-head, .strike-col{ color:var(--amber); font-weight:600; }
        .atm-row{ background:rgba(240,180,41,0.08); }

        .buildup-card{ border-radius:10px; padding:12px; border:1px solid var(--border); }
        .bull-card{ background:rgba(52,211,153,0.06); }
        .bear-card{ background:rgba(248,113,113,0.06); }
        .buildup-title{ font-weight:600; margin-bottom:4px; }

        .tf-row, .ind-row{ display:flex; gap:6px; flex-wrap:wrap; }
        .tf-btn{ font-family:'IBM Plex Mono',monospace; font-size:12px; padding:5px 10px; border-radius:6px; border:1px solid var(--border); background:var(--panel-alt); color:var(--muted); cursor:pointer; }
        .tf-btn.active{ color:var(--text); border-color:var(--cyan); }
        .ind-chip{ font-size:11px; display:flex; align-items:center; gap:4px; padding:5px 8px; border-radius:6px; border:1px solid var(--border); color:var(--muted); cursor:pointer; }
        .ind-chip.on{ color:var(--text); border-color:var(--amber); }
        .vol-bars{ display:flex; align-items:flex-end; height:60px; gap:1px; margin-top:8px; }
        .vol-bar{ flex:1; opacity:0.7; }

        .stat-label{ font-size:11px; color:var(--muted); text-transform:uppercase; }
        .stat-value{ font-size:20px; font-weight:600; margin-top:4px; }

        .news-row{ display:flex; align-items:center; gap:12px; padding:9px 0; border-bottom:1px solid var(--border); font-size:13px; }
        .news-text{ flex:1; }

        .report-text{ font-size:13px; line-height:1.7; color:#C8D0DE; }

        .footer-note{ text-align:center; color:var(--muted); font-size:11px; padding:18px; }
      `}</style>

      <div className="topbar">
        <div className="brand"><span className="dot" /> AI MARKET TERMINAL <Pill tone="amber">Analysis Only</Pill></div>
        <div className="clock">{clock.toLocaleTimeString("en-IN")} IST</div>
      </div>

      <div className="ticker-strip">
        {indices.map((i) => {
          const pct = ((i.value - i.base) / i.base) * 100;
          return (
            <div key={i.key} className="ticker-item">
              <div className="ticker-name">{i.name}</div>
              <div className="ticker-value">{fmt(i.value)}</div>
              <div className={`ticker-chg ${pct >= 0 ? "up" : "down"}`}>{pct >= 0 ? "▲" : "▼"} {fmt(Math.abs(pct))}%</div>
            </div>
          );
        })}
      </div>

      <div className="tabs">
        {TABS.map(([k, l]) => (
          <button key={k} className={`tab-btn ${tab === k ? "active" : ""}`} onClick={() => setTab(k)}>{l}</button>
        ))}
      </div>

      <div className="content">
        {tab === "dashboard" && <DashboardTab indices={indices} sectors={sectors} movers={movers} aiDecision={aiDecision} candles={candles} />}
        {tab === "options" && <OptionChainTab chain={chain} spot={indices[0].value} />}
        {tab === "charts" && <ChartsTab candles={candles} tf={tf} setTf={setTf} toggles={toggles} setToggles={setToggles} />}
        {tab === "scanner" && <ScannerTab scanner={scanner} />}
        {tab === "watchlist" && <WatchlistTab watchlist={watchlist} indices={indices} />}
        {tab === "news" && <NewsTab />}
        {tab === "reports" && <ReportsTab aiDecision={aiDecision} />}
        <div className="footer-note">Prototype · Simulated data only · No Buy/Sell order placement · Live API integration தேவைப்படும் போது broker/data-vendor connect செய்யலாம்</div>
      </div>
    </div>
  );
}
