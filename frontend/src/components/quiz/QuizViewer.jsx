import { useState } from 'react';
import { ChevronLeft, ChevronRight, RotateCcw } from 'lucide-react';
import { api } from '../../services/api';
import Card from '../ui/Card';
import Button from '../ui/Button';
import QuizQuestion from './QuizQuestion';

export default function QuizViewer({ userId, quizId, questions, onRestart }) {
  const [index, setIndex] = useState(0);
  const [answers, setAnswers] = useState({});
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState(null);

  const question = questions[index];
  const isLast = index === questions.length - 1;
  const allAnswered = questions.every((q) => answers[q.id] !== undefined);

  const select = (optionIndex) => {
    setAnswers({ ...answers, [question.id]: optionIndex });
  };

  const submit = async () => {
    setSubmitting(true);
    const res = await api.submitQuiz(userId, quizId, answers);
    setResult(res);
    setSubmitting(false);
  };

  if (result) {
    const resultByQuestion = Object.fromEntries(result.results.map((r) => [r.questionId, r]));
    return (
      <div className="stack">
        <Card>
          <div className="result-score">
            <div className="value" style={{ color: result.percent >= 60 ? 'var(--success)' : 'var(--danger)' }}>
              {result.score}/{result.total}
            </div>
            <div className="label">You scored {result.percent}% on this quiz</div>
          </div>
        </Card>
        {questions.map((q, i) => (
          <Card key={q.id}>
            <QuizQuestion
              question={q}
              index={i}
              total={questions.length}
              selectedIndex={answers[q.id]}
              correctIndex={resultByQuestion[q.id]?.correctIndex}
              revealed
              onSelect={() => {}}
            />
          </Card>
        ))}
        <Button variant="secondary" icon={<RotateCcw size={14} />} onClick={onRestart} block>
          Generate Another Quiz
        </Button>
      </div>
    );
  }

  return (
    <Card>
      <QuizQuestion
        question={question}
        index={index}
        total={questions.length}
        selectedIndex={answers[question.id]}
        onSelect={select}
      />
      <div className="quiz-nav">
        <Button variant="secondary" icon={<ChevronLeft size={14} />} disabled={index === 0} onClick={() => setIndex((i) => i - 1)}>
          Previous
        </Button>
        <div className="quiz-dots">
          {questions.map((q, i) => (
            <span
              key={q.id}
              className={`quiz-dot ${i === index ? 'quiz-dot-current' : answers[q.id] !== undefined ? 'quiz-dot-answered' : ''}`}
            />
          ))}
        </div>
        {isLast ? (
          <Button onClick={submit} loading={submitting} disabled={!allAnswered}>
            Submit Quiz
          </Button>
        ) : (
          <Button onClick={() => setIndex((i) => i + 1)}>
            Next
            <ChevronRight size={14} />
          </Button>
        )}
      </div>
    </Card>
  );
}
