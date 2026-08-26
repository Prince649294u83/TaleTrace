import { useState } from 'react';
import { Sun, Moon, Check, Gauge } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import { useTheme } from '../context/ThemeContext';
import { api } from '../services/api';
import Card from '../components/ui/Card';
import Input from '../components/ui/Input';
import Toggle from '../components/ui/Toggle';
import Button from '../components/ui/Button';
import Modal from '../components/ui/Modal';
import ReaderTypeSelector from '../components/onboarding/ReaderTypeSelector';
import SpeedSelector from '../components/onboarding/SpeedSelector';

const DEVICE_FEATURES = [
  { key: 'textToSpeech', label: 'Text-to-Speech', description: 'Have TaleTrace read pages aloud.' },
  { key: 'ambientMusic', label: 'Ambient Music', description: 'Play background music while you read.' },
  { key: 'readOutMeaning', label: 'Read Out Meaning', description: 'Speak word meanings aloud on lookup.' },
  { key: 'displayMeaning', label: 'Display Meaning', description: 'Show word meanings on the OLED screen.' },
];

export default function Settings() {
  const { user, refreshUser } = useAuth();
  const { theme, setTheme } = useTheme();

  const [form, setForm] = useState({ name: user.name, email: user.email, age: user.age });
  const [prefs, setPrefs] = useState(user.devicePrefs);
  const [readerType, setReaderType] = useState(user.readerType);
  const [speedModalOpen, setSpeedModalOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const save = async () => {
    setSaving(true);
    setSaved(false);
    const updated = await api.updateSettings(user.id, {
      name: form.name,
      email: form.email,
      age: Number(form.age),
      devicePrefs: prefs,
      readerType,
      theme,
    });
    refreshUser(updated);
    setSaving(false);
    setSaved(true);
    setTimeout(() => setSaved(false), 2500);
  };

  const closeSpeedModal = async () => {
    setSpeedModalOpen(false);
    const fresh = await api.getMe();
    refreshUser(fresh);
  };

  return (
    <>
      <div>
        <h1 className="page-title">Settings</h1>
        <p className="page-subtitle">Everything you configured during setup, all in one place.</p>
      </div>

      <div className="stack">
        <Card>
          <div className="settings-section">
            <div className="section-title">User Details</div>
            <div className="settings-fields">
              <Input label="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
              <Input label="Email" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
              <Input label="Age" type="number" value={form.age} onChange={(e) => setForm({ ...form, age: e.target.value })} />
            </div>
          </div>
        </Card>

        <Card>
          <div className="settings-section">
            <div className="row-between">
              <div className="section-title">Reading Speed</div>
              <Button variant="secondary" size="sm" icon={<Gauge size={13} />} onClick={() => setSpeedModalOpen(true)}>
                Update
              </Button>
            </div>
            <div className="row" style={{ gap: 10 }}>
              <span className="stat-tile-value" style={{ fontSize: 20 }}>
                {user.wpm} WPM
              </span>
              {user.readingSpeedPreset && <span className="badge badge-accent">{user.readingSpeedPreset} preset</span>}
            </div>
          </div>
        </Card>

        <Card>
          <div className="settings-section">
            <div className="section-title">Device Features</div>
            {DEVICE_FEATURES.map((f) => (
              <Toggle
                key={f.key}
                label={f.label}
                description={f.description}
                checked={prefs[f.key]}
                onChange={(val) => setPrefs({ ...prefs, [f.key]: val })}
              />
            ))}
          </div>
        </Card>

        <Card>
          <div className="settings-section">
            <div className="section-title">Reader Type</div>
            <ReaderTypeSelector value={readerType} onChange={setReaderType} />
          </div>
        </Card>

        <Card>
          <div className="settings-section">
            <div className="section-title">Appearance</div>
            <div className="appearance-grid">
              <button
                type="button"
                className={`appearance-option ${theme === 'light' ? 'appearance-option-active' : ''}`}
                onClick={() => setTheme('light')}
              >
                <Sun size={16} /> Light Mode
              </button>
              <button
                type="button"
                className={`appearance-option ${theme === 'dark' ? 'appearance-option-active' : ''}`}
                onClick={() => setTheme('dark')}
              >
                <Moon size={16} /> Dark Mode
              </button>
            </div>
          </div>
        </Card>

        <div className="save-bar">
          {saved && (
            <span className="badge badge-success">
              <Check size={12} /> Saved
            </span>
          )}
          <Button onClick={save} loading={saving}>
            Save Changes
          </Button>
        </div>
      </div>

      <Modal open={speedModalOpen} onClose={closeSpeedModal} title="Update Reading Speed">
        <SpeedSelector userId={user.id} onComplete={closeSpeedModal} />
      </Modal>
    </>
  );
}
