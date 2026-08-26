import { useEffect } from 'react';

export default function Modal({ open, onClose, title, children, footer }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="modal-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal-box">
        {title && <h3 className="section-title">{title}</h3>}
        {children}
        {footer && <div className="row" style={{ justifyContent: 'flex-end', marginTop: 4 }}>{footer}</div>}
      </div>
    </div>
  );
}
