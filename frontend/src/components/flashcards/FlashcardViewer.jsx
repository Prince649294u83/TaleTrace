import { useState } from 'react';
import { ChevronLeft, ChevronRight, RotateCcw } from 'lucide-react';
import Button from '../ui/Button';

export default function FlashcardViewer({ cards, onRestart }) {
  const [index, setIndex] = useState(0);
  const [revealed, setRevealed] = useState(false);
  const card = cards[index];

  const go = (delta) => {
    setRevealed(false);
    setIndex((i) => Math.min(cards.length - 1, Math.max(0, i + delta)));
  };

  return (
    <div className="flashcard-wrap">
      <div className="quiz-progress">
        Flashcard {index + 1} / {cards.length}
      </div>

      <div className="flashcard" onClick={() => setRevealed((r) => !r)}>
        {revealed ? (
          <div className="flashcard-definition">{card.definition}</div>
        ) : (
          <>
            <div className="flashcard-term">{card.term}</div>
            <span className="badge badge-accent">Tap to reveal</span>
          </>
        )}
      </div>

      <div className="flashcard-nav">
        <Button variant="secondary" iconOnly icon={<ChevronLeft size={16} />} disabled={index === 0} onClick={() => go(-1)} aria-label="Previous" />
        <Button variant={revealed ? 'secondary' : 'primary'} onClick={() => setRevealed((r) => !r)}>
          Reveal
        </Button>
        <Button
          variant="secondary"
          iconOnly
          icon={<ChevronRight size={16} />}
          disabled={index === cards.length - 1}
          onClick={() => go(1)}
          aria-label="Next"
        />
      </div>

      <Button variant="ghost" size="sm" icon={<RotateCcw size={13} />} onClick={onRestart}>
        Generate new flashcards
      </Button>
    </div>
  );
}
