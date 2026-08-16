const STATUS_URL = "http://127.0.0.1:9333/";
const REFRESH_INTERVAL_MS = 2000;

const statusLabel = document.querySelector("#status-label");
const statusDetail = document.querySelector("#status-detail");
const clientCount = document.querySelector("#client-count");
const targetCount = document.querySelector("#target-count");
const updatedAt = document.querySelector("#updated-at");
const refreshButton = document.querySelector("#refresh");

let requestInFlight = false;

function showStatus(state, label, detail, status = null) {
  document.body.dataset.state = state;
  statusLabel.textContent = label;
  statusDetail.textContent = detail;
  clientCount.textContent = status?.cdpClients ?? "—";
  targetCount.textContent = status?.pageTargets ?? status?.targets ?? "—";
  updatedAt.textContent = `刚刚更新 · ${new Date().toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  })}`;
}

async function refreshStatus() {
  if (requestInFlight) return;
  requestInFlight = true;
  refreshButton.dataset.loading = "true";
  refreshButton.disabled = true;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 1200);
  try {
    const response = await fetch(STATUS_URL, {
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const status = await response.json();
    if (status?.name !== "webbridge") throw new Error("Unexpected daemon");

    if (status.extensionConnected && status.pageTargets == null) {
      try {
        const targetsResponse = await fetch(`${STATUS_URL}json/list`, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (targetsResponse.ok) {
          const targets = await targetsResponse.json();
          if (Array.isArray(targets)) {
            status.pageTargets = targets.filter((target) =>
              ["page", "webview"].includes(target?.type),
            ).length;
          }
        }
      } catch {
        // Older daemons still show their aggregate target count as a fallback.
      }
    }

    if (status.extensionConnected) {
      showStatus(
        "connected",
        "已连接",
        "浏览器扩展已连接到 Daemon",
        status,
      );
    } else {
      showStatus(
        "waiting",
        "等待浏览器连接",
        "Daemon 正在运行，扩展正在重连",
        status,
      );
    }
  } catch {
    showStatus(
      "offline",
      "Daemon 未运行",
      "请先运行 webbridge start",
    );
  } finally {
    clearTimeout(timeout);
    refreshButton.dataset.loading = "false";
    refreshButton.disabled = false;
    requestInFlight = false;
  }
}

refreshButton.addEventListener("click", refreshStatus);
const refreshTimer = setInterval(refreshStatus, REFRESH_INTERVAL_MS);
window.addEventListener("unload", () => clearInterval(refreshTimer));
void refreshStatus();
