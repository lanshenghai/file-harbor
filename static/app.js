const state = {
  sessionId: null,
  root: null,
  host: null,
  protocol: "smb",
  selected: new Map(),
};

const connectForm = document.getElementById("connect-form");
const downloadForm = document.getElementById("download-form");
const treeRoot = document.getElementById("tree-root");
const statusText = document.getElementById("status-text");
const sessionSummary = document.getElementById("session-summary");
const downloadQueue = document.getElementById("download-queue");
const selectionCount = document.getElementById("selection-count");
const selectAllBtn = document.getElementById("select-all");
const deselectAllBtn = document.getElementById("deselect-all");
const browseHint = document.getElementById("browse-hint");
const connectBtn = document.getElementById("connect-btn");
const downloadBtn = document.getElementById("download-btn");
const retrySftpBtn = document.getElementById("retry-sftp-btn");

function setStatus(message, isError = false) {
  statusText.textContent = message;
  statusText.style.color = isError ? "#ffa198" : "#e6edf3";
}

function updateSelectionCount() {
  const n = state.selected.size;
  selectionCount.textContent = `${n} selected`;
}

function syncProtocolVisibility() {
  state.protocol = connectForm.elements.protocol.value;
}

function formatSize(size) {
  if (size === null || size === undefined) {
    return "";
  }
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KiB`;
  }
  return `${(size / (1024 * 1024)).toFixed(1)} MiB`;
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const data = await response.json();
      if (typeof data.detail === "string") {
        detail = data.detail;
      } else if (Array.isArray(data.detail)) {
        detail = data.detail
          .map((item) => {
            const where = Array.isArray(item.loc)
              ? item.loc.filter((p) => p !== "body").join(".")
              : "";
            const msg = item.msg || JSON.stringify(item);
            return where ? `${where}: ${msg}` : msg;
          })
          .join("; ");
      } else if (data.detail) {
        detail = JSON.stringify(data.detail);
      }
    } catch (error) {
      // Ignore JSON parse failures on non-JSON responses.
    }
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }

  if (response.status === 204) {
    return null;
  }
  return response.json();
}

async function loadTree(path = state.root, container = null) {
  const entries = await api(
    `/api/tree?session_id=${encodeURIComponent(state.sessionId)}&path=${encodeURIComponent(path)}`
  );
  const list = renderTree(entries);
  if (container) {
    container.replaceChildren(list);
  } else {
    treeRoot.replaceChildren(list);
    treeRoot.classList.remove("empty-state");
  }
}

function setSelection(path, type, checked) {
  if (checked) {
    state.selected.set(path, { path, type });
  } else {
    state.selected.delete(path);
  }
  updateSelectionCount();
}

function renderTree(entries) {
  const list = document.createElement("ul");
  if (!entries.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "(empty directory)";
    list.appendChild(empty);
    return list;
  }

  for (const entry of entries) {
    const item = document.createElement("li");
    const row = document.createElement("div");
    row.className = "tree-row";

    let children = null;
    if (entry.type === "dir") {
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "tree-toggle";
      toggle.textContent = "+";
      toggle.title = "Expand";

      children = document.createElement("div");
      children.hidden = true;

      toggle.addEventListener("click", async () => {
        if (children.hidden) {
          if (!children.dataset.loaded) {
            toggle.disabled = true;
            try {
              await loadTree(entry.path, children);
              children.dataset.loaded = "true";
            } catch (error) {
              setStatus(`Tree load failed: ${error.message}`, true);
              children.textContent = `Failed: ${error.message}`;
              children.className = "muted";
            } finally {
              toggle.disabled = false;
            }
          }
          children.hidden = false;
          toggle.textContent = "−";
          toggle.title = "Collapse";
        } else {
          children.hidden = true;
          toggle.textContent = "+";
          toggle.title = "Expand";
        }
      });
      row.appendChild(toggle);
    } else {
      const spacer = document.createElement("span");
      row.appendChild(spacer);
    }

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selected.has(entry.path);
    checkbox.title = entry.path;
    checkbox.dataset.path = entry.path;
    checkbox.dataset.type = entry.type;
    checkbox.addEventListener("change", () => setSelection(entry.path, entry.type, checkbox.checked));
    row.appendChild(checkbox);

    const label = document.createElement("span");
    label.className = "tree-label";
    label.title = entry.path;
    const badge = document.createElement("span");
    badge.className = entry.type === "dir" ? "dir-badge" : "file-badge";
    badge.textContent = entry.type === "dir" ? "dir" : "file";
    label.append(entry.name, badge);
    row.appendChild(label);

    if (entry.type === "file" && entry.size !== null && entry.size !== undefined) {
      const size = document.createElement("span");
      size.className = "tree-size";
      size.textContent = formatSize(entry.size);
      row.appendChild(size);
    } else {
      const spacer = document.createElement("span");
      row.appendChild(spacer);
    }

    item.appendChild(row);
    if (children) {
      item.appendChild(children);
    }
    list.appendChild(item);
  }
  return list;
}

function persistAppState() {
  RottaState.save(localStorage, state);
}

async function cancelDownloadJob(jobId, button) {
  button.disabled = true;
  try {
    await api(`/api/download/${encodeURIComponent(jobId)}/cancel`, {
      method: "POST",
    });
    setStatus(`Cancelling download ${jobId.slice(0, 8)}…`);
    await pollDownloadQueue();
  } catch (error) {
    button.disabled = false;
    setStatus(`Cancel failed: ${error.message}`, true);
  }
}

function renderDownloadQueue(jobs) {
  if (!jobs.length) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.textContent = "No download jobs";
    downloadQueue.replaceChildren(empty);
    return;
  }

  const cards = jobs.map((job) => {
    const card = document.createElement("article");
    card.className = "download-job";
    card.dataset.jobId = job.id;

    const header = document.createElement("div");
    header.className = "download-job-header";
    const id = document.createElement("span");
    id.className = "mono";
    id.textContent = job.id.slice(0, 8);
    id.title = job.id;
    const summary = document.createElement("span");
    summary.textContent = `${job.status} ${job.done}/${job.total}`;
    const actions = document.createElement("span");
    actions.className = "download-job-actions";
    actions.appendChild(summary);
    if (["queued", "running"].includes(job.status)) {
      const cancelButton = document.createElement("button");
      cancelButton.type = "button";
      cancelButton.className = "cancel-job";
      cancelButton.textContent = "Cancel";
      cancelButton.addEventListener("click", () =>
        cancelDownloadJob(job.id, cancelButton)
      );
      actions.appendChild(cancelButton);
    } else if (job.status === "cancelling") {
      const cancelButton = document.createElement("button");
      cancelButton.type = "button";
      cancelButton.className = "cancel-job";
      cancelButton.textContent = "Cancelling…";
      cancelButton.disabled = true;
      actions.appendChild(cancelButton);
    }
    header.append(id, actions);

    const progress = document.createElement("progress");
    progress.max = Math.max(job.total, 1);
    progress.value = job.done;

    const current = document.createElement("div");
    current.className = "mono muted";
    current.textContent = job.current || "";

    const errors = document.createElement("ul");
    errors.className = "error-list";
    errors.replaceChildren(
      ...job.errors.map((message) => {
        const item = document.createElement("li");
        item.textContent = message;
        return item;
      })
    );
    card.append(header, progress, current, errors);
    return card;
  });
  downloadQueue.replaceChildren(...cards);
}

async function pollDownloadQueue() {
  try {
    const jobs = await api("/api/downloads");
    renderDownloadQueue(jobs);
    if (statusText.textContent.startsWith("Queue polling failed:")) {
      setStatus("Download queue polling restored.");
    }
  } catch (error) {
    setStatus(`Queue polling failed: ${error.message}`, true);
  }
}

async function restoreAppState() {
  const saved = RottaState.load(localStorage);
  if (!saved) {
    return;
  }

  state.sessionId = saved.sessionId;
  state.root = saved.root;
  state.host = saved.host;
  state.protocol = saved.protocol;

  const protocolRadio = connectForm.querySelector(
    `input[name="protocol"][value="${state.protocol}"]`
  );
  if (protocolRadio) {
    protocolRadio.checked = true;
  }
  syncProtocolVisibility();
  sessionSummary.textContent = `${state.protocol.toUpperCase()} · ${state.host} · ${state.root}`;
  sessionSummary.title = sessionSummary.textContent;
  browseHint.textContent = state.root;
  browseHint.title = state.root;
  treeRoot.textContent = "Loading…";
  treeRoot.classList.add("empty-state");

  try {
    await loadTree(state.root);
  } catch (error) {
    if (error.status === 404) {
      RottaState.clear(localStorage);
      state.sessionId = null;
      state.root = null;
      state.host = null;
      sessionSummary.textContent = "Not connected";
      browseHint.textContent = "Connect to load the remote tree";
      treeRoot.textContent = "Saved session expired. Connect again.";
    }
    setStatus(`Could not restore session: ${error.message}`, true);
    return;
  }

  setStatus("Previous session restored.");
}

connectForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  syncProtocolVisibility();

  const remotePathEl = document.getElementById("remote-path");
  const remotePath = (remotePathEl && remotePathEl.value ? remotePathEl.value : "").trim();
  if (!remotePath) {
    setStatus("Connect failed: remote_path is required (paste the UNC or SFTP path).", true);
    remotePathEl && remotePathEl.focus();
    return;
  }

  const payload = {
    protocol: connectForm.elements.protocol.value,
    remote_path: remotePath,
  };

  connectBtn.disabled = true;
  retrySftpBtn.classList.add("hidden");
  setStatus("Connecting…");
  try {
    const data = await api("/api/connect", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.sessionId = data.session_id;
    state.root = data.root;
    state.host = data.host;
    state.protocol = data.protocol;
    state.selected.clear();
    persistAppState();
    updateSelectionCount();
    sessionSummary.textContent = `${data.protocol.toUpperCase()} · ${data.host} · ${data.root}`;
    sessionSummary.title = sessionSummary.textContent;
    browseHint.textContent = data.root;
    browseHint.title = data.root;
    treeRoot.textContent = "Loading…";
    treeRoot.classList.add("empty-state");
    await loadTree(data.root);
    setStatus("Connected. Expand folders and check items to download.");
  } catch (error) {
    const msg = error.message || String(error);
    setStatus(`Connect failed: ${msg}`, true);
    if (
      payload.protocol === "smb" &&
      /SFTP|NO_LOGON_SERVERS|0xc000005e|3221225566|domain logon failed/i.test(msg)
    ) {
      retrySftpBtn.classList.remove("hidden");
    }
  } finally {
    connectBtn.disabled = false;
  }
});

retrySftpBtn.addEventListener("click", () => {
  const sftpRadio = connectForm.querySelector('input[name="protocol"][value="sftp"]');
  if (sftpRadio) {
    sftpRadio.checked = true;
    syncProtocolVisibility();
  }
  retrySftpBtn.classList.add("hidden");
  setStatus("Switched to SFTP. Click Connect again (same UNC path is OK).");
  connectForm.requestSubmit();
});

downloadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.sessionId) {
    setStatus("Connect before starting a download.", true);
    return;
  }
  const items = [...state.selected.values()];
  if (!items.length) {
    setStatus("Select at least one file or directory in Browse.", true);
    return;
  }

  const workers = Number.parseInt(downloadForm.elements.workers.value, 10);
  const payload = {
    session_id: state.sessionId,
    items,
    local_dir: downloadForm.elements.local_dir.value.trim(),
    workers: Number.isFinite(workers) ? workers : 8,
  };

  downloadBtn.disabled = true;
  try {
    const data = await api("/api/download", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setStatus(`Download started (${items.length} item(s)).`);
    await pollDownloadQueue();
  } catch (error) {
    setStatus(`Download failed to start: ${error.message}`, true);
  } finally {
    downloadBtn.disabled = false;
  }
});

function syncCheckboxUi() {
  for (const checkbox of treeRoot.querySelectorAll('input[type="checkbox"]')) {
    checkbox.checked = state.selected.has(checkbox.dataset.path);
  }
  updateSelectionCount();
}

selectAllBtn.addEventListener("click", () => {
  const boxes = treeRoot.querySelectorAll('input[type="checkbox"][data-path]');
  if (!boxes.length) {
    setStatus("Nothing to select yet. Connect and load the tree first.", true);
    return;
  }
  for (const checkbox of boxes) {
    checkbox.checked = true;
    setSelection(checkbox.dataset.path, checkbox.dataset.type, true);
  }
  setStatus(`Selected ${state.selected.size} loaded item(s). Expand folders to select more.`);
});

deselectAllBtn.addEventListener("click", () => {
  state.selected.clear();
  syncCheckboxUi();
  setStatus("Selection cleared.");
});

for (const radio of connectForm.querySelectorAll('input[name="protocol"]')) {
  radio.addEventListener("change", syncProtocolVisibility);
}

syncProtocolVisibility();
updateSelectionCount();
pollDownloadQueue();
setInterval(pollDownloadQueue, 500);
restoreAppState();
