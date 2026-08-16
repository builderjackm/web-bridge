const DAEMON_URL = "ws://127.0.0.1:9222/extension";
const RECONNECT_MAX_MS = 10000;

let socket = null;
let reconnectTimer = null;
let heartbeatTimer = null;
let reconnectDelay = 500;

function send(payload) {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload));
  }
}

function emit(method, params = []) {
  send({ method, params });
}

async function extensionRpc(method, params = []) {
  return await chrome.runtime.sendMessage({ type: "bridge-rpc", method, params });
}

async function announceReady() {
  const [tabsResponse, targetsResponse] = await Promise.all([
    extensionRpc("chrome.tabs.query", [{}]),
    extensionRpc("chrome.debugger.getTargets", []),
  ]);
  if (tabsResponse?.error) throw new Error(tabsResponse.error.message);
  if (targetsResponse?.error) throw new Error(targetsResponse.error.message);
  emit("bridge.ready", [{
    tabs: tabsResponse.result,
    targets: targetsResponse.result,
    browser: { userAgent: navigator.userAgent },
  }]);
}

async function handleRequest(message) {
  const response = await extensionRpc(message.method, message.params);
  send({ id: message.id, ...(response ?? { error: { message: "No RPC response" } }) });
}

function setStatus(connected) {
  chrome.runtime.sendMessage({ type: "bridge-status", connected }).catch(() => {});
}

function scheduleReconnect() {
  if (reconnectTimer !== null) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
}

function connect() {
  if (socket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(socket.readyState)) return;
  const current = new WebSocket(DAEMON_URL);
  socket = current;

  current.addEventListener("open", async () => {
    if (socket !== current) return;
    reconnectDelay = 500;
    setStatus(true);
    try {
      await announceReady();
    } catch (error) {
      console.warn("WebBridge failed to enumerate browser targets", error);
    }
    clearInterval(heartbeatTimer);
    heartbeatTimer = setInterval(() => emit("bridge.ping", [Date.now()]), 20000);
  });

  current.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      current.close(1003, "invalid JSON");
      return;
    }
    if (Number.isInteger(message.id) && typeof message.method === "string") {
      void handleRequest(message);
    }
  });

  const disconnected = () => {
    if (socket !== current) return;
    socket = null;
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
    setStatus(false);
    scheduleReconnect();
  };
  current.addEventListener("close", disconnected);
  current.addEventListener("error", () => current.close());
}

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === "bridge-event") {
    emit(message.method, message.params);
  }
});

connect();
