import "../styles/SuggestedQuestions.css";

export default function SuggestedQuestions({ onSelect }) {

  const questions = [
    "Late submission policy?",
    "Extenuating circumstances?",
    "Plagiarism policy?",
    "Grade calculation?",
    "Exam regulations?",
    "Academic appeals?"
  ];


  return (

    <div className="suggestions">

      <div className="suggestions-title">
        SUGGESTED QUESTIONS
      </div>


      <div className="suggestions-list">

        {questions.map((q,index)=>(

          <button
            key={index}
            onClick={()=>onSelect(q)}
            className="suggestion-pill"
          >
            {q}
          </button>

        ))}

      </div>


    </div>

  );
}