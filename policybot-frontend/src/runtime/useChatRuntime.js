import { useExternalStoreRuntime, WebSpeechDictationAdapter } from "@assistant-ui/react";

const dictationAdapter = WebSpeechDictationAdapter.isSupported()
  ? new WebSpeechDictationAdapter({ language: "en-IE", interimResults: true })
  : undefined;

export default function useChatRuntime({ messages, sendMessage, isRunning }) {
  return useExternalStoreRuntime({
    messages,
    isRunning,
    convertMessage: (message) => ({
      role: message.role,
      content: message.content,
      metadata: { custom: { citations: message.citations ?? [] } },
    }),
    onNew: async (message) => {
      const text = message.content.find((part) => part.type === "text")?.text ?? "";
      await sendMessage(text);
    },
    adapters: dictationAdapter ? { dictation: dictationAdapter } : undefined,
  });
}
