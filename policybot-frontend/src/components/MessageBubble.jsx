import { useState, useEffect, useRef } from "react";
import "../styles/MessageBubble.css";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export default function MessageBubble({ message }) {
  const isUser = message.role === "user";

  // For typewriter effect
  const [displayText, setDisplayText] = useState("");
  const [isTyping, setIsTyping] = useState(false);
  const hasTypedRef = useRef(false);  // guards against re-triggering on same content

  // Reset typing when message changes
  useEffect(() => {
    if (isUser) {
      setDisplayText(message.content);
      hasTypedRef.current = false;
      return;
    }

    // Assistant
    if (message.status === "thinking") {
      setDisplayText("");
      setIsTyping(false);
      hasTypedRef.current = false;
    } else if (message.status === "done" && message.content) {
      if (hasTypedRef.current) return;
      hasTypedRef.current = true;
      setDisplayText("");
      setIsTyping(true);
    }
  }, [isUser, message.status, message.content]);

  // Typewriter loop — uses requestAnimationFrame for batched rendering
  // instead of per-character setTimeout to avoid thousands of re-renders.
  // Tracks progress via displayText.length to avoid the cancel/restart
  // cycle that typingIndex in deps would cause.
  useEffect(() => {
    if (!isTyping || !message.content) return;

    const BATCH_SIZE = 4; // characters per animation frame
    const content = message.content;
    let rafId;

    const tick = () => {
      let done = false;

      setDisplayText((prev) => {
        if (prev.length >= content.length) {
          done = true;
          return prev;
        }
        const end = Math.min(prev.length + BATCH_SIZE, content.length);
        return prev + content.slice(prev.length, end);
      });

      if (done) {
        setIsTyping(false);
        return; // stop the rAF loop
      }

      rafId = requestAnimationFrame(tick);
    };

    rafId = requestAnimationFrame(tick);

    return () => cancelAnimationFrame(rafId);
  }, [isTyping, message.content]);

  // Speak function (unchanged)
  const speakMessage = () => {
    if (!window.speechSynthesis) return;
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
      voices.find((v) => v.lang === "en-IE") ||
      voices.find((v) => v.lang === "en-GB") ||
      voices.find((v) => v.lang.startsWith("en"));
    if (englishVoice) speech.voice = englishVoice;
    window.speechSynthesis.speak(speech);
  };

  // Status line
  let statusLine = null;
  if (!isUser) {
    if (message.status === "thinking") {
      statusLine = <div className="status-line thinking">Thinking…</div>;
    } else if (message.status === "done" && message.latency != null) {
      statusLine = (
        <div className="status-line thought">
          {message.ttft != null ? (
            <>First token in {message.ttft}s · Generated in {message.latency}s</>
          ) : (
            <>Generated in {message.latency}s</>
          )}
        </div>
      );
    }
  }

  // The content to render:
  // - For user: always show full content
  // - For assistant: show the typed displayText (which gradually reveals)
  const contentToRender = isUser ? message.content : displayText;

  return (
    <div className={`bubble-row ${isUser ? "right" : "left"}`}>
      <div className={`bubble ${isUser ? "user" : "bot"}`}>
        {/* Status line (if any) */}
        {statusLine}

        {/* Main content */}
        <div className="markdown-body">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {contentToRender}
          </ReactMarkdown>
        </div>

        {/* Actions & citations – only for assistant and only when full content exists */}
        {!isUser && message.content && (
          <>
            <div className="bubble-actions">
              <button
                className="speak-btn"
                onClick={speakMessage}
              title={
                window.speechSynthesis?.speaking ? "Stop reading" : "Read aloud"
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
                  <div className="citation-empty">No sources available</div>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}