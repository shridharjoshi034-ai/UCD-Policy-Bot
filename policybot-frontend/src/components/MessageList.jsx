import { ThreadPrimitive } from "@assistant-ui/react";
import MessageBubble from "./MessageBubble";
import "../styles/MessageList.css";

export default function MessageList() {
  return (
    <div className="message-list">
      <ThreadPrimitive.Messages>
        {({ message }) => <MessageBubble message={message} />}
      </ThreadPrimitive.Messages>
    </div>
  );
}
