export default function ProgressIndicator({ step, total = 3 }) {
  const dots = Array.from({ length: total }, (_, i) => i + 1);
  return (
    <div className="progress">
      {dots.map((d, i) => (
        <div key={d} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span
            className={`progress-dot ${d === step ? 'progress-dot-active' : d < step ? 'progress-dot-done' : ''}`}
          />
          {i < dots.length - 1 && <span className={`progress-line ${d < step ? 'progress-line-done' : ''}`} />}
        </div>
      ))}
    </div>
  );
}
