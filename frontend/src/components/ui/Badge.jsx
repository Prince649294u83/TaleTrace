export default function Badge({ children, variant = 'muted', icon }) {
  return (
    <span className={`badge badge-${variant}`}>
      {icon}
      {children}
    </span>
  );
}
