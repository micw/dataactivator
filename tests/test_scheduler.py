from dataactivator.core.scheduler import PollTarget, Scheduler


def test_runs_each_target_and_stops_at_max_cycles() -> None:
    calls: list[str] = []
    targets = [
        PollTarget("a", 0.0, lambda: calls.append("a")),
        PollTarget("b", 0.0, lambda: calls.append("b")),
    ]
    Scheduler(targets).run_forever(install_signals=False, max_cycles=4)
    assert calls.count("a") == 2
    assert calls.count("b") == 2


def test_one_target_exception_does_not_kill_loop() -> None:
    calls: list[str] = []

    def boom() -> None:
        calls.append("boom")
        raise RuntimeError("nope")

    targets = [
        PollTarget("bad", 0.0, boom),
        PollTarget("good", 0.0, lambda: calls.append("good")),
    ]
    Scheduler(targets).run_forever(install_signals=False, max_cycles=4)
    assert calls.count("good") == 2  # good kept running despite bad raising


def test_request_stop_halts() -> None:
    calls: list[str] = []
    sched = Scheduler([PollTarget("a", 0.0, lambda: calls.append("a"))])

    def run_then_stop() -> None:
        calls.append("a")
        sched.request_stop()

    sched._targets[0] = PollTarget("a", 0.0, run_then_stop)
    sched.run_forever(install_signals=False)
    assert calls == ["a"]
