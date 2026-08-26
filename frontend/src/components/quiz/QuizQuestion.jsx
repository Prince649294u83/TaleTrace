export default function QuizQuestion({ question, index, total, selectedIndex, onSelect, revealed, correctIndex }) {
  return (
    <div>
      <div className="quiz-progress">
        Question {index + 1} of {total}
      </div>
      <div className="quiz-question">{question.question}</div>
      <div className="quiz-options">
        {question.options.map((opt, i) => {
          let cls = 'quiz-option';
          if (revealed) {
            if (i === correctIndex) cls += ' quiz-option-correct';
            else if (i === selectedIndex) cls += ' quiz-option-incorrect';
          } else if (i === selectedIndex) {
            cls += ' quiz-option-selected';
          }
          return (
            <button key={i} type="button" className={cls} onClick={() => !revealed && onSelect(i)} disabled={revealed}>
              <span className={`quiz-radio ${i === selectedIndex ? 'quiz-radio-selected' : ''}`} />
              {opt}
            </button>
          );
        })}
      </div>
    </div>
  );
}
