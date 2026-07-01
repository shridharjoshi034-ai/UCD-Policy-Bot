import "../styles/MessageBubble.css";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export default function MessageBubble({ message }) {
  const isUser = message.role === "user";

  return (
    <div className={`bubble-row ${isUser ? "right" : "left"}`}>
      <div className={`bubble ${isUser ? "user" : "bot"}`}>

        <div className="markdown-body">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {message.content}
          </ReactMarkdown>
        </div>

        {/* 👇 SAME SIMPLE LOGIC AS BEFORE */}
        {!isUser && message.citations && (
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

      </div>
    </div>
  );
}

