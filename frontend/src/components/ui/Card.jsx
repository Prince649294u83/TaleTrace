export default function Card({ children, tight = false, hover = false, className = '', ...rest }) {
  const classes = ['card', tight ? 'card-tight' : '', hover ? 'card-hover' : '', className]
    .filter(Boolean)
    .join(' ');
  return (
    <div className={classes} {...rest}>
      {children}
    </div>
  );
}
