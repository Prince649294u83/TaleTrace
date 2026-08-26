import { useState } from 'react';
import { api } from '../../services/api';
import Button from '../ui/Button';

const PRESETS = ['1x', '1.5x', '2x'];

export default function SpeedSelector({ userId, onComplete }) {
  const [mode, setMode] = useState('preset');
  const [preset, setPreset] = useState('1x');
  const [paragraph, setParagraph] = useState(null);
  const [testDone, setTestDone] = useState(false);
  const [wpm, setWpm] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  // When the passage went on screen. The backend measures wpm from this and its
  // own word count — it never trusts a wpm the browser calculated, and it cannot
  // calculate one at all without the elapsed time.
  const [startedAt, setStartedAt] = useState(null);

  const loadParagraph = async () => {
    setMode('test');
    setError(null);
    if (!paragraph) {
      const res = await api.getReadingTestParagraph();
      setParagraph(res.paragraph);
    }
    // Restarted on every visit to the tab, not just the first: someone who
    // switches away to Preset and back has not been reading in the meantime.
    setStartedAt(Date.now());
  };

  const finishTest = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.submitReadingTestCompleted(userId, Date.now() - startedAt);
      setWpm(res.wpm);
      setTestDone(true);
    } catch (e) {
      // The backend refuses a timing it cannot believe rather than recording a
      // baseline that would be wrong for every session afterwards. Retrying is
      // the fix, so the passage stays on screen and the clock restarts.
      setError(e.message);
      setStartedAt(Date.now());
    }
    setLoading(false);
  };

  const confirmPreset = async () => {
    setLoading(true);
    setError(null);
    try {
      await api.submitReadingSpeedPreset(userId, preset);
      onComplete();
    } catch (e) {
      setError(e.message);
    }
    setLoading(false);
  };

  return (
    <div className="stack" style={{ gap: 18 }}>
      <div className="speed-tabs">
        <button
          className={`speed-tab ${mode === 'preset' ? 'speed-tab-active' : ''}`}
          onClick={() => setMode('preset')}
          type="button"
        >
          Preset Speed
        </button>
        <button
          className={`speed-tab ${mode === 'test' ? 'speed-tab-active' : ''}`}
          onClick={loadParagraph}
          type="button"
        >
          Reading Test
        </button>
      </div>

      {error && <div className="auth-error">{error}</div>}

      {mode === 'preset' && (
        <>
          <div className="preset-grid">
            {PRESETS.map((p) => (
              <button
                key={p}
                type="button"
                className={`preset-option ${preset === p ? 'preset-option-active' : ''}`}
                onClick={() => setPreset(p)}
              >
                {p}
              </button>
            ))}
          </div>
          <Button block onClick={confirmPreset} loading={loading}>
            Continue
          </Button>
        </>
      )}

      {mode === 'test' && !testDone && (
        <>
          {paragraph ? <div className="read-passage">{paragraph}</div> : <div className="text-muted">Loading passage…</div>}
          <Button block onClick={finishTest} loading={loading} disabled={!paragraph}>
            I&apos;m Finished
          </Button>
        </>
      )}

      {mode === 'test' && testDone && (
        <>
          <div className="wpm-result">
            <div className="value">{wpm} WPM</div>
            <div className="label">Your reading speed</div>
          </div>
          <Button block onClick={onComplete}>
            Continue
          </Button>
        </>
      )}
    </div>
  );
}
