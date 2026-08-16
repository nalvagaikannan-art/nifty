from typing import Dict

class RiskManager:
    @staticmethod
    def assess_risk(market_data: Dict) -> Dict:
        """Compute risk score based on volatility, trend, PCR, etc."""
        vix = market_data.get("vix", 0)
        pcr = market_data.get("pcr", 0)
        trend = market_data.get("trend", "sideways")
        # Simple logic
        risk_score = 0
        if vix > 25:
            risk_score += 30
        elif vix > 20:
            risk_score += 20
        if pcr < 0.7 or pcr > 1.3:
            risk_score += 20
        if trend == "bearish":
            risk_score += 20
        elif trend == "bullish":
            risk_score += 10
        risk_level = "low" if risk_score < 30 else "medium" if risk_score < 60 else "high"
        return {"score": risk_score, "level": risk_level}
