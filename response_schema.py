"""
response_schema.py
===================
Strict Pydantic schema for Gemini's job-screener JSON response (Strict Deterministic Template
Engine routing keys only - never resume/email prose). Centralizes bounds/type validation so
main.py's evaluate_job_with_gemini() no longer hand-rolls isinstance()/range checks per field.
"""

from pydantic import BaseModel, Field, field_validator
from typing import List, Literal


class GeminiJobScreenerResponse(BaseModel):
    score: int = Field(..., ge=0, le=100)
    reason: str = "N/A"
    track: Literal["a", "b", "c", "d", "e"] = "a"
    tone_mode: Literal["conservative", "tech"] = "conservative"
    bullet_indices: List[int] = Field(default_factory=lambda: [0, 1, 2])
    linkedin_template_id: int = Field(default=0, ge=0, le=9)
    outreach_template_id: int = Field(default=0, ge=0, le=5)

    # Literal fields are case-sensitive and score is required - normalize before validation so
    # model_validate_json() can be called directly on Gemini's raw JSON text.
    @field_validator("score", mode="before")
    @classmethod
    def _coerce_score(cls, v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    @field_validator("track", mode="before")
    @classmethod
    def _normalize_track(cls, v):
        return str(v or "a").strip().lower()

    @field_validator("tone_mode", mode="before")
    @classmethod
    def _normalize_tone_mode(cls, v):
        return str(v or "conservative").strip().lower()
