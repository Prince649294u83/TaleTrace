export default function Button({
  children,
  variant = 'primary',
  size = 'md',
  icon,
  iconOnly = false,
  block = false,
  loading = false,
  className = '',
  ...rest
}) {
  const classes = [
    'btn',
    `btn-${variant}`,
    size === 'sm' ? 'btn-sm' : '',
    block ? 'btn-block' : '',
    iconOnly ? 'btn-icon' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <button className={classes} disabled={loading || rest.disabled} {...rest}>
      {loading ? <span className="spinner" style={{ width: 14, height: 14 }} /> : icon}
      {!iconOnly && children}
    </button>
  );
}
