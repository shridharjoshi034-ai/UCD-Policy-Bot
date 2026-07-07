import "../styles/MessageBubble.css";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export default function MessageBubble({ message }) {
  const isUser = message.role === "user";

  const speakMessage = () => {
    // Stop current speech if already speaking
    if (window.speechSynthesis.speaking) {
      window.speechSynthesis.cancel();
      return;
    }

    const speech = new SpeechSynthesisUtterance(message.content);

    speech.rate = 1;
    speech.pitch = 1;
    speech.volume = 1;

    const voices = window.speechSynthesis.getVoices();

    const englishVoice =
      voices.find(v => v.lang === "en-IE") ||
      voices.find(v => v.lang === "en-GB") ||
      voices.find(v => v.lang.startsWith("en"));

    if (englishVoice) {
      speech.voice = englishVoice;
    }

    window.speechSynthesis.speak(speech);
  };

  return (
    <div className={`bubble-row ${isUser ? "right" : "left"}`}>
      <div className={`bubble ${isUser ? "user" : "bot"}`}>

        <div className="markdown-body">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {message.content}
          </ReactMarkdown>
        </div>

        {!isUser && (
  <>
    <div className="bubble-actions">
      <button
        className="speak-btn"
        onClick={speakMessage}
        title={
          window.speechSynthesis.speaking
            ? "Stop reading"
            : "Read aloud"
        }
      >
        Read aloud 🔊 
      </button>
    </div>

    {message.citations && (
      <div className="citations">
        <div className="citations-title">Sources:</div>

        {message.citations.length > 0 ? (
          message.citations.map((c, i) => (
            <div key={i} className="citation-item">
              <a href={c.source_url} target="_blank" rel="noreferrer">
                🔗 {c.title}
              </a>
            </div>
          ))
        ) : (
          <div className="citation-empty">
            No sources available
          </div>
        )}
      </div>
    )}
  </>
)}
      </div>
    </div>
  );
}