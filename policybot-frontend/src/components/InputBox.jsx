import { ComposerPrimitive, WebSpeechDictationAdapter, useAui } from "@assistant-ui/react";
import "../styles/InputBox.css";

const dictationSupported = WebSpeechDictationAdapter.isSupported();

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
    <ComposerPrimitive.Root className="input-wrapper">
      <ComposerPrimitive.Input
        className="input-box"
        placeholder="Ask your question..."
        rows={1}
        onKeyDown={handleKeyDown}
      />

      {dictationSupported && (
        <>
          <ComposerPrimitive.If dictation={false}>
            <ComposerPrimitive.Dictate className="mic-btn" title="Ask by voice">
              🎤
            </ComposerPrimitive.Dictate>
          </ComposerPrimitive.If>

          <ComposerPrimitive.If dictation={true}>
            <span className="dictation-preview">
              <ComposerPrimitive.DictationTranscript />
            </span>
            <ComposerPrimitive.StopDictation className="mic-btn recording" title="Stop recording">
              ⏹
            </ComposerPrimitive.StopDictation>
          </ComposerPrimitive.If>
        </>
      )}

      <ComposerPrimitive.Send className="send-btn">
        Send
      </ComposerPrimitive.Send>
    </ComposerPrimitive.Root>
  );
}