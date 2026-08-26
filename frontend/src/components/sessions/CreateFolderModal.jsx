import { useState } from 'react';
import Modal from '../ui/Modal';
import Input from '../ui/Input';
import Button from '../ui/Button';

export default function CreateFolderModal({ open, onClose, onCreate }) {
  const [name, setName] = useState('');
  const [saving, setSaving] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!name.trim()) return;
    setSaving(true);
    await onCreate(name.trim());
    setSaving(false);
    setName('');
  };

  return (
    <Modal open={open} onClose={onClose} title="Create Folder">
      <form onSubmit={submit} className="stack" style={{ gap: 16 }}>
        <Input
          label="Folder Name"
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Biology"
        />
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" loading={saving} disabled={!name.trim()}>
            Create
          </Button>
        </div>
      </form>
    </Modal>
  );
}
