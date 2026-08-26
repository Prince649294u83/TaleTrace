import { useEffect, useState } from 'react';
import { Folder, FileText } from 'lucide-react';
import { api } from '../../services/api';
import Checkbox from '../ui/Checkbox';
import LoadingState from '../ui/LoadingState';
import ErrorState from '../ui/ErrorState';
import EmptyState from '../ui/EmptyState';
import Card from '../ui/Card';

export default function SessionSelector({ userId, selected, onChange }) {
  const [status, setStatus] = useState('loading');
  const [folders, setFolders] = useState([]);
  const [sessions, setSessions] = useState([]);

  const load = async () => {
    setStatus('loading');
    try {
      const res = await api.getSessions(userId);
      setFolders(res.folders);
      setSessions(res.sessions);
      setStatus('ready');
    } catch {
      setStatus('error');
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId]);

  const toggleSession = (id) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange(next);
  };

  const toggleFolder = (folderId, sessionIds) => {
    const next = new Set(selected);
    const allSelected = sessionIds.every((id) => next.has(id));
    sessionIds.forEach((id) => (allSelected ? next.delete(id) : next.add(id)));
    onChange(next);
  };

  if (status === 'loading') {
    return (
      <Card>
        <LoadingState label="Loading your reading sessions…" />
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
  if (sessions.length === 0) {
    return (
      <Card>
        <EmptyState
          icon={<FileText size={20} />}
          title="No reading sessions yet."
          description="Read using TaleTrace to create sessions you can generate content from."
        />
      </Card>
    );
  }

  const unorganized = sessions.filter((s) => !s.folderId);

  return (
    <Card>
      <div className="selector-list">
        {folders.map((folder) => {
          const folderSessions = sessions.filter((s) => s.folderId === folder.id);
          if (folderSessions.length === 0) return null;
          const ids = folderSessions.map((s) => s.id);
          const allSelected = ids.every((id) => selected.has(id));
          const someSelected = ids.some((id) => selected.has(id));

          return (
            <div className="selector-folder" key={folder.id}>
              <div className="selector-folder-header" onClick={() => toggleFolder(folder.id, ids)}>
                <Checkbox checked={allSelected} indeterminate={someSelected && !allSelected} onChange={() => toggleFolder(folder.id, ids)} />
                <Folder size={15} style={{ color: 'var(--text-muted)' }} />
                <span className="folder-name">{folder.name}</span>
              </div>
              {folderSessions.map((s) => (
                <div className="selector-session-row" key={s.id} onClick={() => toggleSession(s.id)}>
                  <Checkbox checked={selected.has(s.id)} onChange={() => toggleSession(s.id)} />
                  <span className="session-name">{s.name}</span>
                  <span className="session-date">{s.date}</span>
                </div>
              ))}
            </div>
          );
        })}

        {unorganized.map((s) => (
          <div className="selector-standalone-row" key={s.id} onClick={() => toggleSession(s.id)}>
            <Checkbox checked={selected.has(s.id)} onChange={() => toggleSession(s.id)} />
            <FileText size={15} style={{ color: 'var(--text-muted)' }} />
            <span className="session-name">{s.name}</span>
            <span className="session-date">{s.date}</span>
          </div>
        ))}
      </div>

      <div className="selector-footer">
        <span className="text-muted" style={{ fontSize: 13 }}>
          {selected.size} selected
        </span>
      </div>
    </Card>
  );
}
