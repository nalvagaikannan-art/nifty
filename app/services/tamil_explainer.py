"""
Tamil Indicator Explainer
=========================
decision_engine.py-ன் அதே 20 conditions-க்கும் detailed Tamil விளக்கம்
தரும் module. Threshold logic decision_engine.py-ஓடு exact-ஆ match
ஆகணும் — இல்லனா Tamil விளக்கம் ஒரு direction சொல்லும், rule-engine score
வேற direction காட்டும்-ன்னு mismatch வரும். அதனால எந்த threshold மாறினாலும்
இரண்டு இடத்திலயும் மாத்தணும்.

Output: build_tamil_indicators(market_data, decision) → List[Dict]
Each item: {
    id, icon, title_ta, value_display,
    direction ("bull"|"bear"|"neutral"),
    direction_label_ta, explanation_ta
}
"""
from typing import Dict, List


DIR_LABEL = {
    "bull":    "📈 ஏறும் அழுத்தம் (Bullish)",
    "bear":    "📉 இறங்கும் அழுத்தம் (Bearish)",
    "neutral": "➖ நடுநிலை (Neutral)",
}


def _item(id_, icon, title, value, direction, explanation):
    return {
        "id": id_,
        "icon": icon,
        "title_ta": title,
        "value_display": value,
        "direction": direction,
        "direction_label_ta": DIR_LABEL[direction],
        "explanation_ta": explanation,
    }


