export default function EmptyState({ icon, title, description, action }) {
  return (
    <div className="state-block">
      {icon && <div className="state-icon chip-accent">{icon}</div>}
      {title && <div className="state-title">{title}</div>}
      {description && <div className="state-desc">{description}</div>}
      {action}
    </div>
  );
}
