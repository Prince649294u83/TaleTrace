import { BookOpen, SpellCheck } from 'lucide-react';

const OPTIONS = [
  { value: 'normal', label: 'Normal Reader', icon: BookOpen },
  { value: 'dyslexic', label: 'Dyslexic Reader', icon: SpellCheck },
];

export default function ReaderTypeSelector({ value, onChange }) {
  return (
    <div className="reader-type-grid">
      {OPTIONS.map((opt) => (
        <button
          key={opt.value}
          type="button"
          className={`reader-type-option ${value === opt.value ? 'reader-type-option-active' : ''}`}
          onClick={() => onChange(opt.value)}
        >
          <opt.icon size={22} strokeWidth={1.8} />
          {opt.label}
        </button>
      ))}
    </div>
  );
}
