import { ComposerPrimitive, WebSpeechDictationAdapter, useAui } from "@assistant-ui/react";
import "../styles/InputBox.css";

const dictationSupported = WebSpeechDictationAdapter.isSupported();

function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3Z" />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <line x1="12" y1="19" x2="12" y2="23" />
      <line x1="8" y1="23" x2="16" y2="23" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor">
      <rect x="5" y="5" width="14" height="14" rx="2" />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="12" y1="19" x2="12" y2="5" />
      <polyline points="6 11 12 5 18 11" />
    </svg>
  );
}

export default function InputBox() {
  const aui = useAui();

  // While a response is still streaming, assistant-ui's own Enter handler
  // deliberately no-ops instead of calling preventDefault(), so the keypress
  // falls through to the textarea and inserts a newline. Swallow it here too
  // so Enter is a no-op (not "insert newline") whenever sending is blocked.
  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey && aui.thread().getState().isRunning) {
      e.preventDefault();
    }
  };

  return (
    <ComposerPrimitive.Root className="composer-bar">
      <ComposerPrimitive.Input
        className="composer-input"
        placeholder="Ask your question..."
        rows={1}
        maxRows={6}
        onKeyDown={handleKeyDown}
      />

      {dictationSupported && (
        <>
          <ComposerPrimitive.If dictation={false}>
            <ComposerPrimitive.Dictate className="icon-btn" title="Ask by voice">
              <MicIcon />
            </ComposerPrimitive.Dictate>
          </ComposerPrimitive.If>

          <ComposerPrimitive.If dictation={true}>
            <span className="dictation-preview">
              <ComposerPrimitive.DictationTranscript />
            </span>
            <ComposerPrimitive.StopDictation className="icon-btn recording" title="Stop recording">
              <StopIcon />
            </ComposerPrimitive.StopDictation>
          </ComposerPrimitive.If>
        </>
      )}

      <ComposerPrimitive.Send className="icon-btn send-btn" title="Send">
        <SendIcon />
      </ComposerPrimitive.Send>
    </ComposerPrimitive.Root>
  );
}