def build_tamil_indicators(market_data: Dict, decision: Dict) -> List[Dict]:
    spot_data  = market_data.get("spot", {})
    spot       = spot_data.get("price", 0)
    technicals = market_data.get("technicals", {})
    oi_summary = market_data.get("oi_summary", {})
    oi_change  = market_data.get("oi_change", {})
    macd       = market_data.get("macd", {})

    pcr             = market_data.get("pcr", 0)
    max_pain        = market_data.get("max_pain", 0)
    futures_premium = market_data.get("futures_premium", 0)
    rsi             = market_data.get("rsi", 50)
    vix             = market_data.get("vix", 15)
    global_pct      = market_data.get("global_change_pct", 0)
    global_status   = market_data.get("global_status", "unavailable")
    gift_pct        = market_data.get("gift_nifty_change_pct", 0)
    gift_status     = market_data.get("gift_status", "unavailable")
    fii_cr          = market_data.get("fii_net_cr", 0)
    fii_status      = market_data.get("fii_status", "unavailable")
    dii_cr          = market_data.get("dii_net_cr", 0)
    dii_status      = market_data.get("dii_status", "unavailable")
    futures_status  = market_data.get("futures_premium_status", "unavailable")

    ema20      = technicals.get("ema20", 0)
    ema50      = technicals.get("ema50", 0)
    vwap       = technicals.get("vwap", 0)
    adx        = technicals.get("adx", 0)
    di_plus    = technicals.get("di_plus", 0)
    di_minus   = technicals.get("di_minus", 0)
    atr        = technicals.get("atr", 0)
    supertrend = (technicals.get("supertrend") or "").lower()
    vol_spike  = technicals.get("volume_spike", False)
    vol_ratio  = technicals.get("volume_ratio", 1.0)

    out: List[Dict] = []

    # 1. PCR — Put-Call Ratio
    # FIX: pcr<=0 guard இல்லாம இருந்துச்சு — option chain fetch fail ஆனா
    # pcr=0.0 "Strong call writing, bearish"-ன்னு fabricate ஆகிடும்
    # (decision_engine.py-ல் இதே bug fix பண்ணிருக்கேன், இங்க mirror பண்றேன்).
    if pcr <= 0:
        d, ex = "neutral", "PCR இப்போ கிடைக்கல் (option chain data missing) — 0-ஐ 'strong call writing'-ன்னு எடுத்துக்கக் கூடாது."
    elif pcr >= 1.3:
        d, ex = "bull", (f"PCR {pcr:.2f} — 1.3-க்கு மேல இருக்கு. இதுன்னா Put options-ஐ "
                          f"அதிகமா எழுதி (sell) இருக்காங்க, அதாவது traders கீழே போகாது-ன்னு "
                          f"நம்பி இருக்காங்க. இது பொதுவா bullish signal.")
    elif pcr >= 1.1:
        d, ex = "bull", f"PCR {pcr:.2f} — லேசான Put writing இருக்கு, mild bullish lean."
    elif pcr <= 0.7:
        d, ex = "bear", (f"PCR {pcr:.2f} — 0.7-க்கு கீழ இருக்கு. Call options அதிகமா "
                          f"எழுதி இருக்காங்க, அதாவது traders மேலே போகாது-ன்னு நினைக்காங்க. "
                          f"இது பொதுவா bearish signal.")
    elif pcr <= 0.9:
        d, ex = "bear", f"PCR {pcr:.2f} — லேசான Call writing இருக்கு, mild bearish lean."
    else:
        d, ex = "neutral", f"PCR {pcr:.2f} — 0.9 முதல் 1.1 வரைக்கும் neutral zone-ல இருக்கு, தெளிவான bias இல்ல."
    out.append(_item("pcr", "⚖️", "புட்-கால் விகிதம் (PCR)", f"{pcr:.3f}" if pcr > 0 else "N/A", d, ex))

    # 2. OI Change
    ce_chg = oi_change.get("ce_change", 0)
    pe_chg = oi_change.get("pe_change", 0)
    if pe_chg > ce_chg and pe_chg > 0:
        d = "bull"
        ex = (f"இன்னிக்கு Put side-ல Open Interest (+{pe_chg:,}) Call side-ஐ விட அதிகமா "
              f"கூடி இருக்கு. அதாவது support level-ல bulls confidence-ஆ position போட்டு "
              f"இருக்காங்க — bullish signal.")
    elif ce_chg > pe_chg and ce_chg > 0:
        d = "bear"
        ex = (f"இன்னிக்கு Call side-ல Open Interest (+{ce_chg:,}) Put side-ஐ விட அதிகமா "
              f"கூடி இருக்கு. அதாவது resistance level-ல bears confidence-ஆ position போட்டு "
              f"இருக்காங்க — bearish signal.")
    else:
        d, ex = "neutral", "CE/PE Open Interest change இரண்டு பக்கமும் சமமா இருக்கு, தெளிவான திசை இல்ல."
    out.append(_item("oi_change", "📊", "ஓபன் இன்ட்ரெஸ்ட் மாற்றம் (OI Change)",
                      f"CE {ce_chg:+,} / PE {pe_chg:+,}", d, ex))

    # 3. Max Pain
    if max_pain > 0:
        diff_pct = ((spot - max_pain) / max_pain) * 100
        if diff_pct > 1.5:
            d = "bear"
            ex = (f"தற்போதைய விலை ({spot:,.0f}) Max Pain level-ஐ ({max_pain:,.0f}) விட "
                  f"{diff_pct:.1f}% அதிகமா இருக்கு. Expiry நாள் அருகில வர வர, விலை "
                  f"Max Pain level பக்கம் இழுக்கப்படும்-ன்னு option-sellers நம்பிக்கை — "
                  f"அதனால கீழ இழுக்கும் அழுத்தம் இருக்கலாம்.")
        elif diff_pct < -1.5:
            d = "bull"
            ex = (f"தற்போதைய விலை ({spot:,.0f}) Max Pain level-ஐ ({max_pain:,.0f}) விட "
                  f"{abs(diff_pct):.1f}% குறைவா இருக்கு. Expiry நெருங்க நெருங்க, விலை Max "
                  f"Pain பக்கம் மேலே இழுக்கப்படலாம் — bullish pull.")
        else:
            d = "neutral"
            ex = f"தற்போதைய விலை Max Pain level ({max_pain:,.0f}) அருகிலேயே இருக்கு, தெளிவான gravity pull இல்ல."
    else:
        d, ex = "neutral", "Max Pain data இப்போ கிடைக்கல."
    out.append(_item("max_pain", "🎯", "மேக்ஸ் பெயின் (Max Pain)",
                      f"{max_pain:,.0f}" if max_pain else "--", d, ex))

    # 4. Call Writing
    ce_top_oi = oi_summary.get("ce_max_oi_strike", 0)
    if ce_top_oi and spot:
        dist = ce_top_oi - spot
        if 0 < dist < spot * 0.02:
            d = "bear"
            ex = (f"{ce_top_oi:,.0f} strike-ல அதிக அளவு Call OI இருக்கு, இது spot-க்கு "
                  f"ரொம்ப அருகில (2%-க்குள்) இருக்கு. இது ஒரு strong resistance wall — "
                  f"விலை இந்த level-ஐ தாண்ட கஷ்டப்படலாம்.")
        elif dist > spot * 0.03:
            d = "bull"
            ex = f"Call writing {ce_top_oi:,.0f}-ல far OTM-ஆ இருக்கு, spot-க்கு அருகில resistance இல்ல — குறைவான தடை."
        else:
            d, ex = "neutral", f"Call writing pattern ({ce_top_oi:,.0f}) தெளிவான signal தரல."
    else:
        d, ex = "neutral", "Call writing data கிடைக்கல."
    out.append(_item("call_writing", "🔴", "கால் ரைட்டிங் (Call Writing / Resistance)",
                      f"{ce_top_oi:,.0f}" if ce_top_oi else "--", d, ex))

    # 5. Put Writing
    pe_top_oi = oi_summary.get("pe_max_oi_strike", 0)
    if pe_top_oi and spot:
        dist = spot - pe_top_oi
        if 0 < dist < spot * 0.02:
            d = "bull"
            ex = (f"{pe_top_oi:,.0f} strike-ல அதிக அளவு Put OI இருக்கு, spot-க்கு அருகில் "
                  f"இருக்கு. இது ஒரு strong support wall — விலை இந்த level-ஐ கீழே "
                  f"உடைக்க கஷ்டப்படலாம்.")
        elif dist > spot * 0.03:
            d = "bear"
            ex = f"Put writing {pe_top_oi:,.0f}-ல far OTM-ஆ இருக்கு, spot-க்கு அருகில support இல்ல — support பலவீனம்."
        else:
            d, ex = "neutral", f"Put writing pattern ({pe_top_oi:,.0f}) தெளிவான signal தரல."
    else:
        d, ex = "neutral", "Put writing data கிடைக்கல."
    out.append(_item("put_writing", "🟢", "புட் ரைட்டிங் (Put Writing / Support)",
                      f"{pe_top_oi:,.0f}" if pe_top_oi else "--", d, ex))

    # 6. Futures Premium
    if futures_status != "live":
        d, ex = "neutral", "Futures Premium — Angel One broker connection இல்லாததால இந்த data இப்போ கிடைக்கல் (0 என்பது neutral-ன்னு அர்த்தம் இல்ல, 'data இல்ல'-ன்னு அர்த்தம்)."
    elif futures_premium > 30:
        d, ex = "bull", f"Futures premium +{futures_premium:.0f} — traders futures-ஐ spot-ஐ விட அதிக விலைக்கு வாங்குறாங்க, strong long buildup."
    elif futures_premium > 10:
        d, ex = "bull", f"Futures premium +{futures_premium:.0f} — mild bullish lean இருக்கு."
    elif futures_premium < -30:
        d, ex = "bear", f"Futures discount {futures_premium:.0f} — traders futures-ஐ discount-ல வித்துக்கிட்டு இருக்காங்க, strong short buildup."
    elif futures_premium < -10:
        d, ex = "bear", f"Futures discount {futures_premium:.0f} — mild bearish lean இருக்கு."
    else:
        d, ex = "neutral", f"Futures premium {futures_premium:.0f} — spot-க்கும் futures-க்கும் பெரிய வித்தியாசம் இல்ல, neutral."
    out.append(_item("futures_premium", "📈", "ஃப்யூச்சர்ஸ் பிரீமியம்",
                      f"{futures_premium:+.0f}" if futures_status == "live" else "N/A", d, ex))

    # 7. VWAP
    if vwap > 0:
        if spot > vwap * 1.003:
            d, ex = "bull", f"Spot ({spot:,.0f}) VWAP-க்கு ({vwap:,.0f}) மேல trade ஆகுது — buyers control-ல இருக்காங்க, bullish."
        elif spot < vwap * 0.997:
            d, ex = "bear", f"Spot ({spot:,.0f}) VWAP-க்கு ({vwap:,.0f}) கீழ trade ஆகுது — sellers control-ல இருக்காங்க, bearish."
        else:
            d, ex = "neutral", f"Spot VWAP ({vwap:,.0f}) அருகிலேயே இருக்கு, balanced trading."
    else:
        d, ex = "neutral", "VWAP data இப்போ கிடைக்கல."
    out.append(_item("vwap", "⚡", "வி-வேப் (VWAP)", f"{vwap:,.0f}" if vwap else "--", d, ex))

    # 8. EMA20
    if ema20 > 0:
        if spot > ema20 * 1.002:
            d, ex = "bull", f"Spot ({spot:,.0f}) 20-day EMA-க்கு ({ema20:,.0f}) மேல இருக்கு — short-term trend bullish."
        elif spot < ema20 * 0.998:
            d, ex = "bear", f"Spot ({spot:,.0f}) 20-day EMA-க்கு ({ema20:,.0f}) கீழ இருக்கு — short-term trend bearish."
        else:
            d, ex = "neutral", f"Spot EMA20 ({ema20:,.0f}) அருகிலேயே இருக்கு."
    else:
        d, ex = "neutral", "EMA20 data கிடைக்கல."
    out.append(_item("ema20", "📉", "20-நாள் நகரும் சராசரி (EMA20)", f"{ema20:,.0f}" if ema20 else "--", d, ex))

    # 9. EMA50
    if ema50 > 0:
        if spot > ema50 * 1.002:
            d, ex = "bull", f"Spot ({spot:,.0f}) 50-day EMA-க்கு ({ema50:,.0f}) மேல இருக்கு — medium-term trend bullish."
        elif spot < ema50 * 0.998:
            d, ex = "bear", f"Spot ({spot:,.0f}) 50-day EMA-க்கு ({ema50:,.0f}) கீழ இருக்கு — medium-term trend bearish."
        else:
            d, ex = "neutral", f"Spot EMA50 ({ema50:,.0f}) அருகிலேயே இருக்கு."
    else:
        d, ex = "neutral", "EMA50 data கிடைக்கல."
    out.append(_item("ema50", "📉", "50-நாள் நகரும் சராசரி (EMA50)", f"{ema50:,.0f}" if ema50 else "--", d, ex))

    # 10. RSI
    if rsi >= 70:
        d, ex = "bear", f"RSI {rsi:.1f} — 70-க்கு மேல, Overbought zone. விலை அதிகமா ஏறிடுச்சு, correction வரலாம் — கவனமா இருங்க."
    elif rsi >= 60:
        d, ex = "bull", f"RSI {rsi:.1f} — bullish momentum zone-ல இருக்கு."
    elif rsi <= 30:
        d, ex = "bull", f"RSI {rsi:.1f} — 30-க்கு கீழ, Oversold zone. விலை அதிகமா இறங்கிடுச்சு, bounce back வரலாம்."
    elif rsi <= 40:
        d, ex = "bear", f"RSI {rsi:.1f} — bearish momentum zone-ல இருக்கு."
    else:
        d, ex = "neutral", f"RSI {rsi:.1f} — 40-60 நடுநிலை zone, தெளிவான momentum இல்ல."
    out.append(_item("rsi", "🌡️", "ஆர்எஸ்ஐ (RSI)", f"{rsi:.1f}", d, ex))

    # 11. MACD
    m, s, h = macd.get("macd", 0), macd.get("signal", 0), macd.get("histogram", 0)
    if m > s and h > 0:
        d, ex = "bull", f"MACD ({m:.1f}) Signal line-க்கு ({s:.1f}) மேல crossover ஆகி இருக்கு — bullish momentum பலப்படுது."
    elif m < s and h < 0:
        d, ex = "bear", f"MACD ({m:.1f}) Signal line-க்கு ({s:.1f}) கீழ crossover ஆகி இருக்கு — bearish momentum பலப்படுது."
    else:
        d, ex = "neutral", f"MACD ({m:.1f}) தெளிவான crossover இல்ல."
    out.append(_item("macd", "〰️", "மேக்டி (MACD)", f"{m:.1f}", d, ex))

    # 12. ADX
    if adx <= 0:
        d, ex = "neutral", "ADX data கிடைக்கல."
    elif adx < 20:
        d, ex = "neutral", f"ADX {adx:.1f} — 20-க்கு கீழ, இது range-bound market-ஐ காட்டுது, தெளிவான trend இல்ல."
    elif di_plus > di_minus:
        d, ex = "bull", f"ADX {adx:.1f} strong trend காட்டுது, +DI ({di_plus:.1f}) -DI-ஐ ({di_minus:.1f}) விட அதிகமா — strong uptrend."
    else:
        d, ex = "bear", f"ADX {adx:.1f} strong trend காட்டுது, -DI ({di_minus:.1f}) +DI-ஐ ({di_plus:.1f}) விட அதிகமா — strong downtrend."
    out.append(_item("adx", "💪", "ஏடிஎக்ஸ் (ADX — Trend Strength)", f"{adx:.1f}" if adx else "--", d, ex))

    # 13. ATR / Volatility risk — ATR திசையை (bullish/bearish) காட்டாது,
    # எதிர்பார்க்கப்படும் range/risk-ஐ மட்டும் காட்டும். Low ATR = quiet
    # market (bullish இல்ல), High ATR = wide range எதிர்பார்க்கலாம் (bearish
    # இல்ல) — அதனால decision_engine.py-ஓடு ஒத்துப்போக இது எப்போதும் neutral.
    if atr <= 0 or spot <= 0:
        d, ex = "neutral", "ATR data கிடைக்கல."
    else:
        atr_pct = (atr / spot) * 100
        if atr_pct > 1.5:
            d, ex = "neutral", f"ATR {atr:.0f} ({atr_pct:.1f}%) — அதிக volatility, இன்னிக்கு wide range எதிர்பார்க்கலாம். இது ஒரு direction indicator இல்ல, risk/range அளவுகோல் மட்டும்."
        else:
            d, ex = "neutral", f"ATR {atr:.0f} ({atr_pct:.1f}%) — குறைவான volatility, quiet/narrow range market. இதுவும் direction சொல்லாது, range மட்டும் சொல்லும்."
    out.append(_item("atr", "📏", "ஏடிஆர் (ATR — Range/Risk, திசை இல்ல)", f"{atr:.0f}" if atr else "--", d, ex))

    # 14. Supertrend
    if supertrend == "buy":
        d, ex = "bull", "Supertrend indicator BUY signal காட்டுது — trend bullish-ஆ மாறி இருக்கு."
    elif supertrend == "sell":
        d, ex = "bear", "Supertrend indicator SELL signal காட்டுது — trend bearish-ஆ மாறி இருக்கு."
    else:
        d, ex = "neutral", "Supertrend indicator தெளிவான signal தரல, neutral."
    out.append(_item("supertrend", "🎯", "சூப்பர்டிரெண்ட் (Supertrend)", (supertrend or "--").upper(), d, ex))

    # 15. Volume Spike
    if vol_spike and vol_ratio > 1.5:
        d, ex = "bull", f"சராசரியை விட {vol_ratio:.1f}x அதிக trading volume இருக்கு — strong interest/participation, move-க்கு பின்னால conviction இருக்கு."
    else:
        d, ex = "neutral", f"Volume ratio {vol_ratio:.1f}x — சாதாரண trading activity, spike இல்ல."
    out.append(_item("volume", "📶", "வால்யூம் ஸ்பைக் (Volume)", f"{vol_ratio:.1f}x avg", d, ex))

    # 16. India VIX — இதுவும் திசை indicator இல்ல, volatility/risk regime
    # மட்டும் காட்டும் (decision_engine.py-ல் VIX இனி bull/bear score-க்கு
    # பங்களிக்காது — high VIX = "பெரிய move வரலாம்" mattum, எந்த side-ன்னு
    # சொல்லாது; அதனால Long straddle போன்ற non-directional context-ல் இது
    # பயன்படும், confidence-ஐ dampen பண்ணும்).
    # FIX: vix<=0 guard சேர்க்கல முன்பு — VIX fetch fail ஆனா 0.0 fallback
    # value "12-க்கு கீழ், low volatility regime"-ன்னு fabricate பண்ணிச்சு
    # (real India VIX ஒருபோதும் 0 ஆக முடியாது, அது sentinel மட்டும்) —
    # decision_engine.py-ன் _score_vix()/_volatility_context() ஏற்கனவே
    # இந்த guard வெச்சிருந்தது, இந்த ஒரு இடம் மட்டும் miss ஆயிருந்துச்சு.
    if vix <= 0:
        d, ex = "neutral", "India VIX இப்போ கிடைக்கல் — 0-ஐ 'low volatility'-ன்னு எடுத்துக்கக் கூடாது, 'data இல்ல'-ன்னு அர்த்தம் (real VIX ஒருபோதும் 0 ஆக முடியாது)."
    elif vix > 22:
        d, ex = "neutral", f"India VIX {vix:.1f} — 22-க்கு மேல, high fear/volatility regime. பெரிய swings (எந்த direction-லும்) எதிர்பார்க்கலாம், position size/confidence கவனமா வைக்கவும்."
    elif vix < 12:
        d, ex = "neutral", f"India VIX {vix:.1f} — 12-க்கு கீழ், low volatility regime. பெரிய sudden moves வாய்ப்பு குறைவு — ஆனா இது bullish-ன்னு அர்த்தம் இல்ல, market quiet-ஆ இருக்கு-ன்னு மட்டும் அர்த்தம்."
    else:
        d, ex = "neutral", f"India VIX {vix:.1f} — normal/moderate volatility range."
    out.append(_item("vix", "😨", "இந்தியா விக்ஸ் (VIX — Volatility, திசை இல்ல)",
                      f"{vix:.2f}" if vix > 0 else "N/A", d, ex))

    # 17. Global Market
    if global_status != "live":
        d, ex = "neutral", "Global market cues இப்போ fetch ஆகல் — 0%-ன்னு காட்டினாலும் அது 'flat'-ன்னு அர்த்தம் இல்ல, 'data இல்ல'-ன்னு அர்த்தம்."
    elif global_pct > 0.5:
        d, ex = "bull", f"Global markets (US/Asia) {global_pct:+.2f}% ஆக இருக்கு — positive global cues, இது நம்ம market-க்கும் support ஆகும்."
    elif global_pct < -0.5:
        d, ex = "bear", f"Global markets {global_pct:+.2f}% ஆக இருக்கு — negative global cues, இது நம்ம market-க்கு பாதிப்பு தரலாம்."
    else:
        d, ex = "neutral", f"Global markets {global_pct:+.2f}% — பெரிய move இல்ல, genuinely neutral global sentiment (live data)."
    out.append(_item("global_market", "🌍", "குளோபல் மார்க்கெட் (Global Cues)", f"{global_pct:+.2f}%" if global_status == "live" else "N/A", d, ex))

    # 18. Gift Nifty
    if gift_status != "live":
        d, ex = "neutral", "Gift Nifty data இப்போ கிடைக்கல் — 0%-ஐ 'flat opening'-ன்னு எடுத்துக்கக் கூடாது."
    elif gift_pct > 0.3:
        d = "bull"
        ex = f"Gift Nifty {gift_pct:+.2f}% ஆக trade ஆகுது — இன்னிக்கு NIFTY gap-up-ஆ open ஆகும்-ன்னு indicate பண்ணுது."
    elif gift_pct < -0.3:
        d = "bear"
        ex = f"Gift Nifty {gift_pct:+.2f}% ஆக trade ஆகுது — இன்னிக்கு NIFTY gap-down-ஆ open ஆகும்-ன்னு indicate பண்ணுது."
    else:
        d = "neutral"
        ex = f"Gift Nifty {gift_pct:+.2f}% (live) — flat opening எதிர்பார்க்கலாம்."
    out.append(_item("gift_nifty", "🎁", "கிஃப்ட் நிஃப்டி (Gift Nifty)", f"{gift_pct:+.2f}%" if gift_status == "live" else "N/A", d, ex))

    # 19. FII
    if fii_status != "live":
        d, ex = "neutral", "FII data இப்போ கிடைக்கல் — ₹0 Cr-ன்னு காட்டினா அது real net-zero இல்ல, 'data இல்ல'-ன்னு அர்த்தம்."
    elif fii_cr > 500:
        d, ex = "bull", f"FII (Foreign Investors) ₹{fii_cr:,.0f} Cr net buying பண்ணி இருக்காங்க — strong foreign inflow, bullish."
    elif fii_cr < -500:
        d, ex = "bear", f"FII (Foreign Investors) ₹{abs(fii_cr):,.0f} Cr net selling பண்ணி இருக்காங்க — foreign outflow, bearish."
    else:
        d, ex = "neutral", f"FII net flow ₹{fii_cr:,.0f} Cr (live) — பெரிய activity இல்ல."
    out.append(_item("fii", "🌐", "எஃப்ஐஐ (FII Net Flow)", f"₹{fii_cr:,.0f} Cr" if fii_status == "live" else "N/A", d, ex))

    # 20. DII
    if dii_status != "live":
        d, ex = "neutral", "DII data இப்போ கிடைக்கல் — ₹0 Cr-ன்னு காட்டினா அது real net-zero இல்ல, 'data இல்ல'-ன்னு அர்த்தம்."
    elif dii_cr > 500:
        d, ex = "bull", f"DII (Domestic Investors) ₹{dii_cr:,.0f} Cr net buying பண்ணி இருக்காங்க — local institutional support, bullish."
    elif dii_cr < -500:
        d, ex = "bear", f"DII (Domestic Investors) ₹{abs(dii_cr):,.0f} Cr net selling பண்ணி இருக்காங்க — local institutional pullback, bearish."
    else:
        d, ex = "neutral", f"DII net flow ₹{dii_cr:,.0f} Cr (live) — பெரிய activity இல்ல."
    out.append(_item("dii", "🏛️", "டிஐஐ (DII Net Flow)", f"₹{dii_cr:,.0f} Cr" if dii_status == "live" else "N/A", d, ex))

    return out
