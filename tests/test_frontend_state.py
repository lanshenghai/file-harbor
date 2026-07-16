from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_frontend_polls_global_download_queue_after_refresh():
    source = (ROOT / "static" / "app.js").read_text()

    assert 'api("/api/downloads")' in source
    assert "function pollDownloadQueue()" in source
    assert "setInterval(pollDownloadQueue, 500)" in source
    assert 'setStatus("Download queue polling restored.")' in source
    assert "saved.jobId" not in source


def test_download_state_storage_round_trip():
    script = r"""
const assert = require("node:assert/strict");
const RottaState = require("./static/download-state.js");

class MemoryStorage {
  constructor() {
    this.values = new Map();
  }
  getItem(key) {
    return this.values.has(key) ? this.values.get(key) : null;
  }
  setItem(key, value) {
    this.values.set(key, value);
  }
  removeItem(key) {
    this.values.delete(key);
  }
}

const storage = new MemoryStorage();
const expected = {
  sessionId: "session-1",
  root: "/root",
  host: "example.test",
  protocol: "sftp",
};

RottaState.save(storage, expected);
assert.deepEqual(RottaState.load(storage), expected);

storage.setItem(RottaState.STORAGE_KEY, "{broken");
assert.equal(RottaState.load(storage), null);
assert.equal(storage.getItem(RottaState.STORAGE_KEY), null);
"""
    subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_frontend_renders_cancel_action_for_active_jobs():
    source = (ROOT / "static" / "app.js").read_text()

    assert "function cancelDownloadJob(jobId, button)" in source
    assert "`/api/download/${encodeURIComponent(jobId)}/cancel`" in source
    assert '["queued", "running"].includes(job.status)' in source
    assert 'cancelButton.textContent = "Cancel"' in source
