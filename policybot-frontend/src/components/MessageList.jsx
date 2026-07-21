import { useEffect, useRef } from "react";
import MessageBubble from "./MessageBubble";
import "../styles/MessageList.css";

export default function MessageList({ messages }) {

  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "end",
    });
  }, [messages]);

  return (
    <div className="message-list">

      {messages.map((msg, i) => (
        <MessageBubble 
          key={i}
          message={msg}
        />
      ))}

      <div ref={bottomRef} />

    </div>
  );
}
