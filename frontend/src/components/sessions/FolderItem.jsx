import { useState } from 'react';
import { ChevronRight, Folder, Pencil, Trash2 } from 'lucide-react';
import Dropdown from '../ui/Dropdown';
import SessionItem from './SessionItem';

export default function FolderItem({
  folder,
  sessions,
  onRenameFolder,
  onDeleteFolder,
  onRenameSession,
  onDeleteSession,
  onOpenSession,
  onDropSession,
  draggedId,
  setDraggedId,
}) {
  const [open, setOpen] = useState(true);
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(folder.name);
  const [dragOver, setDragOver] = useState(false);

  const submit = () => {
    setEditing(false);
    const trimmed = value.trim();
    if (trimmed && trimmed !== folder.name) onRenameFolder(trimmed);
    else setValue(folder.name);
  };

  return (
    <div
      className={`folder-block ${dragOver ? 'drag-over' : ''}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        if (draggedId) onDropSession(draggedId, folder.id);
      }}
    >
      <div className="folder-header" onClick={() => !editing && setOpen((o) => !o)}>
        <ChevronRight className={`chevron ${open ? 'chevron-open' : ''}`} />
        <Folder size={15} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
        {editing ? (
          <input
            autoFocus
            className="input rename-input"
            value={value}
            onClick={(e) => e.stopPropagation()}
            onChange={(e) => setValue(e.target.value)}
            onBlur={submit}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
          />
        ) : (
          <span className="folder-name">{folder.name}</span>
        )}
        <span className="text-faint mono" style={{ fontSize: 11.5 }}>{sessions.length}</span>
        <div onClick={(e) => e.stopPropagation()}>
          <Dropdown
            items={[
              { label: 'Rename', icon: <Pencil size={13} />, onClick: () => setEditing(true) },
              { label: 'Delete', icon: <Trash2 size={13} />, onClick: onDeleteFolder, danger: true },
            ]}
          />
        </div>
      </div>
      {open && (
        <div className="session-list">
          {sessions.length === 0 && (
            <div className="session-row text-faint" style={{ fontSize: 13 }}>
              Drag sessions here to file them under {folder.name}.
            </div>
          )}
          {sessions.map((s) => (
            <SessionItem
              key={s.id}
              session={s}
              onOpen={onOpenSession}
              onRename={(name) => onRenameSession(s.id, name)}
              onDelete={() => onDeleteSession(s)}
              dragging={draggedId === s.id}
              dragHandlers={{
                draggable: true,
                onDragStart: () => setDraggedId(s.id),
                onDragEnd: () => setDraggedId(null),
              }}
            />
          ))}
        </div>
      )}
    </div>
  );
}
