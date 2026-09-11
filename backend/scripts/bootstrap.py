#!/usr/bin/env python
"""First-run bootstrap: ingest history, train a model, put it through the gate.

Safe to re-run. Ingestion upserts, and a retrained model is registered as a new
CANDIDATE version that only replaces the incumbent if it clears the promotion
gate.

    python scripts/bootstrap.py                # default: 5-day horizon
    python scripts/bootstrap.py --years 3 --horizons 1,5,21
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.exceptions import InsufficientDataError
from app.database.seed import seed_reference_data
from app.database.session import session_scope
from app.ml.dataset import build_dataset
from app.ml.registry import PromotionGate, promote_model, register_model
from app.ml.trainer import train_and_select
from app.models.enums import AssetType, PredictionTarget
from app.models.market import Security
from app.services.pipeline import Pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=float, default=4.0, help="history to ingest")
    parser.add_argument("--horizons", default="5", help="comma-separated forecast horizons")
    parser.add_argument("--skip-ingest", action="store_true")
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]

    print("=" * 68)
    print("Stock Intelligence Platform -- bootstrap")
    print("=" * 68)

    with session_scope() as db:
        counts = seed_reference_data(db)
        print(f"\n[1/4] reference data: {counts}")

    if not args.skip_ingest:
        print(f"\n[2/4] ingesting {args.years:g} years of history "
              "(this calls the providers and may take a few minutes)...")
        with session_scope() as db:
            result = Pipeline(db).ingest(lookback_days=int(args.years * 365))
        detail = result.detail
        print(f"      bars: {detail['bars_written']:,} | quotes: {detail['quotes_written']} "
              f"| benchmarks: {detail.get('benchmark_bars', 0):,}")
        if detail["failed_count"]:
            print(f"      {detail['failed_count']} securities failed: "
                  f"{detail['failed_symbols'][:5]}")
        if not result.ok:
            print("\n  ERROR: every provider failed for every security.")
            print("  Check network access and MARKET_DATA_PROVIDERS, then re-run.")
            return 1
    else:
        print("\n[2/4] ingestion skipped")

    print("\n[3/4] training and validating models")
    promoted_any = False
    with session_scope() as db:
        securities = list(
            db.scalars(
                select(Security).where(
                    Security.is_active.is_(True), Security.asset_type == AssetType.EQUITY
                )
            )
        )
        for horizon in horizons:
            print(f"\n      horizon {horizon}d:")
            try:
                dataset = build_dataset(db, securities, horizon_days=horizon)
            except InsufficientDataError as exc:
                print(f"        skipped -- {exc}")
                continue

            print(f"        dataset: {dataset.n_rows:,} rows, "
                  f"{len(dataset.feature_names)} features, "
                  f"{dataset.meta['security_id'].nunique()} securities")

            winner, results = train_and_select(dataset)
            for result in results:
                auc = result.cv_mean.get("roc_auc")
                marker = " <- selected" if result.model_name == winner.model_name else ""
                print(f"          {result.model_name:22s} cv_auc={auc:.4f} "
                      f"lift={result.cv_mean.get('lift_over_baseline', 0):+.4f}{marker}")

            version = register_model(
                db, winner, name=f"direction_{horizon}d", target=PredictionTarget.DIRECTION
            )
            promoted, failures = promote_model(
                db, version, winner, gate=PromotionGate(), actor="bootstrap"
            )
            if promoted:
                promoted_any = True
                print(f"        PROMOTED {version.name}:{version.version} "
                      f"(holdout auc {winner.holdout.get('roc_auc'):.4f}, "
                      f"calibration error {winner.holdout.get('calibration_error'):.4f})")
            else:
                print(f"        NOT PROMOTED -- {version.name}:{version.version} "
                      "stays a candidate:")
                for failure in failures:
                    print(f"          - {failure}")

    print("\n[4/4] running one full pipeline cycle")
    with session_scope() as db:
        report = Pipeline(db).run_full_cycle(horizon_days=horizons[0])
    for stage in report.stages:
        status = "skipped" if stage.skipped_reason else ("ok" if stage.ok else "FAILED")
        print(f"      {stage.name:16s} {status}")
        if stage.skipped_reason:
            print(f"        {stage.skipped_reason}")

    print("\n" + "=" * 68)
    if promoted_any:
        print("Bootstrap complete. The scheduler will keep data and signals current.")
    else:
        print("Bootstrap complete, but NO model cleared the promotion gate.")
        print("No signals will be generated until one does -- this is deliberate:")
        print("a model with no demonstrated edge must not drive recommendations.")
        print("Try a longer history (--years 6) or a different horizon.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
