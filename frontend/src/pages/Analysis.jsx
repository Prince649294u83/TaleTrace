import { useCallback, useEffect, useState } from 'react';
import { LineChart as LineChartIcon } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import { api } from '../services/api';
import TimeFilter from '../components/analytics/TimeFilter';
import AnalyticsChart from '../components/analytics/AnalyticsChart';
import Card from '../components/ui/Card';
import LoadingState from '../components/ui/LoadingState';
import ErrorState from '../components/ui/ErrorState';
import EmptyState from '../components/ui/EmptyState';

export default function Analysis() {
  const { user } = useAuth();
  const [range, setRange] = useState('week');
  const [status, setStatus] = useState('loading');
  const [data, setData] = useState(null);

  const load = useCallback(
    async (r) => {
      setStatus('loading');
      try {
        const res = await api.getAnalysis(user.id, r);
        setData(res);
        setStatus('ready');
      } catch {
        setStatus('error');
      }
    },
    [user.id]
  );

  useEffect(() => {
    load(range);
  }, [range, load]);

  return (
    <>
      <div className="row-between" style={{ flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h1 className="page-title">Analysis</h1>
          <p className="page-subtitle">Reading analytics from your TaleTrace device.</p>
        </div>
        <TimeFilter value={range} onChange={setRange} />
      </div>

      {status === 'loading' && (
        <Card>
          <LoadingState />
        </Card>
      )}

      {status === 'error' && (
        <Card>
          <ErrorState onRetry={() => load(range)} />
        </Card>
      )}

      {status === 'ready' && data.empty && (
        <Card>
          <EmptyState
            icon={<LineChartIcon size={20} />}
            title="No analytics yet."
            description="Once you've read a few sessions on TaleTrace, your trends will show up here."
          />
        </Card>
      )}

      {status === 'ready' && !data.empty && (
        <div className="stack">
          <Card>
            <div className="chart-card-head">
              <div className="section-title">Reading Time per Day</div>
              <div className="text-muted" style={{ fontSize: 12.5 }}>Minutes spent reading each day</div>
            </div>
            <AnalyticsChart type="bar" data={data.readingTimePerDay} dataKey="minutes" color="var(--accent)" unit=" min" />
          </Card>

          <Card>
            <div className="chart-card-head">
              <div className="section-title">Pages Read per Day</div>
              <div className="text-muted" style={{ fontSize: 12.5 }}>Total pages completed each day</div>
            </div>
            <AnalyticsChart type="bar" data={data.pagesPerDay} dataKey="pages" color="var(--info)" unit=" pages" />
          </Card>

          <div className="analysis-row">
            <Card>
              <div className="chart-card-head">
                <div className="section-title">Meaning Lookups per Day</div>
              </div>
              <AnalyticsChart type="line" data={data.lookupsPerDay} dataKey="lookups" color="var(--warning)" unit=" lookups" />
            </Card>
            <Card>
              <div className="chart-card-head">
                <div className="section-title">Reading Speed Trend</div>
              </div>
              <AnalyticsChart type="line" data={data.speedTrend} dataKey="wpm" color="var(--success)" unit=" WPM" />
            </Card>
          </div>

          <Card>
            <div className="chart-card-head">
              <div className="section-title">Reading Difficulty Trend</div>
              <div className="text-muted" style={{ fontSize: 12.5 }}>Average difficulty of what you read each day</div>
            </div>
            <AnalyticsChart type="line" data={data.difficultyTrend} dataKey="difficulty" color="var(--danger)" isDifficulty />
          </Card>
        </div>
      )}
    </>
  );
}
