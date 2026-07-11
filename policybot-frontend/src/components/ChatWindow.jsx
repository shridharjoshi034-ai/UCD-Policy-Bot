import { useState } from "react";
import useChatStream from "../hooks/useChatStream";

import MessageList from "./MessageList";
import InputBox from "./InputBox";
import SuggestedQuestions from "./SuggestedQuestions";
import ucdLogo from "../assets/ucd-logo.png";
import "../styles/ChatWindow.css";

export default function ChatWindow() {
  const { messages, sendMessage } = useChatStream();
  const [darkMode, setDarkMode] = useState(false);
  const [started, setStarted] = useState(false);

  const toggleDarkMode = () => {
    setDarkMode((prev) => !prev);
  };

  const handleSend = (msg) => {
    if (!started) setStarted(true);
    sendMessage(msg);
  };

  return (
    <div
  className={`h-screen flex flex-col transition-colors duration-300 ${
    darkMode
      ? "bg-[#0F172A] text-[#E5E7EB]"
      : "bg-[#F8FAFC] text-[#111827]"
  }`}
>
      {/* HEADER */}
      <div className="flex items-center gap-4 p-4 ">
        <img
            src={ucdLogo}
            alt="UCD Logo"
            className="w-25 h-24 object-contain"
        />

        <div>
          <h1 className="text-2xl font-semibold">
            University College Dublin
          </h1>
          <p className="text-sm text-gray-500">
            An Coláiste Ollscoile, Baile Átha Cliath
          </p>
          <p
            style={{
              color: "#004077", 
              fontWeight: 700,
              letterSpacing: "0.08em",
              fontSize: "16px",
              marginTop: "2px"
            }}
          >
          STUDENT HELPDESK
          </p>
      </div>

        <button
          onClick={toggleDarkMode}
          className="px-3 py-1 border rounded-lg ml-auto"
        >
          Theme
        </button>
      </div>

      {/* BODY */}
      {messages.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center px-4">
          <h2 className="text-3xl font-bold mb-2">
            Hey There, Welcome to UCD PolicyBot
          </h2>

          <p className="text-gray-500 mb-6 text-center">
            Ask any academic or administrative question
          </p>

          <div className="w-full max-w-2xl">
            <SuggestedQuestions onSelect={handleSend} />
          </div>
        </div>
      ) : (
          <div className="flex-1 overflow-y-auto px-4 py-6">
          <div className="max-w-4xl mx-auto">
            <MessageList messages={messages} />
          </div>
          </div>
      )}

      {/* INPUT */}
      <div className="p-4">
        <InputBox onSend={handleSend} />
      </div>
    </div>
  );
}