import "../styles/SuggestedQuestions.css";

export default function SuggestedQuestions({ onSelect }) {
  const questions = [
    "Late submission policy?",
    "Extenuating circumstances?",
    "Plagiarism policy?",
    "Grade calculation?",
  ];

  return (
    <div className="suggestions">
      <div className="title">Suggested Questions</div>

      <div className="list">
        {questions.map((q, i) => (
          <button key={i} onClick={() => onSelect(q)}>
            {q}
          </button>
        ))}
      </div>
    </div>
  );
}