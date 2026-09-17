'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { fetcher } from '@/lib/api';
import { formatDateTime, relativeTime } from '@/lib/format';
import { Empty, Loading, Panel } from '@/components/ui';

interface ModelVersion {
  id: number; name: string; version: string; algorithm: string; target: string;
  horizon_days: number; status: string; feature_set_version: string;
  training_rows: number | null; trained_at: string | null;
  promoted_at: string | null; notes: string | null;
}

interface ModelDetail {
  model: ModelVersion;
  metrics: Record<string, Record<string, number>>;
  feature_names: string[] | null;
  artifact_sha256: string | null;
  disclaimer: string;
}

interface Drift {
  max_psi?: number; drift_detected?: boolean;
  drifted_features?: string[]; feature_psi?: Record<string, number>;
  note?: string;
}

const STATUS_STYLES: Record<string, string> = {
  PRODUCTION: 'bg-bull-soft text-bull',
  CANDIDATE: 'bg-accent-soft text-accent',
  ARCHIVED: 'bg-ground-overlay text-ink-muted',
  FAILED: 'bg-bear-soft text-bear',
};

/** Metrics worth surfacing, with why each one matters. */
const KEY_METRICS: [string, string][] = [
  ['roc_auc', 'Ranking quality; 0.5 is a coin flip'],
  ['accuracy', 'Raw hit rate — read it against the baseline'],
  ['baseline_accuracy', 'Always predicting the majority class'],
  ['lift_over_baseline', 'Accuracy minus baseline; this is the real edge'],
  ['brier', 'Probability error, lower is better'],
  ['calibration_error', 'Gap between stated and realised confidence'],
  ['precision', 'Of predicted ups, how many rose'],
  ['recall', 'Of actual ups, how many were caught'],
  ['f1', 'Harmonic mean of precision and recall'],
  ['sample_size', 'Rows evaluated'],
];

export default function ModelsPage() {
  const models = useSWR<ModelVersion[]>('/models', fetcher);
  const [selected, setSelected] = useState<number | null>(null);
  const detail = useSWR<ModelDetail>(selected ? `/models/${selected}` : null, fetcher);
  const drift = useSWR<Drift>(selected ? `/models/${selected}/drift` : null, fetcher);

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">Model monitoring</h1>
        <p className="text-2xs text-ink-faint">
          Validation metrics measured out-of-sample during training — not live
          trading results.
        </p>
      </div>

      {models.isLoading && <Panel><Loading /></Panel>}
      {models.data?.length === 0 && (
        <Panel>
          <Empty
            message="No models registered"
            hint="Train a model via the retraining job; only models passing the promotion gate serve signals."
          />
        </Panel>
      )}

      {!!models.data?.length && (
        <Panel title="Registered models" bodyClassName="p-0">
          <div className="w-full max-w-full overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Name</th><th>Version</th><th>Algorithm</th><th>Status</th>
                  <th>Horizon</th><th className="text-right">Training rows</th>
                  <th>Features</th><th>Trained</th><th>Promoted</th><th></th>
                </tr>
              </thead>
              <tbody>
                {models.data.map((model) => (
                  <tr key={model.id}>
                    <td className="font-medium">{model.name}</td>
                    <td className="font-mono">{model.version}</td>
                    <td className="text-ink-muted">{model.algorithm}</td>
                    <td>
                      <span className={`chip ${STATUS_STYLES[model.status] ?? STATUS_STYLES.ARCHIVED}`}>
                        {model.status}
                      </span>
                    </td>
                    <td className="font-mono tabular-nums">{model.horizon_days}d</td>
                    <td className="text-right font-mono tabular-nums">
                      {model.training_rows?.toLocaleString() ?? '--'}
                    </td>
                    <td className="font-mono text-2xs text-ink-muted">{model.feature_set_version}</td>
                    <td className="text-2xs text-ink-faint">{relativeTime(model.trained_at)}</td>
                    <td className="text-2xs text-ink-faint">
                      {model.promoted_at ? relativeTime(model.promoted_at) : '--'}
                    </td>
                    <td>
                      <button
                        className="text-2xs text-accent hover:underline"
                        onClick={() => setSelected(model.id === selected ? null : model.id)}
                      >
                        {selected === model.id ? 'Hide' : 'Inspect'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}

      {selected && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Panel title="Validation metrics">
            {detail.isLoading && <Loading />}
            {detail.data && (
              <>
                {Object.entries(detail.data.metrics).map(([split, values]) => (
                  <div key={split} className="mb-4 last:mb-0">
                    <h3 className="stat-label mb-2">
                      {split === 'cv_mean'
                        ? 'Walk-forward cross-validation (mean)'
                        : split === 'holdout'
                          ? 'Hold-out block (scored once)'
                          : split}
                    </h3>
                    <table className="data-table">
                      <tbody>
                        {KEY_METRICS.filter(([key]) => values[key] !== undefined).map(
                          ([key, description]) => (
                            <tr key={key}>
                              <td className="text-ink-muted">
                                {key.replace(/_/g, ' ')}
                                <div className="text-2xs text-ink-faint">{description}</div>
                              </td>
                              <td className="text-right font-mono tabular-nums">
                                {key === 'sample_size'
                                  ? values[key].toLocaleString()
                                  : values[key].toFixed(4)}
                              </td>
                            </tr>
                          ),
                        )}
                      </tbody>
                    </table>
                  </div>
                ))}
                <p className="mt-3 text-2xs text-ink-faint">{detail.data.disclaimer}</p>
                {detail.data.artifact_sha256 && (
                  <p className="mt-1 font-mono text-2xs text-ink-faint">
                    artefact sha256 {detail.data.artifact_sha256.slice(0, 16)}…
                  </p>
                )}
              </>
            )}
          </Panel>

          <Panel title="Feature drift (PSI)">
            {drift.isLoading && <Loading />}
            {drift.data?.note && <Empty message={drift.data.note} />}
            {drift.data && !drift.data.note && (
              <>
                <div className="mb-3 flex items-center gap-2">
                  <span
                    className={`chip ${drift.data.drift_detected ? 'bg-warn-soft text-warn' : 'bg-bull-soft text-bull'}`}
                  >
                    {drift.data.drift_detected ? 'DRIFT DETECTED' : 'STABLE'}
                  </span>
                  <span className="text-2xs text-ink-faint">
                    max PSI {drift.data.max_psi?.toFixed(3)} (&gt;0.25 is a significant shift)
                  </span>
                </div>
                <table className="data-table">
                  <thead>
                    <tr><th>Feature</th><th className="text-right">PSI</th></tr>
                  </thead>
                  <tbody>
                    {Object.entries(drift.data.feature_psi ?? {}).slice(0, 12).map(([feature, psi]) => (
                      <tr key={feature}>
                        <td className="font-mono text-2xs">{feature}</td>
                        <td
                          className={`text-right font-mono tabular-nums ${
                            psi > 0.25 ? 'text-warn' : 'text-ink-muted'
                          }`}
                        >
                          {psi.toFixed(4)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </Panel>
        </div>
      )}
    </div>
  );
}
