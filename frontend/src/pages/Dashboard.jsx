import { useCallback, useEffect, useState } from 'react';
import { Wifi, WifiOff, Gauge, BookOpenCheck, Search, BookMarked } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import { api } from '../services/api';
import Card from '../components/ui/Card';
import LoadingState from '../components/ui/LoadingState';
import ErrorState from '../components/ui/ErrorState';
import EmptyState from '../components/ui/EmptyState';

function StatCard({ icon, chipVariant, label, value }) {
  return (
    <Card className="stat-tile">
      <span className={`chip chip-${chipVariant}`}>{icon}</span>
      <div>
        <div className="stat-tile-value">{value}</div>
        <div className="stat-tile-label">{label}</div>
      </div>
    </Card>
  );
}

function DeviceCard({ status }) {
  const connected = status === 'connected';
  return (
    <StatCard
      icon={connected ? <Wifi size={16} /> : <WifiOff size={16} />}
      chipVariant={connected ? 'success' : 'danger'}
      label="Device Status"
      value={connected ? 'Connected' : 'Disconnected'}
    />
  );
}

export default function Dashboard() {
  const { user } = useAuth();
  const [status, setStatus] = useState('loading');
  const [data, setData] = useState(null);

  const load = useCallback(async () => {
    setStatus('loading');
    try {
      const res = await api.getDashboard(user.id);
      setData(res);
      setStatus('ready');
    } catch {
      setStatus('error');
    }
  }, [user.id]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <>
      <div>
        <h1 className="page-title">Welcome back, {user.name}</h1>
        <p className="page-subtitle">Here&apos;s where your reading stands today.</p>
      </div>

      {status === 'loading' && (
        <Card>
          <LoadingState />
        </Card>
      )}

      {status === 'error' && (
        <Card>
          <ErrorState onRetry={load} />
        </Card>
      )}

      {/* Whether the rig is on the network is device truth, not session truth. It
          shows before the first session too, because that is exactly when someone
          is most likely to be asking whether the thing is plugged in. */}
      {status === 'ready' && data.empty && (
        <>
          <div className="grid grid-4">
            <DeviceCard status={data.deviceStatus} />
          </div>
          <Card>
            <EmptyState
              icon={<BookMarked size={20} />}
              title="No reading sessions yet."
              description="Start reading using TaleTrace to begin tracking your progress."
            />
          </Card>
        </>
      )}

      {status === 'ready' && !data.empty && (
        <div className="grid grid-4">
          <DeviceCard status={data.deviceStatus} />
          <StatCard icon={<Gauge size={16} />} chipVariant="accent" label="Reading Speed" value={`${data.wpm} WPM`} />
          <StatCard icon={<BookOpenCheck size={16} />} chipVariant="info" label="Pages Read Today" value={data.pagesToday} />
          <StatCard icon={<Search size={16} />} chipVariant="warning" label="Meaning Lookups Today" value={data.lookupsToday} />
        </div>
      )}
    </>
  );
}
