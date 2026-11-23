import json
from app.schemas import AnalysisDecisionResponse

with open("/tmp/decision_validate.json") as f:
    data = json.load(f)

model = AnalysisDecisionResponse.model_validate(data)
out = model.model_dump()

print("VALID")
print("LIVE_KEYS=" + str(len(data)))
print("MODEL_KEYS=" + str(len(out)))
print("DROPPED_KEYS=" + str(sorted(set(data) - set(out))))
print("strategy_detail=" + type(data.get("strategy_detail")).__name__)
print("price_levels=" + type(data.get("price_levels")).__name__)
