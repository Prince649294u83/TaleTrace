export default function Input({ label, error, id, ...rest }) {
  const inputId = id || rest.name;
  return (
    <div className="field">
      {label && <label htmlFor={inputId}>{label}</label>}
      <input id={inputId} className={`input ${error ? 'input-error' : ''}`} {...rest} />
      {error && <span className="field-error">{error}</span>}
    </div>
  );
}
