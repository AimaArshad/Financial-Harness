"""Structured suggestion format + guardrail validation (runs BEFORE a suggestion is accepted)."""
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, model_validator

MAX_DATA_AGE_MINUTES = 15
DISCLAIMER = "Demo only. Not financial advice."


def data_age_minutes(iso_timestamp: str, now: Optional[datetime] = None) -> float:
    """Age of a data timestamp in minutes. Raises ValueError if the format is bad."""
    now = now or datetime.now(timezone.utc)
    ts = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 60


class Suggestion(BaseModel):
    symbol: str
    horizon: Literal["long_term", "short_term"]
    action: Literal["HOLD", "ACCUMULATE", "BUY", "SELL"]
    confidence: Literal["low", "medium", "high"]
    reasoning: str = Field(min_length=20)
    stop_loss: Optional[float] = None
    data_as_of: str

    @model_validator(mode="after")
    def check_rules(self):
        if self.horizon == "long_term" and self.action not in ("HOLD", "ACCUMULATE"):
            raise ValueError("long_term suggestions must be HOLD or ACCUMULATE")
        if self.horizon == "short_term" and self.action not in ("BUY", "SELL", "HOLD"):
            raise ValueError("short_term suggestions must be BUY, SELL or HOLD")
        # Guardrail rule 1: every BUY needs a stop-loss (SELL reduces risk, so it needs none)
        if self.action == "BUY" and self.stop_loss is None:
            raise ValueError(f"{self.symbol}: BUY requires a stop_loss price below the current price")
        return self


class SuggestionSet(BaseModel):
    goal_horizon: str
    summary: str
    suggestions: list[Suggestion] = Field(min_length=1)


def validate_suggestions(payload: dict, held_symbols: set, now: Optional[datetime] = None):
    """Return (True, final_dict) or (False, list_of_error_strings)."""
    now = now or datetime.now(timezone.utc)
    try:
        parsed = SuggestionSet(**payload)
    except ValidationError as e:
        return False, [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
    except TypeError as e:
        return False, [str(e)]

    errors = []
    for s in parsed.suggestions:
        if s.symbol.upper() not in held_symbols:
            errors.append(f"{s.symbol}: not in the user's portfolio")
        # Guardrail rule 3: data must be fresh and timestamped
        try:
            age_min = data_age_minutes(s.data_as_of, now)
            if age_min > MAX_DATA_AGE_MINUTES:
                errors.append(f"{s.symbol}: data is {age_min:.0f} min old (max {MAX_DATA_AGE_MINUTES}); refetch it")
        except ValueError:
            errors.append(f"{s.symbol}: data_as_of must be the ISO timestamp from the tool output")
    if errors:
        return False, errors

    final = parsed.model_dump()
    final["disclaimer"] = DISCLAIMER  # added by the harness, not the model
    final["generated_at"] = now.isoformat(timespec="seconds")
    return True, final
