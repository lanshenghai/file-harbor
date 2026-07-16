import time
from threading import Event

from app.backends import Entry, FakeBackend
from app.download import DownloadManager


def test_fake_walk_and_download(tmp_path):
    root = "/root"
    fb = FakeBackend(
        tree={
            root: [Entry("a", f"{root}/a", "dir"), Entry("f.txt", f"{root}/f.txt", "file", 3)],
            f"{root}/a": [Entry("b.bin", f"{root}/a/b.bin", "file", 2)],
        },
        files={f"{root}/f.txt": b"abc", f"{root}/a/b.bin": b"xy"},
    )
    files = fb.walk_files(root)
    assert len(files) == 2
    fb.download_file(f"{root}/f.txt", tmp_path / "f.txt")
    assert (tmp_path / "f.txt").read_bytes() == b"abc"


def test_download_job(tmp_path):
    root = "/root"
    fb = FakeBackend(
        tree={
            root: [Entry("a", f"{root}/a", "dir"), Entry("f.txt", f"{root}/f.txt", "file", 3)],
            f"{root}/a": [Entry("b.bin", f"{root}/a/b.bin", "file", 2)],
        },
        files={f"{root}/f.txt": b"abc", f"{root}/a/b.bin": b"xy"},
    )
    mgr = DownloadManager()
    jid = mgr.start(
        backend=fb,
        root=root,
        protocol="sftp",
        items=[{"path": f"{root}/a", "type": "dir"}, {"path": f"{root}/f.txt", "type": "file"}],
        local_dir=str(tmp_path),
        workers=2,
    )
    for _ in range(50):
        p = mgr.get(jid)
        if p.status in ("done", "done_with_errors", "failed"):
            break
        time.sleep(0.05)
    assert p.status == "done"
    assert p.done == 2
    assert (tmp_path / "f.txt").read_bytes() == b"abc"
    assert (tmp_path / "a" / "b.bin").read_bytes() == b"xy"


def test_download_job_keeps_current_when_files_overlap(tmp_path):
    root = "/root"
    fast_path = f"{root}/fast.txt"
    slow_path = f"{root}/slow.txt"

    class BlockingBackend(FakeBackend):
        def __init__(self, tree, files):
            super().__init__(tree=tree, files=files)
            self.slow_started = Event()
            self.slow_release = Event()

        def download_file(self, remote_path, local_path, cancel_event=None):
            if remote_path == fast_path:
                self.slow_started.wait()
            elif remote_path == slow_path:
                self.slow_started.set()
                self.slow_release.wait()
            super().download_file(remote_path, local_path, cancel_event)

    fb = BlockingBackend(
        tree={
            root: [
                Entry("fast.txt", fast_path, "file", 3),
                Entry("slow.txt", slow_path, "file", 3),
            ]
        },
        files={fast_path: b"abc", slow_path: b"xyz"},
    )
    mgr = DownloadManager()
    jid = mgr.start(
        backend=fb,
        root=root,
        protocol="sftp",
        items=[{"path": root, "type": "dir"}],
        local_dir=str(tmp_path),
        workers=2,
    )

    for _ in range(100):
        p = mgr.get(jid)
        if p.done == 1 and p.status == "running":
            break
        time.sleep(0.01)

    assert p.done == 1
    assert p.status == "running"
    assert p.current == slow_path

    fb.slow_release.set()
    for _ in range(100):
        p = mgr.get(jid)
        if p.status == "done":
            break
        time.sleep(0.01)

    assert p.status == "done"
    assert p.done == 2


def test_download_job_does_not_close_backend(tmp_path):
    root = "/root"

    class CloseTrackingBackend(FakeBackend):
        def __init__(self, tree, files):
            super().__init__(tree=tree, files=files)
            self.close_called = False

        def close(self) -> None:
            self.close_called = True
            raise AssertionError("close should not be called by DownloadManager")

    fb = CloseTrackingBackend(
        tree={root: [Entry("f.txt", f"{root}/f.txt", "file", 3)]},
        files={f"{root}/f.txt": b"abc"},
    )
    mgr = DownloadManager()
    jid = mgr.start(
        backend=fb,
        root=root,
        protocol="sftp",
        items=[{"path": f"{root}/f.txt", "type": "file"}],
        local_dir=str(tmp_path),
        workers=1,
    )

    for _ in range(50):
        p = mgr.get(jid)
        if p.status in ("done", "done_with_errors", "failed"):
            break
        time.sleep(0.05)

    assert p.status == "done"
    assert p.errors == []
    assert fb.close_called is False


def test_download_manager_lists_newest_jobs_as_isolated_snapshots(tmp_path):
    root = "/root"
    fb = FakeBackend(
        tree={root: [Entry("f.txt", f"{root}/f.txt", "file", 3)]},
        files={f"{root}/f.txt": b"abc"},
    )
    mgr = DownloadManager()
    first_id = mgr.start(
        backend=fb,
        root=root,
        protocol="sftp",
        items=[{"path": f"{root}/f.txt", "type": "file"}],
        local_dir=str(tmp_path),
        workers=1,
    )
    second_id = mgr.start(
        backend=fb,
        root=root,
        protocol="sftp",
        items=[{"path": f"{root}/f.txt", "type": "file"}],
        local_dir=str(tmp_path),
        workers=1,
    )

    jobs = mgr.list()

    assert [job.id for job in jobs] == [second_id, first_id]
    jobs[0].errors.append("changed outside manager")
    assert "changed outside manager" not in mgr.get(second_id).errors


def test_download_manager_cancels_running_job_without_starting_pending_files(tmp_path):
    root = "/root"
    first_path = f"{root}/first.bin"
    second_path = f"{root}/second.bin"

    class CancellableBackend(FakeBackend):
        def __init__(self, tree, files):
            super().__init__(tree=tree, files=files)
            self.started = Event()
            self.calls = []

        def download_file(self, remote_path, local_path, cancel_event=None):
            self.calls.append(remote_path)
            self.started.set()
            if cancel_event is not None:
                cancel_event.wait(timeout=2)
            else:
                time.sleep(0.1)

    backend = CancellableBackend(
        tree={
            root: [
                Entry("first.bin", first_path, "file", 1),
                Entry("second.bin", second_path, "file", 1),
            ]
        },
        files={first_path: b"a", second_path: b"b"},
    )
    manager = DownloadManager()
    job_id = manager.start(
        backend=backend,
        root=root,
        protocol="sftp",
        items=[{"path": root, "type": "dir"}],
        local_dir=str(tmp_path),
        workers=1,
    )
    assert backend.started.wait(timeout=1)

    cancelling = manager.cancel(job_id)

    assert cancelling.status == "cancelling"
    assert manager.cancel(job_id).status in {"cancelling", "cancelled"}
    for _ in range(100):
        progress = manager.get(job_id)
        if progress.status == "cancelled":
            break
        time.sleep(0.01)

    assert progress.status == "cancelled"
    assert progress.done == 0
    assert progress.errors == []
    assert backend.calls == [first_path]
