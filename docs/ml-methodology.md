# ML methodology and what the numbers mean

This document exists so nobody — including a future maintainer — mistakes a
validation metric for a promise.

## The prediction problem

**Target:** will this security's close be higher in *N* trading days than it is
today? A binary classification over horizons of 1, 3, 5 and 21 days.

This is a deliberately modest target. The platform does not attempt to predict
price levels, and treats "expected return" as an estimate derived from the
model's edge scaled by volatility, not as a price forecast.

## Why not deep learning

With a few thousand labelled rows per horizon and roughly thirty noisy
features, a neural network has no structural advantage over gradient boosting
and considerably more capacity to memorise noise. Baselines are trained
alongside every candidate; if logistic regression wins, that is a finding
worth acting on, not an embarrassment to hide.

## Preventing look-ahead bias

Bias creeps in at three points, each closed explicitly:

**1. Feature computation.** Every indicator is causal: the value at index *i*
uses only data at indices ≤ *i*. This is asserted in the test suite by
recomputing each indicator on a truncated series and requiring identical
values — a parameterised test over sixteen indicators plus the full pipeline.

**2. Label alignment.** The label on row *t* is the return from *t*'s close to
*(t + horizon)*'s close, produced by `shift(-horizon)`. The final `horizon`
rows therefore have no label and are dropped before training. They are the
live prediction set, not training data. `assert_no_leakage` re-derives every
label from raw prices and fails if any disagrees.

**3. Splitting.** Standard k-fold would train on data from after the test
window. Instead:

- Splits are chronological and expanding, never shuffled.
- Splitting happens on **unique dates**, not row positions. With ~40
  securities sharing each date, a positional split would put the same session
  on both sides of the boundary.
- An **embargo** of `ML_EMBARGO_DAYS` trading days sits between train and
  test. Without it the last training rows carry labels computed from prices
  inside the test window — leaking the answer across the boundary.
- A final holdout block is reserved, scored exactly once, and never used to
  choose anything.

Imputation and scaling live inside the sklearn `Pipeline`, so their statistics
are fit per-fold rather than over the whole dataset.

## The canary: proving the absence of leakage

A clean pipeline cannot predict a random walk. The test suite therefore trains
every model family on pure geometric random walks and **requires** ROC-AUC
below 0.56. Measured: **~0.49 across logistic regression, random forest,
gradient boosting, XGBoost and LightGBM**, with every one correctly refused by
the promotion gate.

That test alone is insufficient — a pipeline that is simply broken would also
score 0.5. So the suite also injects a genuine momentum effect and **requires**
AUC above 0.55. Measured: **0.62–0.65**, with the top features being exactly
the trend and momentum features that carry the injected effect, and
calibration error around 1–3%.

Both directions must hold. Either alone proves nothing.

## Metrics, and why accuracy is reported with a chaperone

A market that rises on 54% of days makes an "always up" predictor look 54%
accurate. Every classification result is therefore reported alongside:

- **`baseline_accuracy`** — always predicting the majority class
- **`lift_over_baseline`** — accuracy minus that baseline; this is the real edge
- **ROC-AUC** — ranking quality, insensitive to class balance
- **Brier score** — squared error of the probabilities themselves
- **Expected calibration error** — the gap between stated and realised
  confidence, plus a per-bin reliability table

A model that cannot beat the majority-class baseline is reported as such and
is never promoted, however good its raw accuracy looks.

## Calibration

Raw classifier scores are not probabilities. Outputs are calibrated with
isotonic regression (≥2000 rows) or Platt scaling, fit on the development
block only — the holdout is untouched, so reported out-of-sample figures stay
out-of-sample. A model claiming 70% confidence should be right about 70% of
the time; the reliability table on the Models page shows whether it is.

## Confidence, and why it is not the raw probability

Confidence is expressed on the same scale as the thing it describes, then
*discounted* — never inflated:

```
directional = max(p, 1 - p)                 # 0.5 .. 1.0
quality     = f(validated AUC) × calibration × data freshness × regime
confidence  = 0.5 + (directional - 0.5) × quality
```

A worthless model discounts toward 0.5 — a coin flip — not toward certainty of
the opposite. And a hard ceiling applies: a model with no validated AUC, or
with AUC at or below 0.53, is capped below the actionable floor. No
probability, however extreme, can push a thin-edge model into producing a
signal.

Below `SIGNAL_MIN_CONFIDENCE` the engine emits `NO_ACTION`. **No signal is
better than a forced one.**

## The promotion gate

Training never silently replaces a production model. A freshly trained model
becomes a `CANDIDATE` and reaches `PRODUCTION` only if it clears:

- ROC-AUC ≥ 0.52 out-of-fold
- lift over the majority baseline ≥ 0.005
- calibration error ≤ 0.15 on the holdout
- at least 500 training rows
- no more than 0.02 AUC worse than the incumbent it would replace

Failures are recorded as a system event with reasons. The incumbent keeps
serving. Rollback restores the most recently archived version.

## Drift

Live feature distributions are compared against the baseline stored with each
model using the Population Stability Index (<0.1 stable, 0.1–0.25 moderate,
>0.25 significant). Drift is surfaced on the Models page and is a signal to
retrain — it does not auto-promote anything.

## Backtesting

Backtests answer "what would this have returned", and are labelled
`BACKTEST PERFORMANCE`, never conflated with live results. The engine:

- fills a signal computed from bar *t*'s close at bar *t+1*'s **open**
- applies commission and adverse slippage to every fill, in both directions
- assumes the **stop** filled first when a bar's range spans both stop and
  target, because the intrabar path is unknowable and optimism is unearned
- warns when fewer than 30 trades were closed, because the statistics are then
  not meaningful

Validated by controls: random signals lose approximately the cost drag, and
only a look-ahead oracle turns a large profit.

## Known limitations

- **Survivorship bias.** The universe is a current list of listed securities.
  A backtest over it will not include companies that were delisted during the
  period. The schema carries `delisted_on` to support this properly, but the
  seeded universe does not yet populate it.
- **Free data is delayed.** Every signal is built on 15-minute-delayed or
  end-of-day prices, and is tagged accordingly.
- **Fundamentals coverage is thin** on the free provider path, so the
  fundamental score reports its coverage and flags itself unreliable below 50%.
- **Regime detection is rule-based**, chosen for inspectability over a latent
  state model that would mostly fit noise on a few years of daily data.
- **These are daily-horizon directional models.** An AUC of 0.60 on this
  problem is a modest, real edge — not a trading system that prints money once
  costs, slippage and drawdown are accounted for.
