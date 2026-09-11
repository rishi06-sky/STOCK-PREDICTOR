"""Financial sentiment analysis.

A lexicon-and-rules analyser in the Loughran-McDonald tradition: general-purpose
sentiment models mislabel financial text badly ("liability", "aggressive" and
"tax" are not negatives in a filing), so the vocabulary here is finance-specific.

This is deliberately a transparent, deterministic, zero-cost model rather than a
neural one. Its output carries an explicit `confidence` that falls when little
sentiment-bearing vocabulary is present, and the analyser name and version are
recorded on every row so results stay attributable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.enums import SentimentLabel

ANALYZER_NAME = "lexicon-financial"
ANALYZER_VERSION = "1.0"

POSITIVE_TERMS: dict[str, float] = {
    "beat": 1.5, "beats": 1.5, "exceeded": 1.4, "outperform": 1.5, "outperformed": 1.5,
    "surge": 1.6, "surged": 1.6, "soar": 1.7, "soared": 1.7, "rally": 1.3, "rallied": 1.3,
    "jump": 1.2, "jumped": 1.2, "gain": 1.0, "gains": 1.0, "gained": 1.0, "rise": 0.9,
    "rose": 0.9, "climb": 1.0, "climbed": 1.0, "upgrade": 1.6, "upgraded": 1.6,
    "growth": 1.1, "profit": 1.1, "profitable": 1.3, "record": 1.2, "strong": 1.2,
    "robust": 1.2, "boost": 1.2, "boosted": 1.2, "expansion": 1.0, "expanding": 1.0,
    "dividend": 0.7, "buyback": 1.2, "approval": 1.1, "approved": 1.1, "win": 1.1,
    "won": 1.1, "awarded": 1.1, "partnership": 0.8, "milestone": 0.9, "bullish": 1.5,
    "optimistic": 1.1, "raised": 1.1, "raises": 1.1, "positive": 1.0, "improve": 1.0,
    "improved": 1.0, "improvement": 1.0, "accelerate": 1.1, "breakthrough": 1.4,
    "momentum": 0.8, "high": 0.6, "surpass": 1.4, "surpassed": 1.4, "expands": 1.0,
}

NEGATIVE_TERMS: dict[str, float] = {
    "miss": 1.5, "missed": 1.5, "misses": 1.5, "plunge": 1.7, "plunged": 1.7,
    "slump": 1.5, "slumped": 1.5, "crash": 1.8, "crashed": 1.8, "tumble": 1.5,
    "tumbled": 1.5, "fall": 1.0, "fell": 1.0, "falls": 1.0, "drop": 1.1, "dropped": 1.1,
    "decline": 1.1, "declined": 1.1, "downgrade": 1.6, "downgraded": 1.6, "loss": 1.3,
    "losses": 1.3, "weak": 1.2, "weakness": 1.2, "concern": 1.0, "concerns": 1.0,
    "warning": 1.4, "warns": 1.4, "warned": 1.4, "probe": 1.3, "investigation": 1.4,
    "lawsuit": 1.4, "fraud": 1.9, "penalty": 1.3, "fine": 1.1, "fined": 1.3,
    "default": 1.7, "bankruptcy": 2.0, "insolvency": 2.0, "layoff": 1.4, "layoffs": 1.4,
    "cut": 1.0, "cuts": 1.0, "slashed": 1.5, "bearish": 1.5, "pessimistic": 1.1,
    "resign": 1.2, "resigned": 1.2, "recall": 1.3, "delay": 1.0, "delayed": 1.0,
    "halt": 1.3, "halted": 1.3, "suspend": 1.4, "suspended": 1.4, "breach": 1.4,
    "negative": 1.0, "deteriorate": 1.3, "shortfall": 1.4, "writedown": 1.5,
    "impairment": 1.4, "downturn": 1.3, "risk": 0.7, "risks": 0.7,
}

NEGATORS = {"not", "no", "never", "without", "fails", "fail", "failed", "unable", "denies", "denied"}
INTENSIFIERS = {"very": 1.4, "sharply": 1.5, "significantly": 1.4, "substantially": 1.4,
                "slightly": 0.6, "marginally": 0.5, "modestly": 0.7, "massively": 1.7}

TOKEN_RE = re.compile(r"[a-z']+")


@dataclass(frozen=True, slots=True)
class SentimentResult:
    label: SentimentLabel
    score: float        # -1..1
    confidence: float   # 0..1
    analyzer: str
    matched_terms: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "label": str(self.label), "score": self.score,
            "confidence": self.confidence, "analyzer": self.analyzer,
            "matched_terms": list(self.matched_terms),
        }


def analyze(text: str) -> SentimentResult:
    """Score financial text on a -1..1 scale."""
    analyzer = f"{ANALYZER_NAME}:{ANALYZER_VERSION}"
    if not text or not text.strip():
        return SentimentResult(SentimentLabel.NEUTRAL, 0.0, 0.0, analyzer, ())

    tokens = TOKEN_RE.findall(text.lower())
    if not tokens:
        return SentimentResult(SentimentLabel.NEUTRAL, 0.0, 0.0, analyzer, ())

    total = 0.0
    matched: list[str] = []
    for i, token in enumerate(tokens):
        weight = POSITIVE_TERMS.get(token)
        polarity = 1.0
        if weight is None:
            weight = NEGATIVE_TERMS.get(token)
            polarity = -1.0
        if not weight:
            continue

        # Look back two tokens for a negator or intensifier.
        window = tokens[max(0, i - 2):i]
        if any(w in NEGATORS for w in window):
            polarity *= -1.0
        for w in window:
            weight *= INTENSIFIERS.get(w, 1.0)

        total += polarity * weight
        matched.append(token)

    if not matched:
        return SentimentResult(SentimentLabel.NEUTRAL, 0.0, 0.15, analyzer, ())

    # Normalise by sentiment-bearing term count, then squash to (-1, 1).
    raw = total / (len(matched) ** 0.5)
    score = max(-1.0, min(1.0, raw / 3.0))

    # Confidence rises with how much of the text carried sentiment, and with
    # the strength of the signal. A single weak word stays low-confidence.
    density = len(matched) / max(len(tokens), 1)
    confidence = round(min(1.0, 0.25 + abs(score) * 0.5 + min(density * 2.0, 0.35)), 3)

    if score > 0.15:
        label = SentimentLabel.POSITIVE
    elif score < -0.15:
        label = SentimentLabel.NEGATIVE
    else:
        label = SentimentLabel.NEUTRAL

    return SentimentResult(
        label=label, score=round(score, 4), confidence=confidence,
        analyzer=analyzer, matched_terms=tuple(matched[:12]),
    )


def aggregate_sentiment(results: list[SentimentResult]) -> dict:
    """Confidence-weighted aggregate over a set of articles."""
    if not results:
        return {"score": 0.0, "confidence": 0.0, "count": 0, "label": str(SentimentLabel.NEUTRAL)}

    weight_total = sum(r.confidence for r in results) or 1e-9
    score = sum(r.score * r.confidence for r in results) / weight_total
    confidence = sum(r.confidence for r in results) / len(results)
    # More corroborating articles justify more confidence, with diminishing returns.
    confidence = min(1.0, confidence * (1 + min(len(results), 10) / 20))

    # Articles that contradict each other are weaker evidence, not stronger:
    # discount confidence by how much the individual scores disagree.
    if len(results) > 1:
        mean = sum(r.score for r in results) / len(results)
        spread = (sum((r.score - mean) ** 2 for r in results) / len(results)) ** 0.5
        confidence *= max(0.35, 1.0 - min(spread, 1.0))

    if score > 0.15:
        label = SentimentLabel.POSITIVE
    elif score < -0.15:
        label = SentimentLabel.NEGATIVE
    else:
        label = SentimentLabel.NEUTRAL

    return {
        "score": round(score, 4), "confidence": round(confidence, 3),
        "count": len(results), "label": str(label),
        "positive": sum(1 for r in results if r.label is SentimentLabel.POSITIVE),
        "negative": sum(1 for r in results if r.label is SentimentLabel.NEGATIVE),
        "neutral": sum(1 for r in results if r.label is SentimentLabel.NEUTRAL),
    }
