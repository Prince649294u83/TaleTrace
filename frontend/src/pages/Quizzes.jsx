import { useState } from 'react';
import { Sparkles } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import { api } from '../services/api';
import SessionSelector from '../components/sessions/SessionSelector';
import QuizViewer from '../components/quiz/QuizViewer';
import Button from '../components/ui/Button';
import Card from '../components/ui/Card';
import ErrorState from '../components/ui/ErrorState';

export default function Quizzes() {
  const { user } = useAuth();
  const [selected, setSelected] = useState(new Set());
  const [quiz, setQuiz] = useState(null);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState('');

  const generate = async () => {
    setGenerating(true);
    setError('');
    try {
      const res = await api.generateQuiz(user.id, Array.from(selected));
      setQuiz(res);
    } catch (e) {
      setError(e.message || 'Could not generate a quiz. Please try again.');
    } finally {
      setGenerating(false);
    }
  };

  const reset = () => {
    setQuiz(null);
    setSelected(new Set());
  };

  if (quiz) {
    return (
      <>
        <div>
          <h1 className="page-title">Generated Quiz</h1>
          <p className="page-subtitle">{quiz.questions.length} multiple-choice questions from your selected sessions.</p>
        </div>
        <QuizViewer userId={user.id} quizId={quiz.quizId} questions={quiz.questions} onRestart={reset} />
      </>
    );
  }

  return (
    <>
      <div>
        <h1 className="page-title">Quizzes</h1>
        <p className="page-subtitle">Select the reading sessions you want to generate quizzes from.</p>
      </div>

      <SessionSelector userId={user.id} selected={selected} onChange={setSelected} />

      {error && (
        <Card>
          <ErrorState title="Couldn't generate quiz" description={error} onRetry={generate} />
        </Card>
      )}

      <Button icon={<Sparkles size={15} />} disabled={selected.size === 0} loading={generating} onClick={generate}>
        Generate Quiz
      </Button>
    </>
  );
}
