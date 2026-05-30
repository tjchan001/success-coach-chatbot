/**
 * Dallas College Chatbot Widget.
 *
 * Architectural Intent:
 * This file self-mounts an isolated floating chat widget into any page
 * that includes it. The widget owns only presentation and transport,
 * while all advisory logic stays in the FastAPI backend.
 *
 * Security Rationale:
 * - User input remains text-only; markdown rendering is applied only to
 * backend reply strings after local HTML escaping.
 * - No third-party markdown library is used; formatting is constrained to
 * bold, bullet, and inline footnote link transformations with deterministic regex rules.
 * - Network calls target a fixed local API endpoint and never include
 * user-controlled URLs.
 */

(() => {
  "use strict";

  const API_URL = "https://success-coach-chatbot.onrender.com/api/chat";
  const MAX_MESSAGE_LENGTH = 1000;
  const ROOT_ID = "dc-chatbot-root";
  const CHAT_HISTORY_KEY = "dc_chatbot_history";

  console.log("[DIAGNOSTIC] Chatbot widget initialized. Outgoing target API_URL is:", API_URL);

  if (document.getElementById(ROOT_ID)) {
    return;
  }

  /**
   * Create an element with optional class name.
   *
   * @param {string} tagName
   * @param {string=} className
   * @returns {HTMLElement}
   */
  function createElement(tagName, className) {
    const element = document.createElement(tagName);
    if (className) {
      element.className = className;
    }
    return element;
  }

  /**
   * Append plain-text content safely to an element.
   *
   * @param {HTMLElement} element
   * @param {string} text
   * @returns {void}
   */
  function setSafeText(element, text) {
    element.textContent = text;
  }

  /**
   * Escape special HTML characters before controlled markdown rendering.
   *
   * @param {string} text
   * @returns {string}
   */
  function escapeHtml(text) {
    return text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /**
   * Convert a constrained markdown subset into safe HTML.
   *
   * Supported rules:
   * - **bold** -> <strong>
   * - - bullet -> <li>, grouped into <ul>
   * - [text](url) -> <a href="url" target="_blank">text</a> (Enables footnote hyperlinks)
   *
   * @param {string} text
   * @returns {string}
   */
  function formatMarkdown(text) {
    const escaped = escapeHtml(text).replace(/\r\n/g, "\n");
    const withBold = escaped.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    const withListItems = withBold.replace(/(^|\n)\s*-\s+(.+?)(?=\n|$)/g, "$1<li>$2</li>");
    const groupedLists = withListItems.replace(
      /(?:<li>.*?<\/li>\s*)+/gs,
      (match) => `<ul style="margin: 0.4em 0; padding-left: 1.2em;">${match}</ul>`,
    );
    // Automatically parse inline Markdown links (e.g., footnote citations) safely
    const withLinks = groupedLists.replace(
      /\[(.+?)\]\((https?:\/\/.+?)\)/g,
      '<a href="$2" target="_blank" style="color: #0057d9; font-weight: 600; text-decoration: underline;">$1</a>'
    );
    return withLinks.replace(/\n/g, "<br>");
  }

  /**
   * Inject widget styles once.
   *
   * @returns {void}
   */
  function mountStyles() {
    const style = document.createElement("style");
    style.setAttribute("data-owner", "dc-chatbot-widget");
    style.textContent = `
      #${ROOT_ID} {
        position: fixed;
        right: 24px;
        bottom: 24px;
        z-index: 2147483000;
        font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
        color: #0f172a;
      }

      #${ROOT_ID} .dc-widget-shell {
        position: relative;
      }

      #${ROOT_ID} .dc-launcher {
        width: 64px;
        height: 64px;
        border: none;
        border-radius: 999px;
        background: linear-gradient(135deg, #003087, #0057d9);
        box-shadow: 0 18px 44px rgba(0, 48, 135, 0.32);
        color: #ffffff;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 16px;
        font-weight: 700;
        letter-spacing: 0.02em;
      }

      #${ROOT_ID} .dc-panel {
        position: absolute;
        right: 0;
        bottom: 78px;
        width: min(380px, calc(100vw - 32px));
        height: min(620px, calc(100vh - 120px));
        display: flex;
        flex-direction: column;
        border-radius: 24px;
        overflow: hidden;
        background: #f8fafc;
        border: 1px solid rgba(148, 163, 184, 0.25);
        box-shadow: 0 24px 60px rgba(15, 23, 42, 0.20);
      }

      #${ROOT_ID} .dc-hidden {
        display: none;
      }

      #${ROOT_ID} .dc-header {
        padding: 18px 20px;
        background: linear-gradient(135deg, #003087, #0f4dbd);
        color: #ffffff;
      }

      #${ROOT_ID} .dc-title {
        margin: 0;
        font-size: 18px;
        font-weight: 700;
      }

      #${ROOT_ID} .dc-subtitle {
        margin: 6px 0 0;
        font-size: 13px;
        opacity: 0.88;
      }

      #${ROOT_ID} .dc-header-top {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 8px;
      }

      #${ROOT_ID} .dc-clear-chat {
        border: 1px solid rgba(255, 255, 255, 0.55);
        background: transparent;
        color: #ffffff;
        border-radius: 10px;
        font-size: 11px;
        line-height: 1;
        padding: 6px 8px;
        cursor: pointer;
      }

      #${ROOT_ID} .dc-clear-chat:hover {
        background: rgba(255, 255, 255, 0.16);
      }

      #${ROOT_ID} .dc-status {
        margin-top: 8px;
        font-size: 12px;
        opacity: 0.90;
      }

      #${ROOT_ID} .dc-log {
        flex: 1;
        overflow-y: auto;
        padding: 18px;
        background: linear-gradient(180deg, #eff6ff 0%, #f8fafc 100%);
        display: flex;
        flex-direction: column;
        gap: 12px;
      }

      #${ROOT_ID} .dc-message {
        max-width: 85%;              /* Restricts maximum expansion */
        padding: 12px 14px;
        border-radius: 18px;
        font-size: 14px;
        line-height: 1.45;
        white-space: pre-line;       /* Hard-enforces and honors prompt linebreaks */
        word-wrap: break-word;       /* Breaks long continuous text strings */
        overflow-wrap: anywhere;     /* Double fallback shield preventing UI clipping */
      }

      #${ROOT_ID} .dc-message-user {
        align-self: flex-end;
        background: #003087;
        color: #ffffff;
        border-bottom-right-radius: 6px;
      }

      #${ROOT_ID} .dc-message-bot {
        align-self: flex-start;
        background: #ffffff;
        color: #0f172a;
        border-bottom-left-radius: 6px;
        box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08);
      }

      #${ROOT_ID} .dc-message-error {
        align-self: flex-start;
        background: #fef2f2;
        color: #991b1b;
        border: 1px solid #fecaca;
      }

      #${ROOT_ID} .dc-progress-card {
        margin-top: 10px;
        border: 1px solid #cbd5e1;
        background: #f8fafc;
        border-radius: 12px;
        padding: 10px;
      }

      #${ROOT_ID} .dc-progress-title {
        margin: 0 0 8px;
        font-size: 12px;
        font-weight: 700;
        color: #1e3a8a;
      }

      #${ROOT_ID} .dc-progress-list {
        margin: 0;
        padding-left: 0;
        list-style: none;
        display: flex;
        flex-direction: column;
        gap: 8px;
      }

      #${ROOT_ID} .dc-progress-item {
        display: flex;
        align-items: flex-start;
        gap: 8px;
      }

      #${ROOT_ID} .dc-progress-item label {
        cursor: pointer;
      }

      #${ROOT_ID} .dc-progress-meta {
        display: block;
        font-size: 11px;
        color: #475569;
      }

      #${ROOT_ID} .dc-loading-skeleton {
        width: 180px;
        height: 14px;
        border-radius: 999px;
        background: linear-gradient(
          90deg,
          rgba(148, 163, 184, 0.25) 25%,
          rgba(148, 163, 184, 0.45) 50%,
          rgba(148, 163, 184, 0.25) 75%
        );
        background-size: 220% 100%;
        animation: dc-skeleton-shimmer 1.2s linear infinite;
      }

      #${ROOT_ID} .dc-loading-skeleton + .dc-loading-skeleton {
        margin-top: 8px;
        width: 120px;
      }

      #${ROOT_ID} .dc-warning-note {
        margin-top: 6px;
        font-size: 11px;
        color: #b91c1c;
      }

      #${ROOT_ID} .dc-shake {
        animation: dc-shake 0.35s linear;
      }

      @keyframes dc-skeleton-shimmer {
        from {
          background-position: 200% 0;
        }
        to {
          background-position: -20% 0;
        }
      }

      @keyframes dc-shake {
        0% { transform: translateX(0); }
        20% { transform: translateX(-4px); }
        40% { transform: translateX(4px); }
        60% { transform: translateX(-3px); }
        80% { transform: translateX(3px); }
        100% { transform: translateX(0); }
      }

      #${ROOT_ID} .dc-composer {
        display: flex;
        gap: 10px;
        padding: 16px;
        border-top: 1px solid rgba(148, 163, 184, 0.20);
        background: #ffffff;
      }

      #${ROOT_ID} .dc-input {
        flex: 1;
        min-height: 48px;
        max-height: 120px;
        resize: vertical;
        border: 1px solid #cbd5e1;
        border-radius: 14px;
        padding: 12px 14px;
        font: inherit;
        color: #0f172a;
        background: #f8fafc;
      }

      #${ROOT_ID} .dc-input:focus {
        outline: 2px solid rgba(0, 87, 217, 0.22);
        border-color: #0057d9;
      }

      #${ROOT_ID} .dc-send {
        min-width: 84px;
        border: none;
        border-radius: 14px;
        background: #0057d9;
        color: #ffffff;
        padding: 0 16px;
        font: inherit;
        font-weight: 700;
        cursor: pointer;
      }

      #${ROOT_ID} .dc-send:disabled,
      #${ROOT_ID} .dc-input:disabled,
      #${ROOT_ID} .dc-launcher:disabled {
        cursor: not-allowed;
        opacity: 0.65;
      }

      @media (max-width: 540px) {
        #${ROOT_ID} {
          right: 12px;
          bottom: 12px;
          left: 12px;
        }

        #${ROOT_ID} .dc-panel {
          right: 0;
          left: 0;
          width: auto;
          height: min(72vh, 620px);
        }

        #${ROOT_ID} .dc-launcher {
          margin-left: auto;
        }
      }
    `;
    document.head.appendChild(style);
  }

  mountStyles();

  const root = createElement("div");
  root.id = ROOT_ID;

  const shell = createElement("div", "dc-widget-shell");
  const panel = createElement("section", "dc-panel dc-hidden");
  panel.setAttribute("aria-label", "Dallas College chatbot");
  panel.setAttribute("aria-live", "polite");

  const launcher = createElement("button", "dc-launcher");
  launcher.type = "button";
  launcher.setAttribute("aria-expanded", "false");
  launcher.setAttribute("aria-controls", "dc-chatbot-panel");
  launcher.id = "dc-chatbot-launcher";
  setSafeText(launcher, "Chat");

  panel.id = "dc-chatbot-panel";

  const header = createElement("header", "dc-header");
  const headerTop = createElement("div", "dc-header-top");
  const title = createElement("h2", "dc-title");
  const clearButton = /** @type {HTMLButtonElement} */ (createElement("button", "dc-clear-chat"));
  const subtitle = createElement("p", "dc-subtitle");
  const statusText = createElement("p", "dc-status");
  setSafeText(title, "Dallas College Advisor");
  clearButton.type = "button";
  clearButton.setAttribute("aria-label", "Clear chat history");
  clearButton.title = "Clear Chat";
  setSafeText(clearButton, "X");
  setSafeText(subtitle, "Grounded in the local catalog cache.");
  setSafeText(statusText, "Ready");
  headerTop.append(title, clearButton);
  header.append(headerTop, subtitle, statusText);

  const log = createElement("div", "dc-log");
  log.setAttribute("role", "log");
  log.setAttribute("aria-live", "polite");

  const composer = createElement("div", "dc-composer");
  const input = /** @type {HTMLTextAreaElement} */ (createElement("textarea", "dc-input"));
  input.rows = 1;
  input.maxLength = MAX_MESSAGE_LENGTH;
  input.placeholder = "Ask about courses, credits, or programs...";
  input.setAttribute("aria-label", "Message Dallas College advisor");

  const sendButton = /** @type {HTMLButtonElement} */ (createElement("button", "dc-send"));
  sendButton.type = "button";
  setSafeText(sendButton, "Send");
  composer.append(input, sendButton);

  panel.append(header, log, composer);
  shell.append(panel, launcher);
  root.appendChild(shell);
  document.body.appendChild(root);

  /** @type {HTMLElement | null} */
  let typingMessage = null;
  /** @type {Array<{role: "user"|"bot"|"error", kind: "text"|"progress", text: string, useMarkdown?: boolean, progressCards?: Array<{program_id?: string, title?: string, courses?: Array<{semester?: string, code?: string, title?: string, credits?: string|number, completed?: boolean}>}>, prerequisiteTree?: Object.<string, Array<string>>}>} */
  let conversationHistory = [];

  /**
   * Persist in-memory chat history to localStorage.
   *
   * @returns {void}
   */
  function saveChatHistory() {
    try {
      localStorage.setItem(CHAT_HISTORY_KEY, JSON.stringify(conversationHistory));
    } catch (error) {
      console.warn("[DCChatbot] Unable to persist chat history.", error);
    }
  }

  /**
   * Re-hydrate chat bubbles from localStorage if available.
   *
   * @returns {void}
   */
  function loadChatHistory() {
    try {
      const raw = localStorage.getItem(CHAT_HISTORY_KEY);
      if (!raw) {
        return;
      }

      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) {
        return;
      }

      conversationHistory = parsed;
      for (let i = 0; i < conversationHistory.length; i += 1) {
        const entry = conversationHistory[i];
        if (!entry || typeof entry !== "object") {
          continue;
        }
        if (entry.kind === "progress") {
          appendProgressCardMessage(
            entry.text || "",
            entry.role || "bot",
            entry.progressCards || [],
            false,
            i,
            entry.prerequisiteTree || {},
          );
        } else {
          appendMessage(entry.text || "", entry.role || "bot", Boolean(entry.useMarkdown), false);
        }
      }
    } catch (error) {
      console.warn("[DCChatbot] Unable to load chat history.", error);
    }
  }

  /**
   * Snap the chat log to the latest content with smooth animation.
   *
   * @returns {void}
   */
  function scrollToBottom() {
    log.scrollTo({ top: log.scrollHeight, behavior: "smooth" });
  }

  /**
   * Append a message bubble safely.
   *
   * @param {string} text
   * @param {"user"|"bot"|"error"} role
   * @returns {HTMLElement}
   */
  function appendMessage(text, role, useMarkdown = false, shouldPersist = true) {
    const bubble = createElement("div", `dc-message dc-message-${role}`);
    if (useMarkdown) {
      bubble.innerHTML = formatMarkdown(text);
    } else {
      setSafeText(bubble, text);
    }
    log.appendChild(bubble);
    scrollToBottom();

    if (shouldPersist) {
      conversationHistory.push({ role, kind: "text", text, useMarkdown });
      saveChatHistory();
    }

    return bubble;
  }

  /**
   * Render interactive checklist cards in a bot bubble.
   *
   * @param {string} text
   * @param {"user"|"bot"|"error"} role
   * @param {Array<{program_id?: string, title?: string, courses?: Array<{semester?: string, code?: string, title?: string, credits?: string|number, completed?: boolean}>}>} progressCards
   * @param {boolean=} shouldPersist
   * @param {number=} historyIndexOverride
   * @param {Object.<string, Array<string>>=} prerequisiteTree
   * @returns {HTMLElement}
   */
  function appendProgressCardMessage(
    text,
    role,
    progressCards,
    shouldPersist = true,
    historyIndexOverride = -1,
    prerequisiteTree = {},
  ) {
    const bubble = createElement("div", `dc-message dc-message-${role}`);
    const intro = createElement("div");
    intro.innerHTML = formatMarkdown(text);
    bubble.appendChild(intro);

    const entryIndex = shouldPersist ? conversationHistory.length : historyIndexOverride;

    progressCards.forEach((card, cardIndex) => {
      const cardContainer = createElement("section", "dc-progress-card");
      const cardTitle = createElement("h4", "dc-progress-title");
      setSafeText(cardTitle, card.title || card.program_id || "Degree Plan");
      cardContainer.appendChild(cardTitle);

      const courseList = createElement("ul", "dc-progress-list");
      const courses = Array.isArray(card.courses) ? card.courses : [];

      courses.forEach((course, courseIndex) => {
        const listItem = createElement("li", "dc-progress-item");
        const checkbox = /** @type {HTMLInputElement} */ (createElement("input"));
        checkbox.type = "checkbox";
        checkbox.checked = Boolean(course.completed);
        const warning = createElement("small", "dc-warning-note");

        const label = createElement("label");
        const main = createElement("span");
        const meta = createElement("span", "dc-progress-meta");
        setSafeText(main, `${course.code || "COURSE"}: ${course.title || "Title"}`);
        setSafeText(meta, `${course.semester || "Requirements"} • ${course.credits || "?"} credits`);
        label.append(main, meta);

        checkbox.addEventListener("change", () => {
          if (entryIndex < 0 || entryIndex >= conversationHistory.length) {
            return;
          }
          const historyEntry = conversationHistory[entryIndex];
          if (!historyEntry || historyEntry.kind !== "progress" || !Array.isArray(historyEntry.progressCards)) {
            return;
          }
          const historyCard = historyEntry.progressCards[cardIndex];
          if (!historyCard || !Array.isArray(historyCard.courses)) {
            return;
          }
          if (!historyCard.courses[courseIndex]) {
            return;
          }

          const courseCode = String(course.code || "").trim();
          if (checkbox.checked && courseCode) {
            const required = Array.isArray(prerequisiteTree[courseCode])
              ? prerequisiteTree[courseCode]
              : [];
            
            if (required.length > 0) {
              const completed = new Set(
                historyCard.courses
                  .filter((candidateCourse) => Boolean(candidateCourse.completed))
                  .map((candidateCourse) => String(candidateCourse.code || "").trim()),
              );
              const missing = required.filter((neededCode) => !completed.has(neededCode));
              
              if (missing.length > 0) {
                checkbox.checked = false;
                warning.textContent = `Requires ${missing.join(", ")} first!`;
                listItem.classList.add("dc-shake");
                setTimeout(() => {
                  listItem.classList.remove("dc-shake");
                }, 400);
                return;
              }
            }
          }

          warning.textContent = "";
          historyCard.courses[courseIndex].completed = checkbox.checked;
          saveChatHistory();
        });

        listItem.append(checkbox, label, warning);
        courseList.appendChild(listItem);
      });

      cardContainer.appendChild(courseList);
      bubble.appendChild(cardContainer);
    });

    log.appendChild(bubble);
    scrollToBottom();

    if (shouldPersist) {
      conversationHistory.push({
        role,
        kind: "progress",
        text,
        progressCards,
        prerequisiteTree,
      });
      saveChatHistory();
    }

    return bubble;
  }

  /**
   * Set interactive controls enabled state.
   *
   * @param {boolean} enabled
   * @returns {void}
   */
  function setInteractiveState(enabled) {
    input.disabled = !enabled;
    sendButton.disabled = !enabled;
  }

  /**
   * Toggle the panel open state.
   *
   * @param {boolean} open
   * @returns {void}
   */
  function setPanelOpen(open) {
    panel.classList.toggle("dc-hidden", !open);
    launcher.setAttribute("aria-expanded", String(open));
    if (open) {
      input.focus();
      scrollToBottom();
    }
  }

  /**
   * Update the status line text.
   *
   * @param {string} text
   * @returns {void}
   */
  function setStatus(text) {
    setSafeText(statusText, text);
  }

  /**
   * Show or hide the typing placeholder.
   *
   * @param {boolean} visible
   * @returns {void}
   */
  function setTypingIndicator(visible) {
    if (visible && !typingMessage) {
      typingMessage = createElement("div", "dc-message dc-message-bot");
      const lineOne = createElement("div", "dc-loading-skeleton");
      const lineTwo = createElement("div", "dc-loading-skeleton");
      typingMessage.append(lineOne, lineTwo);
      log.appendChild(typingMessage);
      scrollToBottom();
      typingMessage.dataset.typing = "true";
      return;
    }

    if (!visible && typingMessage) {
      typingMessage.remove();
      typingMessage = null;
    }
  }

  /**
   * Send the chat request to the local FastAPI backend.
   *
   * @param {string} message
   * @returns {Promise<{reply: string, model: string, progress_cards?: Array<{program_id?: string, title?: string, courses?: Array<{semester?: string, code?: string, title?: string, credits?: string|number, completed?: boolean}>}>, prerequisite_tree?: Object.<string, Array<string>>}>}
   */
  async function postMessage(message) {
    console.log(
      "[DIAGNOSTIC] Dispatching POST fetch request to destination:",
      API_URL,
      "with message string:",
      message,
    );

    const response = await fetch(API_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ message }),
    });

    if (!response.ok) {
      let detail = `Request failed with status ${response.status}.`;
      try {
        const rawText = await response.text();
        try {
          const payload = JSON.parse(rawText);
          if (payload && typeof payload.detail === "string" && payload.detail) {
            detail = payload.detail;
          } else {
            detail = rawText || detail;
          }
        } catch {
          detail = rawText || detail;
        }
      } catch (streamError) {
        console.warn("[DCChatbot] Could not read response stream.", streamError);
      }
      throw new Error(detail);
    }

    return response.json();
  }

  /**
   * Validate the current input and normalize it.
   *
   * @returns {string | null}
   */
  function readValidatedMessage() {
    const rawMessage = input.value.trim();
    if (!rawMessage) {
      return null;
    }

    if (rawMessage.length > MAX_MESSAGE_LENGTH) {
      appendMessage(
        `Message exceeds ${MAX_MESSAGE_LENGTH} characters. Please shorten it.`,
        "error",
      );
      setStatus("Input too long");
      return null;
    }

    return rawMessage;
  }

  /**
   * Submit the current message to the backend.
   *
   * @returns {Promise<void>}
   */
  async function handleSubmit() {
    const message = readValidatedMessage();
    if (!message) {
      return;
    }

    appendMessage(message, "user");
    input.value = "";
    setInteractiveState(false);
    setTypingIndicator(true);
    setStatus("Connecting...");

    try {
      const payload = await postMessage(message);
      setTypingIndicator(false);
      if (Array.isArray(payload.progress_cards) && payload.progress_cards.length > 0) {
        appendProgressCardMessage(
          payload.reply,
          "bot",
          payload.progress_cards,
          true,
          -1,
          payload.prerequisite_tree || {},
        );
      } else {
        appendMessage(payload.reply, "bot", true);
      }
      setStatus(`Answered by ${payload.model}`);
    } catch (error) {
      setTypingIndicator(false);
      const messageText =
        error instanceof Error
          ? error.message
          : typeof error === "string" && error
            ? error
            : "Connection error.";
      appendMessage(`Connection error: ${messageText}`, "error");
      setStatus("Connection error");
      console.error("[DCChatbot] Chat request failed.", error);
    } finally {
      setInteractiveState(true);
      input.focus();
    }
  }

  launcher.addEventListener("click", () => {
    const nextOpenState = panel.classList.contains("dc-hidden");
    setPanelOpen(nextOpenState);
  });

  sendButton.addEventListener("click", (event) => {
    event.preventDefault();
    void handleSubmit();
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void handleSubmit();
    }
  });

  clearButton.addEventListener("click", () => {
    conversationHistory = [];
    localStorage.removeItem(CHAT_HISTORY_KEY);
    log.textContent = "";
    appendMessage(
      "Chat history cleared.\n\nGreetings, I am the automated AI Advisor running on the Dallas College AI Club Sandbox Engine.\n\n(This application is a student-led AI Club sandbox demo and is not an officially sanctioned tool of Dallas College. For binding degree planning and institutional support, connect directly with a human advisor at the Official Dallas College Support Directory: https://www.dallascollege.edu/contact.)\n\nAsk me about programs, course codes, or credit hours from our sandbox catalog!",
      "bot",
      false,
      false,
    );
    setStatus("History cleared");
  });

  loadChatHistory();
  if (!conversationHistory.length) {
    appendMessage(
      "Ask about programs, course codes, or credit hours from the cached Dallas College catalog.",
      "bot",
    );
  }
})();