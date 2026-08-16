const OFFSCREEN_PATH = "offscreen.html";
const bridgeAttachedTabs = new Set();

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

let creatingOffscreen = null;

async function ensureOffscreen() {
  const documentUrl = chrome.runtime.getURL(OFFSCREEN_PATH);
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
    documentUrls: [documentUrl],
  });
  if (contexts.length > 0) return;
  if (!creatingOffscreen) {
    creatingOffscreen = chrome.offscreen.createDocument({
      url: OFFSCREEN_PATH,
      reasons: ["WORKERS"],
      justification: "Maintain the local CDP bridge connection.",
    }).finally(() => {
      creatingOffscreen = null;
    });
  }
  await creatingOffscreen;
}

async function handleRpc(message) {
  const handler = rpcMethods.get(message.method);
  if (!handler) {
    return { error: { message: `Unsupported extension RPC: ${message.method}` } };
  }
  try {
    const result = await handler(...(Array.isArray(message.params) ? message.params : []));
    return { result: result ?? null };
  } catch (error) {
    return {
      error: { message: error instanceof Error ? error.message : String(error) },
    };
  }
}

function setBadge(connected) {
  chrome.action.setBadgeText({ text: connected ? "ON" : "" });
  chrome.action.setBadgeBackgroundColor({ color: connected ? "#16803c" : "#6b7280" });
  chrome.action.setTitle({
    title: connected ? "WebBridge CDP: connected on 127.0.0.1:9222" : "WebBridge CDP: daemon disconnected",
  });
}

function forwardEvent(method, params) {
  chrome.runtime.sendMessage({ type: "bridge-event", method, params }).catch(() => {});
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "bridge-rpc") {
    handleRpc(message).then(sendResponse);
    return true;
  }
  if (message?.type === "bridge-status") {
    const connected = Boolean(message.connected);
    setBadge(connected);
    if (!connected) {
      detachBridgeTabs().finally(() => sendResponse({ ok: true }));
      return true;
    }
  }
  return false;
});

chrome.debugger.onEvent.addListener((source, method, params) => {
  forwardEvent("chrome.debugger.onEvent", [source, method, params ?? {}]);
});

chrome.debugger.onDetach.addListener((source, reason) => {
  forwardEvent("chrome.debugger.onDetach", [source, reason]);
});

chrome.tabs.onCreated.addListener((tab) => {
  forwardEvent("chrome.tabs.onCreated", [tab]);
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  forwardEvent("chrome.tabs.onUpdated", [tabId, changeInfo, tab]);
});

chrome.tabs.onRemoved.addListener((tabId, removeInfo) => {
  forwardEvent("chrome.tabs.onRemoved", [tabId, removeInfo]);
});

chrome.runtime.onStartup.addListener(() => void ensureOffscreen());
chrome.runtime.onInstalled.addListener(() => void ensureOffscreen());
setBadge(false);
void ensureOffscreen();
