import queue
import subprocess
import sys
import threading
import time


def test_full_watcher_subscriber_does_not_block_stop():
    from api.gateway_watcher import GatewayWatcher

    watcher = GatewayWatcher()
    subscriber = watcher.subscribe()
    for number in range(subscriber.maxsize):
        subscriber.put_nowait(number)
    stopped = threading.Event()

    def stop():
        watcher.stop()
        stopped.set()

    thread = threading.Thread(target=stop, daemon=True)
    thread.start()
    try:
        assert stopped.wait(1), 'full subscriber blocked watcher shutdown'
        pending = []
        while not subscriber.empty():
            pending.append(subscriber.get_nowait())
        assert pending[-1] is None
        assert watcher.subscribe().get_nowait() is None
    finally:
        try:
            subscriber.get_nowait()
        except queue.Empty:
            pass
        thread.join(2)


def test_usage_probe_cleanup_unblocks_pipe_reader():
    from api.providers import _AccountUsageProbeWorker

    child = subprocess.Popen(
        [sys.executable, '-u', '-c',
         'import time; print("ready", flush=True); time.sleep(60)'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    reader = threading.Thread(target=child.stdout.readline, daemon=True)
    closer = threading.Thread(
        target=_AccountUsageProbeWorker._close_process, args=(child,), daemon=True,
    )
    try:
        assert child.stdout.readline().strip() == 'ready'
        reader.start()
        time.sleep(.05)
        closer.start()
        closer.join(3)
        assert not closer.is_alive(), 'pipe close waited before terminating its writer'
        reader.join(1)
        assert not reader.is_alive()
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)
        if reader.ident is not None:
            reader.join(3)
        if closer.ident is not None:
            closer.join(3)
