/**
 * Dallas College Chatbot Widget — widget.js
 *
 * Architectural Intent:
 *   Thin UI controller that owns DOM interaction and delegates all
 *   intelligence to the FastAPI backend at /chat.  No business logic
 *   lives here; the widget is intentionally replaceable with any other
 *   front-end framework without touching the API.
 *
 * Security Rationale:
 *   - All content inserted into the DOM uses textContent (never innerHTML)
 *     to prevent XSS from untrusted API responses.
 *   - The API base URL is read from a data attribute on the <body> tag
 *     (or falls back to localhost) — it is never constructed from user input.
 *   - Fetch requests include no credentials by default; CORS is enforced
 *     server-side.
 *
 * @version 0.1.0
 */

(() => {
  "use strict";

  // ---------------------------------------------------------------------------
  // Configuration
  // ---------------------------------------------------------------------------

  /** Base URL of the FastAPI backend.  Override via <body data-api-base="…">. */
  const API_BASE =
    document.body.dataset.apiBase?.replace(/\/$/, "") ?? "http://localhost:8000";

  /** Maximum characters accepted from the input field (mirrors backend limit). */
  const MAX_INPUT_LENGTH = 2000;

  // ---------------------------------------------------------------------------
  // DOM references
  // ---------------------------------------------------------------------------

  const chatLog   = /** @type {HTMLElement} */ (document.getElementById("chat-log"));
  const userInput = /** @type {HTMLInputElement} */ (document.getElementById("user-input"));
  const sendBtn   = /** @type {HTMLButtonElement} */ (document.getElementById("send-btn"));

  if (!chatLog || !userInput || !sendBtn) {
    console.error("[DCChatbot] Required DOM elements not found. Widget disabled.");
    return;
  }

  // ---------------------------------------------------------------------------
  // UI helpers
  // ---------------------------------------------------------------------------

  /**
   * Append a message bubble to the chat log and scroll into view.
   *
   * @param {string} text    - The message text to display.
   * @param {"bot"|"user"|"error"} role - Visual style of the bubble.
   * @returns {void}
   */
  function appendMessage(text, role) {
    const div = document.createElement("div");
    div.className = `msg ${role}`;
    // textContent prevents XSS — do NOT change to innerHTML.
    div.textContent = text;
    chatLog.appendChild(div);
    chatLog.scrollTop = chatLog.scrollHeight;
  }

  /**
   * Show or hide the typing indicator while waiting for an API response.
   *
   * @param {boolean} visible
   * @returns {void}
   */
  function setTypingIndicator(visible) {
    const existingIndicator = document.getElementById("typing-indicator");
    if (visible && !existingIndicator) {
      const indicator = document.createElement("div");
      indicator.id = "typing-indicator";
      indicator.className = "msg bot";
      indicator.setAttribute("aria-label", "Assistant is typing");
      indicator.textContent = "…";
      chatLog.appendChild(indicator);
      chatLog.scrollTop = chatLog.scrollHeight;
    } else if (!visible && existingIndicator) {
      existingIndicator.remove();
    }
  }

  /**
   * Enable or disable the send button and input field.
   *
   * @param {boolean} enabled
   * @returns {void}
   */
  function setInputEnabled(enabled) {
    sendBtn.disabled = !enabled;
    userInput.disabled = !enabled;
  }

  // ---------------------------------------------------------------------------
  // API communication
  // ---------------------------------------------------------------------------

  /**
   * Send a user message to the /chat endpoint and return the parsed response.
   *
   * @param {string} message - The user's message text.
   * @returns {Promise<{reply: string, sources: string[]}>}
   */
  async function postChatMessage(message) {
    const response = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error ${response.status}: ${errorText}`);
    }

    return /** @type {{reply: string, sources: string[]}} */ (await response.json());
  }

  // ---------------------------------------------------------------------------
  // Event handlers
  // ---------------------------------------------------------------------------

  /**
   * Handle the send action: validate input, call API, render reply.
   *
   * @returns {Promise<void>}
   */
  async function handleSend() {
    const raw = userInput.value.trim();
    if (!raw) return;
    if (raw.length > MAX_INPUT_LENGTH) {
      appendMessage(`Your message is too long (max ${MAX_INPUT_LENGTH} characters).`, "error");
      return;
    }

    // Render user bubble immediately
    appendMessage(raw, "user");
    userInput.value = "";
    setInputEnabled(false);
    setTypingIndicator(true);

    try {
      const data = await postChatMessage(raw);
      setTypingIndicator(false);
      appendMessage(data.reply, "bot");
      if (data.sources && data.sources.length > 0) {
        appendMessage(`Sources: ${data.sources.join(", ")}`, "bot");
      }
    } catch (err) {
      setTypingIndicator(false);
      const message =
        err instanceof Error ? err.message : "An unexpected error occurred.";
      appendMessage(`Sorry, I couldn't get a response. ${message}`, "error");
      console.error("[DCChatbot] Fetch error:", err);
    } finally {
      setInputEnabled(true);
      userInput.focus();
    }
  }

  // Send on button click
  sendBtn.addEventListener("click", () => { void handleSend(); });

  // Send on Enter key (Shift+Enter inserts newline in textarea; plain Enter here)
  userInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void handleSend();
    }
  });
})();
