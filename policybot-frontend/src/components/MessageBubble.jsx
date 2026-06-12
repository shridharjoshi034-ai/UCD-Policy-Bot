import "../styles/MessageBubble.css";

export default function MessageBubble({ message }) {
  const isUser = message.role === "user";

  return (
    <div className={`bubble-row ${isUser ? "right" : "left"}`}>
      <div className={`bubble ${isUser ? "user" : "bot"}`}>
        <div>{message.content}</div>

        {message.citations && (
          <div className="citations">
            <div className="citations-title">Sources:</div>
            {message.citations.map((c, i) => (
              <div key={i} className="citation-item">
                <a
                  href={c.source_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  🔗 {c.title}
                </a>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}