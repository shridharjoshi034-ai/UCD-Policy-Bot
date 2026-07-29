import { useState } from "react";
import { ActionBarPrimitive } from "@assistant-ui/react";
import "../styles/MessageBubble.css";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const API_BASE = "http://localhost:8000";

function CopyIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="12" height="12" rx="2" />
      <path d="M5 15V5a2 2 0 0 1 2-2h10" />
    </svg>
  );
}

function RegenerateIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12a9 9 0 1 1-3-6.7" />
      <polyline points="21 3 21 9 15 9" />
    </svg>
  );
}

function SpeakerIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polygon points="3 9 3 15 8 15 13 20 13 4 8 9 3 9" fill="currentColor" stroke="none" />
      <path d="M16.5 8.5a5 5 0 0 1 0 7" />
      <path d="M19 6a8 8 0 0 1 0 12" />
    </svg>
  );
}

function StopSpeakingIcon() {
  return (
    <svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor">
      <rect x="5" y="5" width="14" height="14" rx="2" />
    </svg>
  );
}
function cleanTextForSpeech(markdown) {
  return markdown

    // Convert headings into natural pauses
    .replace(/^#{1,6}\s*(.*)$/gm, "$1.")

    // Remove bold and italic markers
    .replace(/\*\*(.*?)\*\*/g, "$1")
    .replace(/\*(.*?)\*/g, "$1")
    .replace(/__(.*?)__/g, "$1")
    .replace(/_(.*?)_/g, "$1")

    // Remove inline code formatting
    .replace(/`([^`]+)`/g, "$1")

    // Convert markdown links
    // [Assessment Policy](url) -> Assessment Policy
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")

    // Remove bullet symbols
    .replace(/^\s*[-*+]\s+/gm, "")

    // Remove numbered list markers
    .replace(/^\s*\d+\.\s+/gm, "")

    // Remove block quotes
    .replace(/^>\s+/gm, "")

    // Remove markdown tables formatting
    .replace(/\|/g, " ")

    // Remove horizontal separators
    .replace(/^[-*_]{3,}$/gm, "")

    // Remove extra whitespace
    .replace(/\n{2,}/g, "\n")

    .trim();
}
export default function MessageBubble({ message }) {
  const isUser = message.role === "user";
  const text = message.content
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("");
  const hasContent = text.trim().length > 0;
  // "running" is only ever true for the last message while the thread is
  // actually streaming into it — earlier stopped-empty messages stay
  // "complete"/"incomplete" even while a later message is running.
  const isMessageRunning = message.status?.type === "running";
  const citations = message.metadata?.custom?.citations;
  const [copied, setCopied] = useState(false);
  const [isSpeaking, setIsSpeaking] = useState(false);

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
      setIsSpeaking(false);
      return;
    }
    
    const cleanSpeechText = cleanTextForSpeech(text);
    const speech = new SpeechSynthesisUtterance(cleanSpeechText);

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

    speech.onend = () => setIsSpeaking(false);
    speech.onerror = () => setIsSpeaking(false);

    window.speechSynthesis.speak(speech);
    setIsSpeaking(true);
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
            isMessageRunning ? (
              <div className="typing-indicator">
                <span></span>
                <span></span>
                <span></span>
              </div>
            ) : (
              <span className="stopped-note">Response stopped</span>
            )
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
        title={isSpeaking ? "Stop reading" : "Read aloud"}
      >
        {isSpeaking ? <StopSpeakingIcon /> : <SpeakerIcon />}
        {isSpeaking ? "Stop reading" : "Read aloud"}
      </button>
    </div>

    {citations && citations.length > 0 && (
      <div className="citations">
        <div className="citations-title">Sources:</div>

        {citations.map((c, i) => (
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

    <div className="copy-row">
      <button
        className="icon-btn-plain"
        onClick={copyMessage}
        title="Copy message"
        aria-label="Copy message"
      >
        <CopyIcon />
      </button>
      <ActionBarPrimitive.Reload
        className="icon-btn-plain"
        title="Regenerate response"
        aria-label="Regenerate response"
      >
        <RegenerateIcon />
      </ActionBarPrimitive.Reload>
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
