"""Import every model so SQLAlchemy's registry and Alembic see the full schema."""
from app.database.base import Base
from app.models.analysis import (
    Fundamental, NewsArticle, NewsSentiment, Prediction, RegimeState, Signal,
    TechnicalFeature,
)
from app.models.enums import *  # noqa: F401,F403
from app.models.market import (
    CorporateAction, Exchange, IntradayData, Market, PriceData, Quote, Security,
)
from app.models.platform import (
    Alert, ApiKey, AuditLog, Backtest, BacktestTrade, DataFreshness, ModelMetric,
    ModelVersion, SystemEvent, User,
)
from app.models.trading import Holding, Order, Portfolio, PortfolioSnapshot, Trade

__all__ = [
    "Base", "Market", "Exchange", "Security", "PriceData", "IntradayData", "Quote",
    "CorporateAction", "TechnicalFeature", "Fundamental", "NewsArticle",
    "NewsSentiment", "RegimeState", "Prediction", "Signal", "Portfolio", "Holding",
    "Order", "Trade", "PortfolioSnapshot", "User", "ApiKey", "ModelVersion",
    "ModelMetric", "Backtest", "BacktestTrade", "Alert", "SystemEvent", "AuditLog",
    "DataFreshness",
]
