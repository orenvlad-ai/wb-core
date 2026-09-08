"""Bounded, spawn-based checkpoints for temporary process/crash fixtures only."""

from contextlib import contextmanager
import multiprocessing


def checkpoint(channel, name):
    """Child announces an exact boundary and waits for the parent's release."""
    channel.send(name)
    if not channel.poll(15) or channel.recv() != name:
        raise AssertionError(f"fixture checkpoint was not released: {name}")


class FixtureProcess:
    def __init__(self, process, channel):
        self.process = process
        self.channel = channel

    def wait(self, name):
        if not self.channel.poll(10):
            raise AssertionError(f"fixture did not reach {name}; exit={self.process.exitcode}")
        assert self.channel.recv() == name, f"unexpected fixture checkpoint; expected {name}"

    def release(self, name):
        self.channel.send(name)

    def finish(self):
        self.process.join(10)
        assert self.process.exitcode == 0, f"fixture failed or hung: {self.process.exitcode}"

    def crash(self):
        assert self.process.is_alive(), "fixture exited before crash point"
        self.process.terminate()
        self.process.join(5)
        assert self.process.exitcode is not None and self.process.exitcode < 0


@contextmanager
def fixture_process(target, *args):
    """Target(channel, *args) must use disposable inputs, never runtime data."""
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=target, args=(child, *args))
    process.start()
    child.close()
    try:
        yield FixtureProcess(process, parent)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        if process.is_alive():
            process.kill()
        process.join(5)
        parent.close()
        assert not process.is_alive(), "fixture child survived cleanup"
        process.close()
