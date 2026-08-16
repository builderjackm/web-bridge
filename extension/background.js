const DAEMON_URL = "ws://127.0.0.1:9222/extension";
const RECONNECT_ALARM = "webbridge-reconnect";
const RECONNECT_MAX_MS = 10000;
const HEARTBEAT_MS = 20000;
const LOG_STORAGE_KEY = "webbridgeDiagnosticLogs";
const LOG_LIMIT = 200;
const WORKER_INSTANCE_ID = crypto.randomUUID();

const bridgeAttachedTabs = new Set();

let socket = null;
let reconnectTimer = null;
let heartbeatTimer = null;
let reconnectDelay = 500;
let logWriteChain = Promise.resolve();

function send(payload) {
  if (socket?.readyState !== WebSocket.OPEN) return false;
  socket.send(JSON.stringify(payload));
  return true;
}

function emit(method, params = []) {
  return send({ method, params });
}

function diagnosticEntry(event, details = {}) {
  return {
    timestamp: new Date().toISOString(),
    instanceId: WORKER_INSTANCE_ID,
    event,
    details,
  };
}

function persistLog(entry) {
  logWriteChain = logWriteChain
    .then(async () => {
      const stored = await chrome.storage.local.get(LOG_STORAGE_KEY);
      const logs = Array.isArray(stored[LOG_STORAGE_KEY])
        ? stored[LOG_STORAGE_KEY]
        : [];
      logs.push(entry);
      await chrome.storage.local.set({
        [LOG_STORAGE_KEY]: logs.slice(-LOG_LIMIT),
      });
    })
    .catch((error) => console.warn("WebBridge failed to persist diagnostics", error));
}

function recordLog(event, details = {}) {
  const entry = diagnosticEntry(event, details);
  console.info("WebBridge diagnostic", entry);
  if (!emit("bridge.log", [entry])) persistLog(entry);
}

async function flushStoredLogs() {
  await logWriteChain;
  const stored = await chrome.storage.local.get(LOG_STORAGE_KEY);
  const logs = stored[LOG_STORAGE_KEY];
  if (!Array.isArray(logs) || logs.length === 0) return;
  if (emit("bridge.logs", [logs])) {
    await chrome.storage.local.remove(LOG_STORAGE_KEY);
  }
}

function setBadge(connected) {
  chrome.action.setBadgeText({ text: connected ? "ON" : "" });
  chrome.action.setBadgeBackgroundColor({ color: connected ? "#16803c" : "#6b7280" });
  chrome.action.setTitle({
    title: connected
      ? "WebBridge CDP: connected on 127.0.0.1:9222"
      : "WebBridge CDP: daemon disconnected",
  });
}

async function attachDebuggee(debuggee, protocolVersion) {
  if (Number.isInteger(debuggee?.tabId)) {
    const targets = await chrome.debugger.getTargets();
    const target = targets.find((item) => item.tabId === debuggee.tabId);
    if (target?.attached) {
      const identifiers = [debuggee, { targetId: target.id }];
      for (const identifier of identifiers) {
        try {
          await chrome.debugger.sendCommand(identifier, "Target.getTargetInfo", {});
          bridgeAttachedTabs.add(debuggee.tabId);
          return;
        } catch {
          // Try the other identifier before treating it as another debugger.
        }
      }
    }
  }
  await chrome.debugger.attach(debuggee, protocolVersion);
  if (Number.isInteger(debuggee?.tabId)) bridgeAttachedTabs.add(debuggee.tabId);
}

async function detachDebuggee(debuggee) {
  await chrome.debugger.detach(debuggee);
  if (Number.isInteger(debuggee?.tabId)) bridgeAttachedTabs.delete(debuggee.tabId);
}

async function detachBridgeTabs() {
  const targets = await chrome.debugger.getTargets();
  const tabIds = new Set(bridgeAttachedTabs);
  for (const target of targets) {
    if (!target.attached || !Number.isInteger(target.tabId)) continue;
    try {
      await chrome.debugger.sendCommand(
        { targetId: target.id },
        "Target.getTargetInfo",
        {},
      );
      tabIds.add(target.tabId);
    } catch {
      // This target belongs to another debugger.
    }
  }
  bridgeAttachedTabs.clear();
  await Promise.allSettled(
    [...tabIds].map((tabId) => chrome.debugger.detach({ tabId })),
  );
  return tabIds.size;
}

