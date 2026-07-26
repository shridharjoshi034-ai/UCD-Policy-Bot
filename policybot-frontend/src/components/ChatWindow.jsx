import { useState } from "react";
import { AssistantRuntimeProvider, ThreadPrimitive } from "@assistant-ui/react";
import useChatStream from "../hooks/useChatStream";
import useChatRuntime from "../runtime/useChatRuntime";

import MessageList from "./MessageList";
import InputBox from "./InputBox";
import SuggestedQuestions from "./SuggestedQuestions";
import ucdLogo from "../assets/ucd-logo.png";
import "../styles/ChatWindow.css";

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="4" />
      <line x1="12" y1="2" x2="12" y2="4" />
      <line x1="12" y1="20" x2="12" y2="22" />
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
      <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
      <line x1="2" y1="12" x2="4" y2="12" />
      <line x1="20" y1="12" x2="22" y2="12" />
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
      <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79Z" />
    </svg>
  );
}

export default function ChatWindow() {
  const { messages, sendMessage, isRunning, cancelMessage, regenerateResponse } = useChatStream();
  const runtime = useChatRuntime({ messages, sendMessage, isRunning, cancelMessage, regenerateResponse });
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
        ? "dark bg-[#0F172A] text-[#E5E7EB]"
        : "bg-[#F8FAFC] text-[#111827]"
    }`}
  >
        {/* HEADER */}
        <header
          className={`absolute top-0 inset-x-0 z-20 flex items-center gap-3 px-4 py-2 backdrop-blur-sm border-b transition-colors duration-300 ${
            darkMode
              ? "bg-[#0F172A]/5 border-white/10"
              : "bg-[#F8FAFC]/5 border-black/5"
          }`}
        >
          <img
              src={ucdLogo}
              alt="UCD Logo"
              className="w-10 h-10 object-contain"
          />

          <div>
            <h1 className="text-base font-semibold leading-tight">
              University College Dublin
            </h1>
            <p className="text-xs text-gray-500 leading-tight">
              An Coláiste Ollscoile, Baile Átha Cliath
            </p>
            <p
              style={{
                color: "#004077",
                fontWeight: 700,
                letterSpacing: "0.08em",
                fontSize: "10px",
                marginTop: "1px"
              }}
            >
            STUDENT HELPDESK
            </p>
        </div>

          <div className="ml-auto flex items-center gap-3">
            <a
              href="http://localhost:8000/admin"
              target="_blank"
              rel="noreferrer"
              className="px-2.5 py-1 text-sm border rounded-lg"
            >
              Admin Login
            </a>

            <button
              onClick={toggleDarkMode}
              role="switch"
              aria-checked={darkMode}
              aria-label="Toggle dark mode"
              className={`theme-toggle ${darkMode ? "dark" : ""}`}
            >
              <span className="theme-toggle-thumb">
                {darkMode ? <MoonIcon /> : <SunIcon />}
              </span>
            </button>
          </div>
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
              <div className="max-w-4xl mx-auto pt-16 pb-40">
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