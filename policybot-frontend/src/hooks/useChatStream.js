import { useRef, useState } from "react";

export default function useChatStream() {
  const [messages, setMessages] = useState([]);
  const [isRunning, setIsRunning] = useState(false);
  const abortControllerRef = useRef(null);

  const streamResponse = async (question) => {
    const controller = new AbortController();
    abortControllerRef.current = controller;

    setIsRunning(true);

    try {
      const response = await fetch(
        "http://localhost:8000/chat/stream",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            question,
          }),
          signal: controller.signal,
        }
      );

      const reader = response.body.getReader();
      const decoder = new TextDecoder();

      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();

        if (done) break;

        buffer += decoder.decode(value, {
          stream: true,
        });

        const events = buffer.split("\n\n");

        buffer = events.pop();

        for (const eventBlock of events) {
          const lines = eventBlock.split("\n");

          let eventName = "";
          let eventData = "";

          for (const line of lines) {
            if (line.startsWith("event:")) {
              eventName = line.replace("event:", "").trim();
            }

            if (line.startsWith("data:")) {
              eventData = line.replace("data:", "").trim();
            }
          }

          if (!eventData) continue;

          const data = JSON.parse(eventData);

          // STREAM TOKENS
          if (eventName === "token") {
            setMessages((prev) => {
              const updated = [...prev];

              const last =
                updated[updated.length - 1];

              if (
                last &&
                last.role === "assistant"
              ) {
                last.content += data.text;
              }

              return [...updated];
            });
          }

          // FINAL RESPONSE
          if (eventName === "final") {
            setMessages((prev) => {
              const updated = [...prev];

              const last =
                updated[updated.length - 1];

              if (
                last &&
                last.role === "assistant"
              ) {
                last.citations =
                  data.citations || [];
              }

              return [...updated];
            });
          }

          // OPTIONAL STATUS EVENTS
          if (
            eventName === "retrieval_started"
          ) {
            console.log(data.message);
          }

          if (
            eventName === "chunks_found"
          ) {
            console.log(
              "Chunks:",
              data.count
            );
          }
        }
      }
    } catch (err) {
      if (err.name !== "AbortError") throw err;
    } finally {
      setIsRunning(false);
      abortControllerRef.current = null;
    }
  };

  const sendMessage = async (question) => {
    setMessages((prev) => [
      ...prev,
      { role: "user", content: question },
      { role: "assistant", content: "", citations: [] },
    ]);

    await streamResponse(question);
  };

  const regenerateResponse = async () => {
    const lastIndex = messages.length - 1;
    if (lastIndex < 1 || messages[lastIndex].role !== "assistant") return;

    const question = messages[lastIndex - 1]?.content;
    if (!question) return;

    setMessages((prev) => {
      const updated = [...prev];
      updated[lastIndex] = { role: "assistant", content: "", citations: [] };
      return updated;
    });

    await streamResponse(question);
  };

  const cancelMessage = () => {
    abortControllerRef.current?.abort();
  };

  return {
    messages,
    sendMessage,
    regenerateResponse,
    isRunning,
    cancelMessage,
  };
}