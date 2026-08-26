import { AlertTriangle } from 'lucide-react';
import Button from './Button';

export default function ErrorState({
  title = 'Something went wrong.',
  description = "We couldn't load your reading data.",
  onRetry,
}) {
  return (
    <div className="state-block">
      <div className="state-icon chip-danger">
        <AlertTriangle size={20} />
      </div>
      <div className="state-title">{title}</div>
      <div className="state-desc">{description}</div>
      {onRetry && (
        <Button variant="secondary" size="sm" onClick={onRetry}>
          Try Again
        </Button>
      )}
    </div>
  );
}
