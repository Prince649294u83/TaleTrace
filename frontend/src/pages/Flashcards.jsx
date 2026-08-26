import { useState } from 'react';
import { Sparkles } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import { api } from '../services/api';
import SessionSelector from '../components/sessions/SessionSelector';
import FlashcardViewer from '../components/flashcards/FlashcardViewer';
import Button from '../components/ui/Button';
import Card from '../components/ui/Card';
import ErrorState from '../components/ui/ErrorState';

export default function Flashcards() {
  const { user } = useAuth();
  const [selected, setSelected] = useState(new Set());
  const [cards, setCards] = useState(null);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState('');

  const generate = async () => {
    setGenerating(true);
    setError('');
    try {
      const res = await api.generateFlashcards(user.id, Array.from(selected));
      setCards(res.cards);
    } catch (e) {
      setError(e.message || 'Could not generate flashcards. Please try again.');
    } finally {
      setGenerating(false);
    }
  };

  const reset = () => {
    setCards(null);
    setSelected(new Set());
  };

  if (cards) {
    return (
      <>
        <div>
          <h1 className="page-title">Flashcards</h1>
          <p className="page-subtitle">{cards.length} cards generated from your selected sessions.</p>
        </div>
        <FlashcardViewer cards={cards} onRestart={reset} />
      </>
    );
  }

  return (
    <>
      <div>
        <h1 className="page-title">Flashcards</h1>
        <p className="page-subtitle">Select the reading sessions you want flashcards from.</p>
      </div>

      <SessionSelector userId={user.id} selected={selected} onChange={setSelected} />

      {error && (
        <Card>
          <ErrorState title="Couldn't generate flashcards" description={error} onRetry={generate} />
        </Card>
      )}

      <Button icon={<Sparkles size={15} />} disabled={selected.size === 0} loading={generating} onClick={generate}>
        Generate Flashcards
      </Button>
    </>
  );
}
