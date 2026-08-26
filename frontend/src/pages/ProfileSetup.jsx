import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { BookMarked } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import { api } from '../services/api';
import ProgressIndicator from '../components/onboarding/ProgressIndicator';
import SpeedSelector from '../components/onboarding/SpeedSelector';
import ReaderTypeSelector from '../components/onboarding/ReaderTypeSelector';
import Toggle from '../components/ui/Toggle';
import Button from '../components/ui/Button';
import Card from '../components/ui/Card';

const DEVICE_FEATURES = [
  { key: 'textToSpeech', label: 'Text-to-Speech', description: 'Have TaleTrace read pages aloud.' },
  { key: 'ambientMusic', label: 'Ambient Music', description: 'Play background music while you read.' },
  { key: 'readOutMeaning', label: 'Read Out Meaning', description: 'Speak word meanings aloud on lookup.' },
  { key: 'displayMeaning', label: 'Display Meaning', description: 'Show word meanings on the OLED screen.' },
];

export default function ProfileSetup() {
  const { user, refreshUser } = useAuth();
  const navigate = useNavigate();
  const [step, setStep] = useState(1);
  const [prefs, setPrefs] = useState({
    textToSpeech: true,
    ambientMusic: false,
    readOutMeaning: true,
    displayMeaning: true,
  });
  const [readerType, setReaderType] = useState('normal');
  const [saving, setSaving] = useState(false);

  const finishStep2 = async () => {
    setSaving(true);
    await api.submitDevicePreferences(user.id, prefs);
    setSaving(false);
    setStep(3);
  };

  const finishSetup = async () => {
    setSaving(true);
    const updated = await api.submitReaderType(user.id, readerType);
    refreshUser(updated);
    setSaving(false);
    navigate('/dashboard');
  };

  return (
    <div className="onboard-screen">
      <div className="onboard-card">
        <div className="auth-brand" style={{ justifyContent: 'center' }}>
          <span className="auth-brand-mark">
            <BookMarked strokeWidth={2.4} />
          </span>
          <span className="auth-brand-name">TaleTrace</span>
        </div>

        <ProgressIndicator step={step} total={3} />

        {step === 1 && (
          <>
            <div className="onboard-heading">
              <h1>How fast do you read?</h1>
              <p>Pick a preset, or take a quick 20-second reading test.</p>
            </div>
            <Card>
              <SpeedSelector userId={user.id} onComplete={() => setStep(2)} />
            </Card>
          </>
        )}

        {step === 2 && (
          <>
            <div className="onboard-heading">
              <h1>Device Preferences</h1>
              <p>Choose how your TaleTrace device should behave while you read.</p>
            </div>
            <Card className="stack" style={{ gap: 18 }}>
              {DEVICE_FEATURES.map((f) => (
                <Toggle
                  key={f.key}
                  label={f.label}
                  description={f.description}
                  checked={prefs[f.key]}
                  onChange={(val) => setPrefs({ ...prefs, [f.key]: val })}
                />
              ))}
            </Card>
            <div className="onboard-actions">
              <Button variant="ghost" onClick={() => setStep(1)}>
                Back
              </Button>
              <Button onClick={finishStep2} loading={saving}>
                Continue
              </Button>
            </div>
          </>
        )}

        {step === 3 && (
          <>
            <div className="onboard-heading">
              <h1>How do you usually read?</h1>
              <p>This helps TaleTrace tune text display and pacing for you.</p>
            </div>
            <ReaderTypeSelector value={readerType} onChange={setReaderType} />
            <div className="onboard-actions">
              <Button variant="ghost" onClick={() => setStep(2)}>
                Back
              </Button>
              <Button variant="ribbon" onClick={finishSetup} loading={saving}>
                Finish Setup
              </Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
