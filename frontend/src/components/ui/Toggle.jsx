export default function Toggle({ checked, onChange, label, description, disabled = false }) {
  return (
    <div className="row-between">
      {(label || description) && (
        <div>
          {label && <div style={{ fontSize: 14, fontWeight: 600 }}>{label}</div>}
          {description && <div className="text-muted" style={{ fontSize: 12.5, marginTop: 2 }}>{description}</div>}
        </div>
      )}
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={`toggle ${checked ? 'toggle-on' : ''}`}
      >
        <span className="toggle-knob" />
      </button>
    </div>
  );
}
