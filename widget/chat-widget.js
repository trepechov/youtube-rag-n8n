(function () {
  "use strict";

  // Config from script tag attributes
  const script = document.currentScript || (function () {
    const scripts = document.getElementsByTagName("script");
    return scripts[scripts.length - 1];
  })();

  const API_URL = (script.getAttribute("data-api-url") || "http://localhost:8000").replace(/\/$/, "");
  const COLLECTION = script.getAttribute("data-collection") || "podcasts";
  const TITLE = script.getAttribute("data-title") || "Ask the Podcast";
  const SUBTITLE = script.getAttribute("data-subtitle") || "AI-powered answers from transcript content";
  const MODEL = script.getAttribute("data-model") || "";

  // Inject CSS
  const cssLink = document.createElement("link");
  cssLink.rel = "stylesheet";
  cssLink.href = `${API_URL}/widget/chat-widget.css`;
  document.head.appendChild(cssLink);

  // Build DOM
  const container = document.createElement("div");
  container.id = "yt-rag-widget";
  container.innerHTML = `
    <button id="yt-rag-toggle" aria-label="Open chat">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
      </svg>
    </button>

    <div id="yt-rag-window" class="hidden">
      <div id="yt-rag-header">
        <div>
          <div id="yt-rag-header-title">${_esc(TITLE)}</div>
          <div id="yt-rag-header-subtitle">${_esc(SUBTITLE)}</div>
        </div>
        <button id="yt-rag-close" aria-label="Close chat">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>

      <div id="yt-rag-messages">
        <div class="yt-rag-msg bot">Hi! Ask me anything about the podcast content.</div>
      </div>

      <div id="yt-rag-input-area">
        <textarea
          id="yt-rag-input"
          rows="1"
          placeholder="Ask a question…"
          aria-label="Chat input"
        ></textarea>
        <button id="yt-rag-send" aria-label="Send">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="22" y1="2" x2="11" y2="13"/>
            <polygon points="22 2 15 22 11 13 2 9 22 2"/>
          </svg>
        </button>
      </div>
    </div>
  `;
  document.body.appendChild(container);

  // Elements
  const toggle = document.getElementById("yt-rag-toggle");
  const window_ = document.getElementById("yt-rag-window");
  const closeBtn = document.getElementById("yt-rag-close");
  const messages = document.getElementById("yt-rag-messages");
  const input = document.getElementById("yt-rag-input");
  const sendBtn = document.getElementById("yt-rag-send");

  // State
  let open = false;
  let loading = false;

  // Toggle open/close
  toggle.addEventListener("click", () => setOpen(!open));
  closeBtn.addEventListener("click", () => setOpen(false));

  function setOpen(value) {
    open = value;
    window_.classList.toggle("hidden", !open);
    toggle.innerHTML = open
      ? `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
           <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
         </svg>`
      : `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
           <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
         </svg>`;
    if (open) setTimeout(() => input.focus(), 50);
  }

  // Auto-resize textarea
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 100) + "px";
  });

  // Send on Enter (Shift+Enter = newline)
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!loading) send();
    }
  });

  sendBtn.addEventListener("click", () => { if (!loading) send(); });

  async function send() {
    const question = input.value.trim();
    if (!question) return;

    addMessage("user", question);
    input.value = "";
    input.style.height = "auto";

    const typingEl = addTyping();
    setLoading(true);

    try {
      const body = { question, collection: COLLECTION, top_k: 5 };
      if (MODEL) body.model = MODEL;

      const resp = await fetch(`${API_URL}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      typingEl.remove();

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        addMessage("error", `Error ${resp.status}: ${err.detail || resp.statusText}`);
        return;
      }

      const data = await resp.json();
      addBotMessage(data.answer, data.sources || []);
    } catch (err) {
      typingEl.remove();
      addMessage("error", "Network error — is the chat API running?");
    } finally {
      setLoading(false);
    }
  }

  function addMessage(type, text) {
    const el = document.createElement("div");
    el.className = `yt-rag-msg ${type}`;
    el.textContent = text;
    messages.appendChild(el);
    scrollBottom();
    return el;
  }

  function addBotMessage(text, sources) {
    const el = document.createElement("div");
    el.className = "yt-rag-msg bot";

    const textNode = document.createElement("div");
    textNode.textContent = text;
    el.appendChild(textNode);

    if (sources.length > 0) {
      const srcEl = document.createElement("div");
      srcEl.className = "yt-rag-sources";
      srcEl.textContent = "Sources:";

      const seen = new Set();
      sources.forEach((s) => {
        if (seen.has(s.video_id)) return;
        seen.add(s.video_id);
        const a = document.createElement("a");
        a.className = "yt-rag-source-link";
        a.href = s.url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = `▶ ${s.title}`;
        srcEl.appendChild(a);
      });

      el.appendChild(srcEl);
    }

    messages.appendChild(el);
    scrollBottom();
  }

  function addTyping() {
    const el = document.createElement("div");
    el.className = "yt-rag-typing";
    el.innerHTML = "<span></span><span></span><span></span>";
    messages.appendChild(el);
    scrollBottom();
    return el;
  }

  function setLoading(value) {
    loading = value;
    sendBtn.disabled = value;
    input.disabled = value;
  }

  function scrollBottom() {
    messages.scrollTop = messages.scrollHeight;
  }

  function _esc(str) {
    return str.replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }
})();
