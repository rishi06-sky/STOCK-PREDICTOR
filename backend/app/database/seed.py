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

# India only. The platform previously seeded NYSE and NASDAQ alongside NSE and
# BSE, which put INR and USD securities in one portfolio -- and the portfolio
# summed their values without converting, so equity was rupees added to
# dollars. Rather than carry an FX rate the free data path cannot source
# honestly, the universe is single-currency by construction.
#: Every seeded venue settles in this currency; see the note on MARKETS.
MARKET_CURRENCY = "INR"

MARKETS = [
    {"code": "IN", "name": "India", "country": "India", "currency": "INR",
     "timezone": "Asia/Kolkata"},
]

EXCHANGES = [
    {"market": "IN", "code": "NSE", "name": "National Stock Exchange of India",
     "open_time": "09:15", "close_time": "15:30", "exchange_suffix": ".NS"},
    {"market": "IN", "code": "BSE", "name": "BSE Limited",
     "open_time": "09:15", "close_time": "15:30", "exchange_suffix": ".BO"},
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

# Benchmarks used for regime detection and backtest comparison.
INDICES = [
    ("NSE", "^NSEI", "NIFTY 50", "INR"),
    ("BSE", "^BSESN", "S&P BSE SENSEX", "INR"),
]

UNIVERSES = {
    "NSE": NSE_UNIVERSE, "BSE": BSE_UNIVERSE,
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
                exchange_suffix=spec["exchange_suffix"] or None,
            )
            db.add(exchange)
            db.flush()
            counts["exchanges"] += 1
        exchange_ids[spec["code"]] = exchange

    for exchange_code, rows in UNIVERSES.items():
        exchange = exchange_ids[exchange_code]
        currency = MARKET_CURRENCY
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
                # Index tickers carry their own prefix; do not append a suffix
                # for any provider.
                provider_symbols={"*": symbol},
            )
        )
        counts["indices"] += 1

    db.commit()
    log.info("reference_data_seeded", **counts)
    return counts
