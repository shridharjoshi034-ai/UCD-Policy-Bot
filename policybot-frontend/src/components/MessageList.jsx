import MessageBubble from "./MessageBubble";
import "../styles/MessageList.css";

export default function MessageList({ messages }) {
  return (
    <div className="message-list">
      {messages.map((msg, i) => (
        <MessageBubble key={i} message={msg} />
      ))}
    </div>
  );
}