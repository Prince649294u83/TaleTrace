const OPTIONS = [
  { value: 'today', label: 'Today' },
  { value: 'week', label: 'Last 7 Days' },
  { value: 'month', label: 'Last 30 Days' },
  { value: 'all', label: 'All Time' },
];

export default function TimeFilter({ value, onChange }) {
  return (
    <div className="time-filter">
      {OPTIONS.map((opt) => (
        <button
          key={opt.value}
          type="button"
          className={`time-filter-btn ${value === opt.value ? 'time-filter-btn-active' : ''}`}
          onClick={() => onChange(opt.value)}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}
