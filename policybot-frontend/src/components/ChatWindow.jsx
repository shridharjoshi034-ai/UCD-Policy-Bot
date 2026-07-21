import { useState } from "react";
import { AssistantRuntimeProvider, ThreadPrimitive } from "@assistant-ui/react";
import useChatStream from "../hooks/useChatStream";
import useChatRuntime from "../runtime/useChatRuntime";

import MessageList from "./MessageList";
import InputBox from "./InputBox";
import SuggestedQuestions from "./SuggestedQuestions";
import ucdLogo from "../assets/ucd-logo.png";
import "../styles/ChatWindow.css";

export default function ChatWindow() {
  const { messages, sendMessage, isRunning } = useChatStream();
  const runtime = useChatRuntime({ messages, sendMessage, isRunning });
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
    <AssistantRuntimeProvider runtime={runtime}>
      <div
    className={`h-screen relative flex flex-col transition-colors duration-300 ${
      darkMode
        ? "bg-[#0F172A] text-[#E5E7EB]"
        : "bg-[#F8FAFC] text-[#111827]"
    }`}
  >
        {/* HEADER */}
        <header
          className={`absolute top-0 inset-x-0 z-20 flex items-center gap-4 p-4 backdrop-blur-md border-b transition-colors duration-300 ${
            darkMode
              ? "bg-[#0F172A]/60 border-white/10"
              : "bg-[#F8FAFC]/70 border-black/5"
          }`}
        >
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
        </header>

        <ThreadPrimitive.Root className="relative flex-1 min-h-0">
          {/* BODY */}
          {messages.length === 0 ? (
            <div className="absolute inset-0 flex flex-col items-center justify-center px-4">
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
            <ThreadPrimitive.Viewport className="absolute inset-0 overflow-y-auto px-4 chat-scroll message-fade-mask">
              <div className="max-w-4xl mx-auto pt-32 pb-40">
                <MessageList />
              </div>
            </ThreadPrimitive.Viewport>
          )}

          {/* INPUT */}
          <div className="absolute bottom-6 inset-x-0 z-20 px-4">
            <InputBox />
          </div>
        </ThreadPrimitive.Root>
      </div>
    </AssistantRuntimeProvider>
  );
}