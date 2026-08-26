export default function LoadingState({ label = 'Loading your reading data\u2026' }) {
  return (
    <div className="state-block">
      <span className="spinner" />
      <span className="state-desc">{label}</span>
    </div>
  );
}
