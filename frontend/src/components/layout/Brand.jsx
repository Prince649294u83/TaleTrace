import { BookMarked } from 'lucide-react';

export default function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark">
        <BookMarked strokeWidth={2.4} />
      </span>
      <span className="brand-name">TaleTrace</span>
    </div>
  );
}
