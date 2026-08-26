import { useEffect, useRef, useState } from 'react';
import { MoreVertical } from 'lucide-react';

export default function Dropdown({ trigger, items }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    window.addEventListener('mousedown', onClick);
    return () => window.removeEventListener('mousedown', onClick);
  }, [open]);

  return (
    <div className="dropdown" ref={ref}>
      <button
        type="button"
        className="btn btn-ghost btn-icon"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((o) => !o);
        }}
        aria-label="More actions"
      >
        {trigger || <MoreVertical />}
      </button>
      {open && (
        <div className="dropdown-menu" onClick={(e) => e.stopPropagation()}>
          {items.map((item, i) => (
            <button
              key={i}
              className={`dropdown-item ${item.danger ? 'dropdown-item-danger' : ''}`}
              onClick={() => {
                setOpen(false);
                item.onClick();
              }}
            >
              {item.icon}
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
