import { ComposerPrimitive, WebSpeechDictationAdapter } from "@assistant-ui/react";
import "../styles/InputBox.css";

const dictationSupported = WebSpeechDictationAdapter.isSupported();

export default function InputBox() {
  return (
    <ComposerPrimitive.Root className="input-wrapper">
      <ComposerPrimitive.Input
        className="input-box"
        placeholder="Ask your question..."
        rows={1}
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