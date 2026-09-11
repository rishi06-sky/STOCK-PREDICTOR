"""Reference-data seed: markets, exchanges and a starter universe.

The securities below are real listings with their real exchange, sector and
currency. Prices are never seeded -- those come from providers only.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.enums import AssetType
from app.models.market import Exchange, Market, Security

log = get_logger(__name__)

MARKETS = [
    {"code": "IN", "name": "India", "country": "India", "currency": "INR",
     "timezone": "Asia/Kolkata"},
    {"code": "US", "name": "United States", "country": "United States", "currency": "USD",
     "timezone": "America/New_York"},
]

EXCHANGES = [
    {"market": "IN", "code": "NSE", "name": "National Stock Exchange of India",
     "open_time": "09:15", "close_time": "15:30", "yahoo_suffix": ".NS"},
    {"market": "IN", "code": "BSE", "name": "BSE Limited",
     "open_time": "09:15", "close_time": "15:30", "yahoo_suffix": ".BO"},
    {"market": "US", "code": "NYSE", "name": "New York Stock Exchange",
     "open_time": "09:30", "close_time": "16:00", "yahoo_suffix": ""},
    {"market": "US", "code": "NASDAQ", "name": "Nasdaq Stock Market",
     "open_time": "09:30", "close_time": "16:00", "yahoo_suffix": ""},
]

# (symbol, name, sector, industry)
NSE_UNIVERSE = [
    ("RELIANCE", "Reliance Industries Ltd", "Energy", "Refineries & Marketing"),
    ("TCS", "Tata Consultancy Services Ltd", "Information Technology", "IT Services"),
    ("HDFCBANK", "HDFC Bank Ltd", "Financial Services", "Private Bank"),
    ("INFY", "Infosys Ltd", "Information Technology", "IT Services"),
    ("ICICIBANK", "ICICI Bank Ltd", "Financial Services", "Private Bank"),
    ("BHARTIARTL", "Bharti Airtel Ltd", "Telecommunication", "Telecom Services"),
    ("SBIN", "State Bank of India", "Financial Services", "Public Bank"),
    ("LT", "Larsen & Toubro Ltd", "Capital Goods", "Construction & Engineering"),
    ("ITC", "ITC Ltd", "Consumer Staples", "Diversified FMCG"),
    ("HINDUNILVR", "Hindustan Unilever Ltd", "Consumer Staples", "Household Products"),
    ("AXISBANK", "Axis Bank Ltd", "Financial Services", "Private Bank"),
    ("MARUTI", "Maruti Suzuki India Ltd", "Automobile", "Passenger Cars"),
    ("SUNPHARMA", "Sun Pharmaceutical Industries Ltd", "Healthcare", "Pharmaceuticals"),
    ("TATAMOTORS", "Tata Motors Ltd", "Automobile", "Commercial Vehicles"),
    ("WIPRO", "Wipro Ltd", "Information Technology", "IT Services"),
    ("ASIANPAINT", "Asian Paints Ltd", "Consumer Discretionary", "Paints"),
    ("TITAN", "Titan Company Ltd", "Consumer Discretionary", "Gems & Jewellery"),
    ("ULTRACEMCO", "UltraTech Cement Ltd", "Materials", "Cement"),
    ("NESTLEIND", "Nestle India Ltd", "Consumer Staples", "Packaged Foods"),
    ("POWERGRID", "Power Grid Corporation of India Ltd", "Utilities", "Power Transmission"),
]

BSE_UNIVERSE = [
    ("RELIANCE", "Reliance Industries Ltd", "Energy", "Refineries & Marketing"),
    ("TCS", "Tata Consultancy Services Ltd", "Information Technology", "IT Services"),
    ("HDFCBANK", "HDFC Bank Ltd", "Financial Services", "Private Bank"),
]

NYSE_UNIVERSE = [
    ("JPM", "JPMorgan Chase & Co.", "Financial Services", "Diversified Banks"),
    ("JNJ", "Johnson & Johnson", "Healthcare", "Pharmaceuticals"),
    ("V", "Visa Inc.", "Financial Services", "Payment Processing"),
    ("WMT", "Walmart Inc.", "Consumer Staples", "Hypermarkets"),
    ("XOM", "Exxon Mobil Corporation", "Energy", "Integrated Oil & Gas"),
    ("PG", "Procter & Gamble Co.", "Consumer Staples", "Household Products"),
    ("UNH", "UnitedHealth Group Inc.", "Healthcare", "Managed Care"),
    ("HD", "Home Depot Inc.", "Consumer Discretionary", "Home Improvement Retail"),
]

NASDAQ_UNIVERSE = [
    ("AAPL", "Apple Inc.", "Information Technology", "Consumer Electronics"),
    ("MSFT", "Microsoft Corporation", "Information Technology", "Software"),
    ("GOOGL", "Alphabet Inc. Class A", "Communication Services", "Interactive Media"),
    ("AMZN", "Amazon.com Inc.", "Consumer Discretionary", "Internet Retail"),
    ("NVDA", "NVIDIA Corporation", "Information Technology", "Semiconductors"),
    ("META", "Meta Platforms Inc.", "Communication Services", "Interactive Media"),
    ("TSLA", "Tesla Inc.", "Consumer Discretionary", "Automobile Manufacturers"),
    ("AVGO", "Broadcom Inc.", "Information Technology", "Semiconductors"),
    ("COST", "Costco Wholesale Corporation", "Consumer Staples", "Hypermarkets"),
    ("NFLX", "Netflix Inc.", "Communication Services", "Entertainment"),
]

# Benchmarks used for regime detection and backtest comparison.
INDICES = [
    ("NSE", "^NSEI", "NIFTY 50", "INR"),
    ("NASDAQ", "^GSPC", "S&P 500", "USD"),
]

UNIVERSES = {
    "NSE": NSE_UNIVERSE, "BSE": BSE_UNIVERSE,
    "NYSE": NYSE_UNIVERSE, "NASDAQ": NASDAQ_UNIVERSE,
}


def seed_reference_data(db: Session) -> dict[str, int]:
    counts = {"markets": 0, "exchanges": 0, "securities": 0, "indices": 0}

    market_ids: dict[str, int] = {}
    for spec in MARKETS:
        market = db.scalar(select(Market).where(Market.code == spec["code"]))
        if market is None:
            market = Market(**spec)
            db.add(market)
            db.flush()
            counts["markets"] += 1
        market_ids[spec["code"]] = market.id

    exchange_ids: dict[str, Exchange] = {}
    for spec in EXCHANGES:
        exchange = db.scalar(select(Exchange).where(Exchange.code == spec["code"]))
        if exchange is None:
            exchange = Exchange(
                market_id=market_ids[spec["market"]],
                code=spec["code"], name=spec["name"],
                open_time=spec["open_time"], close_time=spec["close_time"],
                yahoo_suffix=spec["yahoo_suffix"] or None,
            )
            db.add(exchange)
            db.flush()
            counts["exchanges"] += 1
        exchange_ids[spec["code"]] = exchange

    for exchange_code, rows in UNIVERSES.items():
        exchange = exchange_ids[exchange_code]
        currency = "INR" if exchange_code in ("NSE", "BSE") else "USD"
        for symbol, name, sector, industry in rows:
            exists = db.scalar(
                select(Security).where(
                    Security.exchange_id == exchange.id, Security.symbol == symbol
                )
            )
            if exists:
                continue
            db.add(
                Security(
                    exchange_id=exchange.id, symbol=symbol, name=name,
                    asset_type=AssetType.EQUITY, sector=sector, industry=industry,
                    currency=currency,
                )
            )
            counts["securities"] += 1

    for exchange_code, symbol, name, currency in INDICES:
        exchange = exchange_ids[exchange_code]
        exists = db.scalar(
            select(Security).where(
                Security.exchange_id == exchange.id, Security.symbol == symbol
            )
        )
        if exists:
            continue
        db.add(
            Security(
                exchange_id=exchange.id, symbol=symbol, name=name,
                asset_type=AssetType.INDEX, currency=currency,
                # Index tickers carry their own prefix; do not append a suffix.
                provider_symbols={"yahoo": symbol},
            )
        )
        counts["indices"] += 1

    db.commit()
    log.info("reference_data_seeded", **counts)
    return counts
