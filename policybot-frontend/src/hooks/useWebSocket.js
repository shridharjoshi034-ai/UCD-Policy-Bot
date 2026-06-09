import { useEffect, useRef, useState } from "react";

export default function useWebSocket(url) {
  const ws = useRef(null);
  const [messages, setMessages] = useState([]);

  useEffect(() => {
    ws.current = new WebSocket(url);

    ws.current.onmessage = (event) => {
      const data = JSON.parse(event.data);

      setMessages((prev) => {
        const updated = [...prev];

        if (data.type === "token") {
          const last = updated[updated.length - 1];
          if (last && last.role === "assistant") {
            last.content += data.message;
          } else {
            updated.push({ role: "assistant", content: data.message });
          }
        }

        if (data.type === "final") {
          const last = updated[updated.length - 1];
          if (last?.role === "assistant") {
            last.citations = data.citations;
          }
        }

        if (data.type === "status") {
          updated.push({ role: "status", content: data.message });
        }

        return [...updated];
      });
    };

    return () => ws.current?.close();
  }, [url]);

  const sendMessage = (msg) => {
    ws.current?.send(
      JSON.stringify({
        type: "query",
        message: msg,
      })
    );

    setMessages((prev) => [
      ...prev,
      { role: "user", content: msg },
    ]);
  };

  return { messages, sendMessage };
}