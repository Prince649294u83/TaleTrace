import {
  ResponsiveContainer,
  LineChart,
  Line,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from 'recharts';

const DIFFICULTY_LABELS = { 1: 'Easy', 2: 'Medium', 3: 'Hard' };

function CustomTooltip({ active, payload, label, unit }) {
  if (!active || !payload || !payload.length) return null;
  const entry = payload[0];
  const display = entry.payload.difficultyLabel || `${entry.value}${unit || ''}`;
  return (
    <div
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--border)',
        borderRadius: 8,
        padding: '8px 12px',
        fontSize: 12.5,
        boxShadow: 'var(--shadow)',
      }}
    >
      <div style={{ color: 'var(--text-muted)', marginBottom: 2 }}>{label}</div>
      <div style={{ fontWeight: 700 }}>{display}</div>
    </div>
  );
}

export default function AnalyticsChart({
  type = 'line',
  data,
  dataKey,
  xKey = 'date',
  color = 'var(--accent)',
  unit = '',
  isDifficulty = false,
}) {
  const axisStyle = { fontSize: 11, fill: 'var(--text-faint)' };
  const yWidth = isDifficulty ? 58 : 38;

  return (
    <div className="chart-wrap">
      <ResponsiveContainer width="100%" height="100%">
        {type === 'line' ? (
          <LineChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 5" stroke="var(--border)" vertical={false} />
            <XAxis dataKey={xKey} tick={axisStyle} axisLine={{ stroke: 'var(--border)' }} tickLine={false} />
            <YAxis
              tick={axisStyle}
              axisLine={false}
              tickLine={false}
              domain={isDifficulty ? [1, 3] : ['auto', 'auto']}
              ticks={isDifficulty ? [1, 2, 3] : undefined}
              tickFormatter={isDifficulty ? (v) => DIFFICULTY_LABELS[v] : undefined}
              width={yWidth}
            />
            <Tooltip content={<CustomTooltip unit={unit} />} cursor={{ stroke: 'var(--border)' }} />
            <Line type="monotone" dataKey={dataKey} stroke={color} strokeWidth={2.25} dot={{ r: 3, fill: color }} activeDot={{ r: 5 }} />
          </LineChart>
        ) : (
          <BarChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 5" stroke="var(--border)" vertical={false} />
            <XAxis dataKey={xKey} tick={axisStyle} axisLine={{ stroke: 'var(--border)' }} tickLine={false} />
            <YAxis tick={axisStyle} axisLine={false} tickLine={false} width={yWidth} allowDecimals={false} />
            <Tooltip content={<CustomTooltip unit={unit} />} cursor={{ fill: 'var(--surface-2)' }} />
            <Bar dataKey={dataKey} fill={color} radius={[4, 4, 0, 0]} maxBarSize={34} />
          </BarChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}
