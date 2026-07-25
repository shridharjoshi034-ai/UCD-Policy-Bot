import { useState } from "react";

// TEMPORARY: seed data for visual QA, will be reverted after review.
const TEST_MESSAGES = [
  { role: "user", content: "What is the late submission policy for assignments?" },
  {
    role: "assistant",
    content:
      "## Late Submission Policy\n\nAssignments submitted after the deadline are subject to the following:\n\n- **0-24 hours late**: 10% deduction\n- **24-48 hours late**: 20% deduction\n- **Beyond 48 hours**: not accepted, unless an *extenuating circumstances* form has been approved.\n\nPlease contact your school office if you need an extension.",
    citations: [
      { title: "UCD Assessment Regulations 2025/26", source_url: "https://example.com/assessment-regs" },
      { title: "School of Computer Science Late Policy", source_url: "https://example.com/cs-late-policy" },
    ],
  },
  { role: "user", content: "Thanks! One more question about repeat exams." },
  { role: "assistant", content: "", citations: [] },
];

export default function useChatStream() {
  const [messages, setMessages] = useState(TEST_MESSAGES);
  const [isRunning, setIsRunning] = useState(false);

  const sendMessage = async (question) => {
    // Add user message
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
    },
    ]);

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
    } finally {
      setIsRunning(false);
    }
  };

  return {
    messages,
    sendMessage,
    isRunning,
  };
}