import { useState } from "react";
import "../styles/InputBox.css";

export default function InputBox({ onSend }) {
  const [text, setText] = useState("");

  const handleSend = () => {
    if (!text.trim()) return;
    onSend(text);
    setText("");
  };

  
  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault(); // prevents newline on Enter
      handleSend();
    }
  };

  return (
    <div className="input-wrapper">
      <input
        className="input-box"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={handleKeyDown} 
        placeholder="Ask your question..."
      />

      <button className="send-btn" onClick={handleSend}>
        Send
      </button>
    </div>
  );
}