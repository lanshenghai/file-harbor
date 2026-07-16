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


def test_frontend_loads_form_defaults_from_config_api():
    source = (ROOT / "static" / "app.js").read_text()

    assert 'api("/api/config")' in source
    assert "connectForm.elements.remote_path.value = defaults.remote_path" in source
    assert "downloadForm.elements.local_dir.value = defaults.local_dir" in source
    assert "downloadForm.elements.workers.value = String(defaults.workers)" in source


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


def test_frontend_renders_retry_action_for_failed_jobs():
    source = (ROOT / "static" / "app.js").read_text()

    assert "function retryDownloadJob(jobId, button)" in source
    assert "`/api/download/${encodeURIComponent(jobId)}/retry`" in source
    assert "`/api/download/${encodeURIComponent(jobId)}`" in source
    assert 'method: "DELETE"' in source
    assert "[\"done_with_errors\", \"failed\"].includes(job.status)" in source
    assert 'retryButton.textContent = "Retry"' in source


def test_frontend_renders_remove_action_for_finished_jobs():
    source = (ROOT / "static" / "app.js").read_text()

    assert "function removeDownloadJob(jobId, button)" in source
    assert "`/api/download/${encodeURIComponent(jobId)}`" in source
    assert "[\"done\", \"done_with_errors\", \"failed\", \"cancelled\"].includes(job.status)" in source
    assert 'removeButton.textContent = "Remove"' in source


def test_frontend_sets_hover_title_for_download_file_list():
    source = (ROOT / "static" / "app.js").read_text()

    assert "const hoverPaths = Array.isArray(job.paths)" in source
    assert "card.title = hoverPaths.join(\"\\n\");" in source
    assert "current.title = job.current || \"\";" in source


def test_frontend_shows_job_protocol_and_workers():
    source = (ROOT / "static" / "app.js").read_text()

    assert "const summaryParts = [`${job.status} ${job.done}/${job.total}`];" in source
    assert "if (typeof job.protocol === \"string\" && job.protocol)" in source
    assert "if (Number.isInteger(job.workers))" in source
    assert "summaryParts.push(`x${job.workers}`);" in source
    assert "function formatSpeed(speedBps)" in source
    assert "if (Number.isFinite(job.speed_bps) && job.speed_bps > 0)" in source
