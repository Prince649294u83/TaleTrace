import { useState } from 'react';
import { FileText, Pencil, Trash2 } from 'lucide-react';
import Dropdown from '../ui/Dropdown';

export default function SessionItem({ session, onOpen, onRename, onDelete, dragging, dragHandlers }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(session.name);

  const submit = () => {
    setEditing(false);
    const trimmed = value.trim();
    if (trimmed && trimmed !== session.name) onRename(trimmed);
    else setValue(session.name);
  };

  return (
    <div className={`session-row ${dragging ? 'session-row-dragging' : ''}`} {...dragHandlers}>
      <FileText size={14} style={{ color: 'var(--text-faint)', flexShrink: 0 }} />
      {editing ? (
        <input
          autoFocus
          className="input rename-input"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onBlur={submit}
          onKeyDown={(e) => e.key === 'Enter' && submit()}
        />
      ) : (
        <span className="session-name" onClick={() => onOpen(session)}>
          {session.name}
        </span>
      )}
      <span className="session-date">{session.date}</span>
      <Dropdown
        items={[
          { label: 'Rename', icon: <Pencil size={13} />, onClick: () => setEditing(true) },
          { label: 'Delete', icon: <Trash2 size={13} />, onClick: () => onDelete(session), danger: true },
        ]}
      />
    </div>
  );
}
