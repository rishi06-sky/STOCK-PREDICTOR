"""News ingestion and sentiment persistence.

Articles are deduplicated on a content hash, and both `published_at` (when the
publisher says it happened) and `retrieved_at` (when we saw it) are stored, so
the dashboard can distinguish genuinely breaking news from an old story that
merely arrived in this fetch.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.market_data.registry import ProviderChain, get_chain
from app.market_data.types import NewsItem
from app.models.analysis import NewsArticle, NewsSentiment
from app.models.market import Security
from app.sentiment.analyzer import aggregate_sentiment, analyze

log = get_logger(__name__)

#: Beyond this an article is history, not news, however recently it was
#: retrieved. Kept deliberately short: a four-hour-old story flagged as
#: "breaking" is the kind of small dishonesty that erodes trust in the feed.
BREAKING_WINDOW = timedelta(hours=2)


@dataclass(slots=True)
class NewsIngestReport:
    symbol: str | None
    fetched: int = 0
    stored: int = 0
    duplicates: int = 0
    provider: str | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "provider": self.provider, "fetched": self.fetched,
            "stored": self.stored, "duplicates": self.duplicates, "errors": self.errors,
        }


def content_hash(item: NewsItem) -> str:
    """Stable identity for an article.

    Keyed on headline plus source plus publication day -- the same story
    re-fetched, or served with a tracking-decorated URL, hashes identically.
    """
    basis = "|".join(
        [
            item.headline.strip().lower(),
            (item.source or "").strip().lower(),
            item.published_at.astimezone(timezone.utc).date().isoformat(),
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def is_breaking(article: NewsArticle, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return (now - article.published_at) <= BREAKING_WINDOW


class NewsPipeline:
    def __init__(self, db: Session, chain: ProviderChain | None = None):
        self.db = db
        self.chain = chain or get_chain()

    def ingest_for_security(
        self, security: Security, limit: int = 25, *, commit: bool = True
    ) -> NewsIngestReport:
        report = NewsIngestReport(symbol=security.symbol)
        items: list[NewsItem] = []

        for provider in self.chain.providers:
            if "news" not in provider.capabilities or not provider.is_configured():
                continue
            psym = security.provider_symbol(provider.name)
            if not provider.supports_symbol(psym):
                report.errors.append(f"{provider.name}: does not cover {psym}")
                continue
            try:
                items = provider.fetch_news(psym, limit)
            except Exception as exc:
                report.errors.append(f"{provider.name}: {exc}")
                continue
            if items:
                report.provider = provider.name
                break

        if not items:
            if not report.errors:
                report.errors.append("no news provider returned articles")
            return report

        report.fetched = len(items)
        for item in items:
            if self._store(item, security_id=security.id):
                report.stored += 1
            else:
                report.duplicates += 1

        if commit:
            self.db.commit()
        return report

    def _store(self, item: NewsItem, *, security_id: int | None) -> bool:
        """Insert an article and its sentiment. Returns False when duplicate."""
        digest = content_hash(item)
        if self.db.scalar(select(NewsArticle.id).where(NewsArticle.content_hash == digest)):
            return False

        article = NewsArticle(
            security_id=security_id,
            headline=item.headline[:2000],
            summary=item.summary,
            url=item.url,
            source=item.source[:128],
            category=item.category,
            published_at=item.published_at,
            retrieved_at=item.retrieved_at,
            content_hash=digest,
            provider=item.provider,
        )
        self.db.add(article)
        self.db.flush()

        # Score headline and summary together; the summary adds context the
        # headline often omits.
        text = item.headline if not item.summary else f"{item.headline}. {item.summary}"
        result = analyze(text)
        self.db.add(
            NewsSentiment(
                news_id=article.id,
                security_id=security_id,
                label=result.label,
                score=result.score,
                confidence=result.confidence,
                analyzer=result.analyzer,
                analyzed_at=datetime.now(timezone.utc),
            )
        )
        return True

    # ---------------------------------------------------------------- reads
    def recent_sentiment(
        self, security_id: int, *, days: int = 7, min_confidence: float = 0.2
    ) -> dict:
        """Aggregate sentiment over a recent window, weighted by confidence."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        rows = self.db.execute(
            select(NewsSentiment.score, NewsSentiment.confidence, NewsSentiment.label)
            .join(NewsArticle, NewsArticle.id == NewsSentiment.news_id)
            .where(
                NewsSentiment.security_id == security_id,
                NewsArticle.published_at >= cutoff,
                NewsSentiment.confidence >= min_confidence,
            )
        ).all()

        if not rows:
            return {
                "score": 0.0, "confidence": 0.0, "count": 0,
                "label": "NEUTRAL", "window_days": days,
                "note": "no articles with sufficient confidence in window",
            }

        from app.sentiment.analyzer import SentimentResult

        results = [
            SentimentResult(
                label=row.label, score=float(row.score),
                confidence=float(row.confidence), analyzer="", matched_terms=(),
            )
            for row in rows
        ]
        return {**aggregate_sentiment(results), "window_days": days}

    def latest_articles(
        self, security_id: int, limit: int = 10
    ) -> list[tuple[NewsArticle, NewsSentiment | None]]:
        articles = self.db.scalars(
            select(NewsArticle)
            .where(NewsArticle.security_id == security_id)
            .order_by(NewsArticle.published_at.desc())
            .limit(limit)
        ).all()
        return [(a, a.sentiment) for a in articles]
