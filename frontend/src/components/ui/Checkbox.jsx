import { Check, Minus } from 'lucide-react';

export default function Checkbox({ checked, indeterminate = false, onChange, className = '' }) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={indeterminate ? 'mixed' : checked}
      onClick={(e) => {
        e.stopPropagation();
        onChange(!checked);
      }}
      className={`checkbox ${checked ? 'checkbox-checked' : ''} ${indeterminate ? 'checkbox-indeterminate' : ''} ${className}`}
    >
      {checked && !indeterminate && <Check strokeWidth={3} />}
      {indeterminate && <Minus strokeWidth={3} style={{ color: 'var(--accent)' }} />}
    </button>
  );
}
