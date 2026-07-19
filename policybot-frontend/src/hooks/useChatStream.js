import { useState } from "react";

// Empty default = relative URL (works with Vite dev proxy).
// Set VITE_BACKEND_URL=http://localhost:8000 for direct dev, or
// VITE_BACKEND_URL=http://backend:8000 in Docker.
const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || "";

export default function useChatStream() {
  const [messages, setMessages] = useState([]);
  const [isSending, setIsSending] = useState(false);

  const sendMessage = async (question) => {
    if (isSending) return;
    setIsSending(true);

    setMessages((prev) => [
      ...prev,
      {
        role: "user",
        content: question,
      },
      {
        role: "assistant",
        content: "",
        citations: [],
        status: "thinking",
        latency: null,
        ttft: null,
      },
    ]);

    let fullText = ""; // buffer

    try {
      const response = await fetch(`${BACKEND_URL}/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split("\n\n");
        buffer = events.pop();  // keep incomplete trailing event for next chunk

        for (const eventBlock of events) {
          const lines = eventBlock.split("\n");
          let eventName = "", eventData = "";

          for (const line of lines) {
            if (line.startsWith("event:")) eventName = line.replace("event:", "").trim();
            if (line.startsWith("data:")) eventData = line.replace("data:", "").trim();
          }

          if (!eventData) continue;
          const data = JSON.parse(eventData);

          if (eventName === "token") {
            fullText += data.text; // accumulate, do NOT update state
          }

          if (eventName === "final") {
            const genLatency = data.latency_seconds != null ? data.latency_seconds : 0;
            // TTFT is only present when tokens were actually generated (not for greetings/errors)
            const ttft = data.ttft_seconds != null ? data.ttft_seconds : null;
            setMessages((prev) => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (last && last.role === "assistant") {
                last.content = fullText;
                last.citations = data.citations || [];
                last.status = "done";
                last.latency = Math.round(genLatency * 10) / 10;
                last.ttft = ttft != null ? Math.round(ttft * 10) / 10 : null;
              }
              return updated;
            });
          }

          // optional logs
          if (eventName === "retrieval_started") console.log(data.message);
          if (eventName === "chunks_found") console.log("Chunks:", data.count);
          if (eventName === "chunk_found") console.log("Source:", data.title);
        }
      }
    } catch (error) {
      console.error("Stream error:", error);
    } finally {
      setIsSending(false);
    }
  };

  return { messages, sendMessage };
}