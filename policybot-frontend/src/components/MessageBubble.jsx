import { useState } from "react";
import "../styles/MessageBubble.css";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

function CopyIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="12" height="12" rx="2" />
      <path d="M5 15V5a2 2 0 0 1 2-2h10" />
    </svg>
  );
}

export default function MessageBubble({ message }) {
  const isUser = message.role === "user";
  const text = message.content
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("");
  const hasContent = text.trim().length > 0;
  const citations = message.metadata?.custom?.citations;
  const [copied, setCopied] = useState(false);

  const copyMessage = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (err) {
      console.error("Failed to copy message:", err);
    }
  };

  const speakMessage = () => {
    if (window.speechSynthesis.speaking) {
      window.speechSynthesis.cancel();
      return;
    }

    const speech = new SpeechSynthesisUtterance(text);

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

  // Open a file on click — mimics the dashboard's openFileOnSupabase pattern:
  // makes an API call to get a fresh signed URL, then opens it in a new tab
  const openCitationFile = async (citation, e) => {
    e.preventDefault();

    // Extract the storage path from the filename in the citation
    const title = citation.title || "";
    const ext = title.split(".").pop()?.toLowerCase() || "";
    let filePath = title;
    if (ext === "pdf") {
      filePath = `pdfs/${title}`;
    } else if (ext === "md") {
      filePath = `markdown/${title}`;
    }

    try {
      const res = await fetch(`${API_BASE}/chat/file-url`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_path: filePath }),
      });
      if (!res.ok) {
        console.error("Failed to get file URL:", res.statusText);
        // Fallback: try the pre-generated URL
        const fallbackUrl = citation.open_url || citation.source_url;
        if (fallbackUrl) window.open(fallbackUrl, "_blank");
        return;
      }
      const data = await res.json();
      if (data.url) {
        window.open(data.url, "_blank");
      } else if (data.error) {
        console.error("File URL error:", data.error);
      }
    } catch (err) {
      console.error("Could not open file:", err);
      // Fallback: try the pre-generated URL
      const fallbackUrl = citation.open_url || citation.source_url;
      if (fallbackUrl) window.open(fallbackUrl, "_blank");
    }
  };

  return (
    <div className={`bubble-row ${isUser ? "right" : "left"}`}>
      <div className={isUser ? "bubble user" : "bot-plain"}>

        <div className="markdown-body">
          {!isUser && !hasContent ? (
            <div className="typing-indicator">
              <span></span>
              <span></span>
              <span></span>
            </div>
          ) : (
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {text}
            </ReactMarkdown>
          )}
        </div>

        {!isUser && hasContent && (
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

    {citations && (
      <div className="citations">
        <div className="citations-title">Sources:</div>

        {citations.length > 0 ? (
          citations.map((c, i) => (
            <div key={i} className="citation-item">
              <a href={c.source_url} target="_blank" rel="noreferrer">
                🔗 {c.title}
              </a>
            </div>

            {message.citations && message.citations.length > 0 && (
              <div className="citations">
                <div className="citations-title">Sources:</div>
                {message.citations.map((c, i) => (
                  <div key={i} className="citation-item">
                    <button
                      className="citation-link"
                      onClick={(e) => openCitationFile(c, e)}
                    >
                      📄 {c.title}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    )}

    <div className="copy-row">
      <button
        className="icon-btn-plain"
        onClick={copyMessage}
        title="Copy message"
        aria-label="Copy message"
      >
        <CopyIcon />
      </button>
      {copied && (
        <span className="copy-toast">Message copied to clipboard</span>
      )}
    </div>
  </>
)}
      </div>
    </div>
  );
}
