export default function Skeleton({ height = 16, width = '100%', radius = 6, style = {} }) {
  return (
    <div
      style={{
        height,
        width,
        borderRadius: radius,
        background: 'var(--surface-2)',
        animation: 'pulse 1.4s ease-in-out infinite',
        ...style,
      }}
    />
  );
}
