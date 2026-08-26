import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { FolderPlus, FolderClosed, Trash2 } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import { api } from '../services/api';
import Card from '../components/ui/Card';
import Button from '../components/ui/Button';
import Modal from '../components/ui/Modal';
import LoadingState from '../components/ui/LoadingState';
import ErrorState from '../components/ui/ErrorState';
import EmptyState from '../components/ui/EmptyState';
import FolderItem from '../components/sessions/FolderItem';
import SessionItem from '../components/sessions/SessionItem';
import CreateFolderModal from '../components/sessions/CreateFolderModal';

export default function Sessions() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [status, setStatus] = useState('loading');
  const [folders, setFolders] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [showCreateFolder, setShowCreateFolder] = useState(false);
  const [draggedId, setDraggedId] = useState(null);
  const [dragOverUnorganized, setDragOverUnorganized] = useState(false);
  const [confirmTarget, setConfirmTarget] = useState(null); // { type, id, label }

  const load = useCallback(async () => {
    setStatus('loading');
    try {
      const res = await api.getSessions(user.id);
      setFolders(res.folders);
      setSessions(res.sessions);
      setStatus('ready');
    } catch {
      setStatus('error');
    }
  }, [user.id]);

  useEffect(() => {
    load();
  }, [load]);

  const createFolder = async (name) => {
    const folder = await api.createFolder(user.id, name);
    setFolders((f) => [...f, folder]);
    setShowCreateFolder(false);
  };

  const renameFolder = async (folderId, name) => {
    setFolders((f) => f.map((x) => (x.id === folderId ? { ...x, name } : x)));
    await api.renameFolder(user.id, folderId, name);
  };

  const renameSession = async (sessionId, name) => {
    setSessions((s) => s.map((x) => (x.id === sessionId ? { ...x, name } : x)));
    await api.renameSession(user.id, sessionId, name);
  };

  const moveSession = async (sessionId, folderId) => {
    setSessions((s) => s.map((x) => (x.id === sessionId ? { ...x, folderId } : x)));
    await api.moveSession(user.id, sessionId, folderId);
  };

  const confirmDelete = async () => {
    if (!confirmTarget) return;
    if (confirmTarget.type === 'folder') {
      await api.deleteFolder(user.id, confirmTarget.id);
      setFolders((f) => f.filter((x) => x.id !== confirmTarget.id));
      setSessions((s) => s.map((x) => (x.folderId === confirmTarget.id ? { ...x, folderId: null } : x)));
    } else {
      await api.deleteSession(user.id, confirmTarget.id);
      setSessions((s) => s.filter((x) => x.id !== confirmTarget.id));
    }
    setConfirmTarget(null);
  };

  if (status === 'loading') {
    return (
      <Card>
        <LoadingState />
      </Card>
    );
  }
  if (status === 'error') {
    return (
      <Card>
        <ErrorState onRetry={load} />
      </Card>
    );
  }

  const unorganized = sessions.filter((s) => !s.folderId);

  return (
    <>
      <div className="row-between" style={{ flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h1 className="page-title">Sessions</h1>
          <p className="page-subtitle">Organize your reading sessions into folders.</p>
        </div>
        <Button icon={<FolderPlus />} onClick={() => setShowCreateFolder(true)}>
          New Folder
        </Button>
      </div>

      {sessions.length === 0 && folders.length === 0 ? (
        <Card>
          <EmptyState
            icon={<FolderClosed size={20} />}
            title="No reading sessions yet."
            description="Start reading using TaleTrace to begin tracking your progress."
          />
        </Card>
      ) : (
        <div className="stack">
          {folders.map((folder) => (
            <FolderItem
              key={folder.id}
              folder={folder}
              sessions={sessions.filter((s) => s.folderId === folder.id)}
              onRenameFolder={(name) => renameFolder(folder.id, name)}
              onDeleteFolder={() => setConfirmTarget({ type: 'folder', id: folder.id, label: folder.name })}
              onRenameSession={renameSession}
              onDeleteSession={(s) => setConfirmTarget({ type: 'session', id: s.id, label: s.name })}
              onOpenSession={(s) => navigate(`/sessions/${s.id}`)}
              onDropSession={moveSession}
              draggedId={draggedId}
              setDraggedId={setDraggedId}
            />
          ))}

          <div>
            <div className="section-divider-label">Unorganized</div>
            <div
              className={`unorganized-block ${dragOverUnorganized ? 'drag-over' : ''}`}
              onDragOver={(e) => {
                e.preventDefault();
                setDragOverUnorganized(true);
              }}
              onDragLeave={() => setDragOverUnorganized(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragOverUnorganized(false);
                if (draggedId) moveSession(draggedId, null);
              }}
            >
              {unorganized.length === 0 && (
                <div className="session-row text-faint" style={{ fontSize: 13, paddingLeft: 14 }}>
                  Drag a session here to remove it from its folder.
                </div>
              )}
              {unorganized.map((s) => (
                <SessionItem
                  key={s.id}
                  session={s}
                  onOpen={(sess) => navigate(`/sessions/${sess.id}`)}
                  onRename={(name) => renameSession(s.id, name)}
                  onDelete={() => setConfirmTarget({ type: 'session', id: s.id, label: s.name })}
                  dragging={draggedId === s.id}
                  dragHandlers={{
                    draggable: true,
                    onDragStart: () => setDraggedId(s.id),
                    onDragEnd: () => setDraggedId(null),
                  }}
                />
              ))}
            </div>
          </div>
        </div>
      )}

      <CreateFolderModal open={showCreateFolder} onClose={() => setShowCreateFolder(false)} onCreate={createFolder} />

      <Modal
        open={!!confirmTarget}
        onClose={() => setConfirmTarget(null)}
        title={confirmTarget?.type === 'folder' ? 'Delete Folder' : 'Delete Session'}
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirmTarget(null)}>
              Cancel
            </Button>
            <Button variant="danger" icon={<Trash2 size={14} />} onClick={confirmDelete}>
              Delete
            </Button>
          </>
        }
      >
        <p className="text-muted" style={{ fontSize: 13.5 }}>
          {confirmTarget?.type === 'folder'
            ? `"${confirmTarget?.label}" will be deleted. Sessions inside it will move to Unorganized.`
            : `"${confirmTarget?.label}" will be permanently deleted.`}
        </p>
      </Modal>
    </>
  );
}
