/**
 * Dallas College Chatbot Widget.
 *
 * Architectural Intent:
 *   This file self-mounts an isolated floating chat widget into any page
 *   that includes it. The widget owns only presentation and transport,
 *   while all advisory logic stays in the FastAPI backend.
 *
 * Security Rationale:
 *   - All response rendering uses text nodes and textContent only.
 *   - No innerHTML, template injection, or HTML interpolation is used.
 *   - Network calls target a fixed local API endpoint and never include
 *     user-controlled URLs.
 */

(() => {
  "use strict";

  const API_URL = "http://localhost:8000/api/chat";
  const MAX_MESSAGE_LENGTH = 1000;
  const ROOT_ID = "dc-chatbot-root";

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
        max-width: 88%;
        padding: 12px 14px;
        border-radius: 18px;
        font-size: 14px;
        line-height: 1.45;
        white-space: pre-wrap;
        word-break: break-word;
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
  const title = createElement("h2", "dc-title");
  const subtitle = createElement("p", "dc-subtitle");
  const statusText = createElement("p", "dc-status");
  setSafeText(title, "Dallas College Advisor");
  setSafeText(subtitle, "Grounded in the local catalog cache.");
  setSafeText(statusText, "Ready");
  header.append(title, subtitle, statusText);

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

  /**
   * Append a message bubble safely.
   *
   * @param {string} text
   * @param {"user"|"bot"|"error"} role
   * @returns {HTMLElement}
   */
  function appendMessage(text, role) {
    const bubble = createElement("div", `dc-message dc-message-${role}`);
    setSafeText(bubble, text);
    log.appendChild(bubble);
    log.scrollTop = log.scrollHeight;
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
      log.scrollTop = log.scrollHeight;
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
      typingMessage = appendMessage("...typing", "bot");
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
   * @returns {Promise<{reply: string, model: string}>}
   */
  async function postMessage(message) {
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
        const payload = await response.json();
        if (payload && typeof payload.detail === "string" && payload.detail) {
          detail = payload.detail;
        }
      } catch (error) {
        const fallbackError = error;
        console.warn("[DCChatbot] Failed to parse error payload.", fallbackError);
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
      appendMessage(payload.reply, "bot");
      setStatus(`Answered by ${payload.model}`);
    } catch (error) {
      setTypingIndicator(false);
      const messageText = error instanceof Error ? error.message : "Connection error.";
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

  sendButton.addEventListener("click", () => {
    void handleSubmit();
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void handleSubmit();
    }
  });

  appendMessage(
    "Ask about programs, course codes, or credit hours from the cached Dallas College catalog.",
    "bot",
  );
})();
