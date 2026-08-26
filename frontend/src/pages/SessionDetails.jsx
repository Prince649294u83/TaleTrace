import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, Clock, BookOpenCheck, FileText, Gauge } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import { api } from '../services/api';
import Card from '../components/ui/Card';
import LoadingState from '../components/ui/LoadingState';
import ErrorState from '../components/ui/ErrorState';

const DIFFICULTY_VARIANT = { Easy: 'success', Medium: 'warning', Hard: 'danger' };

export default function SessionDetails() {
  const { user } = useAuth();
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const [status, setStatus] = useState('loading');
  const [session, setSession] = useState(null);

  const load = useCallback(async () => {
    setStatus('loading');
    try {
      const res = await api.getSessionDetails(user.id, sessionId);
      setSession(res);
      setStatus('ready');
    } catch {
      setStatus('error');
    }
  }, [user.id, sessionId]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <>
      <button className="btn btn-ghost btn-sm" style={{ width: 'fit-content' }} onClick={() => navigate('/sessions')}>
        <ArrowLeft size={14} /> Back to Sessions
      </button>

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

      {status === 'ready' && (
        <>
          <div>
            <h1 className="page-title">{session.name}</h1>
            <p className="page-subtitle">
              {session.date} &bull; {session.time}
            </p>
          </div>

          <div className="grid grid-3">
            <Card className="stat-tile">
              <span className="chip chip-accent">
                <Clock size={16} />
              </span>
              <div>
                <div className="stat-tile-value">{session.readingTimeMin} min</div>
                <div className="stat-tile-label">Reading Time</div>
              </div>
            </Card>
            <Card className="stat-tile">
              <span className="chip chip-info">
                <BookOpenCheck size={16} />
              </span>
              <div>
                <div className="stat-tile-value">{session.pagesRead}</div>
                <div className="stat-tile-label">Pages Read</div>
              </div>
            </Card>
            <Card className="stat-tile">
              <span className="chip chip-info">
                <FileText size={16} />
              </span>
              <div>
                {/* Words the reader covered, not words the narrator said out
                    loud. Those are two different numbers and the Audio Engine
                    counts the second one; a session listened to while pointing
                    at four words still covered a page. */}
                <div className="stat-tile-value">{session.wordsRead.toLocaleString()}</div>
                <div className="stat-tile-label">Words Read</div>
              </div>
            </Card>
            <Card className="stat-tile">
              <span className="chip chip-accent">
                <Gauge size={16} />
              </span>
              <div>
                {/* Withheld rather than shown as zero when the reading clock was
                    too short to divide by — the backend returns 0 for "not
                    measurable", and "0 WPM" would read as a finding. */}
                <div className="stat-tile-value">{session.wpm > 0 ? `${session.wpm} WPM` : '—'}</div>
                <div className="stat-tile-label">Reading Speed</div>
              </div>
            </Card>
            <Card className="stat-tile">
              <span className={`chip chip-${DIFFICULTY_VARIANT[session.difficulty] || 'warning'}`}>
                <Gauge size={16} />
              </span>
              <div>
                {/* No difficulty is a real answer, not missing data: the backend
                    declines to rate a session it only saw read slowly, because a
                    reader who set the book down looks exactly like one who
                    struggled. An em-dash says that; "Medium" would invent it. */}
                <div className="stat-tile-value">{session.difficulty ?? '—'}</div>
                <div className="stat-tile-label">Reading Difficulty</div>
              </div>
            </Card>
          </div>

          <Card>
            <div className="section-title" style={{ marginBottom: 10 }}>
              AI Summary
            </div>
            {session.summary ? (
              <p style={{ fontSize: 14, lineHeight: 1.7, color: 'var(--text)' }}>{session.summary}</p>
            ) : (
              <p className="text-muted" style={{ fontSize: 14, lineHeight: 1.7 }}>
                No words were looked up during this session, so there was nothing to summarise.
              </p>
            )}
          </Card>
        </>
      )}
    </>
  );
}