const rpcMethods = new Map([
  ["chrome.debugger.attach", attachDebuggee],
  ["chrome.debugger.detach", detachDebuggee],
  ["chrome.debugger.sendCommand", (...args) => chrome.debugger.sendCommand(...args)],
  ["chrome.debugger.getTargets", (...args) => chrome.debugger.getTargets(...args)],
  ["chrome.tabs.query", (...args) => chrome.tabs.query(...args)],
  ["chrome.tabs.create", (...args) => chrome.tabs.create(...args)],
  ["chrome.tabs.remove", (...args) => chrome.tabs.remove(...args)],
  ["chrome.tabs.update", (...args) => chrome.tabs.update(...args)],
  ["chrome.windows.update", (...args) => chrome.windows.update(...args)],
]);

async function handleRpc(message) {
  const handler = rpcMethods.get(message.method);
  if (!handler) {
    recordLog("rpc.unsupported", { method: message.method });
    return { error: { message: `Unsupported extension RPC: ${message.method}` } };
  }
  try {
    const result = await handler(...(Array.isArray(message.params) ? message.params : []));
    return { result: result ?? null };
  } catch (error) {
    const messageText = error instanceof Error ? error.message : String(error);
    recordLog("rpc.error", { method: message.method, message: messageText });
    return { error: { message: messageText } };
  }
}

async function announceReady() {
  const [tabs, targets] = await Promise.all([
    chrome.tabs.query({}),
    chrome.debugger.getTargets(),
  ]);
  emit("bridge.ready", [{
    tabs,
    targets,
    browser: { userAgent: navigator.userAgent },
  }]);
  recordLog("bridge.ready", { tabs: tabs.length, targets: targets.length });
}

async function handleRequest(message) {
  const response = await handleRpc(message);
  send({ id: message.id, ...response });
}

function clearHeartbeat() {
  if (heartbeatTimer !== null) clearInterval(heartbeatTimer);
  heartbeatTimer = null;
}

function scheduleReconnect() {
  if (reconnectTimer !== null) return;
  const delay = reconnectDelay;
  recordLog("socket.reconnect_scheduled", { delayMs: delay });
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect("timer");
  }, delay);
  reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
}

function connect(trigger = "worker") {
  if (socket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(socket.readyState)) return;
  recordLog("socket.connecting", { trigger, reconnectDelayMs: reconnectDelay });
  const current = new WebSocket(DAEMON_URL);
  socket = current;

  current.addEventListener("open", async () => {
    if (socket !== current) return;
    reconnectDelay = 500;
    if (reconnectTimer !== null) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    setBadge(true);
    recordLog("socket.open");
    try {
      await flushStoredLogs();
      await announceReady();
    } catch (error) {
      recordLog("bridge.ready_error", {
        message: error instanceof Error ? error.message : String(error),
      });
    }
    clearHeartbeat();
    heartbeatTimer = setInterval(
      () => emit("bridge.ping", [Date.now()]),
      HEARTBEAT_MS,
    );
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

  current.addEventListener("close", (event) => {
    if (socket !== current) return;
    socket = null;
    clearHeartbeat();
    setBadge(false);
    recordLog("socket.close", {
      code: event.code,
      reason: event.reason,
      wasClean: event.wasClean,
    });
    void detachBridgeTabs()
      .then((count) => recordLog("debugger.cleanup", { tabs: count }))
      .catch((error) => {
        recordLog("debugger.cleanup_error", {
          message: error instanceof Error ? error.message : String(error),
        });
      });
    scheduleReconnect();
  });

  current.addEventListener("error", () => {
    recordLog("socket.error");
    current.close();
  });
}

function ensureReconnectAlarm() {
  void chrome.alarms.create(RECONNECT_ALARM, { periodInMinutes: 1 });
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  emit("chrome.debugger.onEvent", [source, method, params ?? {}]);
});

chrome.debugger.onDetach.addListener((source, reason) => {
  emit("chrome.debugger.onDetach", [source, reason]);
});

chrome.tabs.onCreated.addListener((tab) => {
  emit("chrome.tabs.onCreated", [tab]);
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  emit("chrome.tabs.onUpdated", [tabId, changeInfo, tab]);
});

chrome.tabs.onRemoved.addListener((tabId, removeInfo) => {
  emit("chrome.tabs.onRemoved", [tabId, removeInfo]);
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== RECONNECT_ALARM) return;
  if (!socket || socket.readyState === WebSocket.CLOSED) {
    recordLog("alarm.reconnect");
    connect("alarm");
  }
});

chrome.runtime.onStartup.addListener(() => {
  recordLog("worker.startup");
  connect("startup");
});

chrome.runtime.onInstalled.addListener((details) => {
  recordLog("worker.installed", { reason: details.reason });
  connect("installed");
});

setBadge(false);
ensureReconnectAlarm();
recordLog("worker.started", { userAgent: navigator.userAgent });
connect();
