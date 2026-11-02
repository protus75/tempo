import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from tempo.git import Git, GitError, RebaseRefused
from tempo.seams import ExecutableNotFound, GroupExec, ProcessExec, SubprocessExec

ENV = {"PATH": "/usr/bin"}
REPO = Path("/repo")


class FakeExec:
    """Scripted process-exec seam: pops one (rc, out, err) per call, records argv."""

    def __init__(self, *results: tuple[int, str, str]) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], Path, dict[str, str], float | None]] = []

    async def run(self, argv, *, cwd, env, timeout, stdin_path=None):
        self.calls.append((list(argv), cwd, dict(env), timeout))
        return self.results.pop(0) if self.results else (0, "", "")

    @property
    def argvs(self) -> list[list[str]]:
        return [c[0] for c in self.calls]


def run(coro):
    return asyncio.run(coro)


def git(*results: tuple[int, str, str]) -> tuple[Git, FakeExec]:
    exec_ = FakeExec(*results)
    return Git(exec_, env=ENV, timeout=30.0), exec_


def test_fake_satisfies_seam_protocol():
    assert isinstance(FakeExec(), ProcessExec)
    assert isinstance(SubprocessExec(), ProcessExec)


def test_env_and_timeout_are_required_keywords():
    with pytest.raises(TypeError):
        Git(FakeExec())  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Git(FakeExec(), ENV, 30.0)  # type: ignore[misc]


def test_every_call_is_dir_pinned_and_passes_env_and_timeout():
    g, exec_ = git()
    run(g.status_porcelain(REPO))
    argv, cwd, env, timeout = exec_.calls[0]
    assert argv == ["git", "-C", "/repo", "status", "--porcelain"]
    assert cwd == REPO
    assert env == ENV
    assert timeout == 30.0


@pytest.mark.parametrize(
    "call, argv",
    [
        (lambda g: g.init(REPO, branch="main"), ["init", "-b", "main"]),
        (lambda g: g.ls_files(REPO), ["ls-files"]),
        (lambda g: g.rev_parse(REPO, "HEAD"), ["rev-parse", "--verify", "HEAD"]),
        (lambda g: g.merge_base(REPO, "main", "t-1"), ["merge-base", "main", "t-1"]),
        (lambda g: g.diff(REPO, "main", "t-1"), ["diff", "main...t-1"]),
        (lambda g: g.diff_stat(REPO, "main", "t-1"), ["diff", "--stat", "main...t-1"]),
        (lambda g: g.add(REPO, ["a.py", "b c.py"]), ["add", "--", "a.py", "b c.py"]),
        (lambda g: g.commit(REPO, "msg\n\ntempo-ticket: t"), ["commit", "-m", "msg\n\ntempo-ticket: t"]),
        (lambda g: g.commit(REPO, "msg", only=["a.py"]), ["commit", "-m", "msg", "--only", "--", "a.py"]),
        (lambda g: g.branch(REPO, "t-1", "main"), ["branch", "t-1", "main"]),
        (lambda g: g.branch_delete(REPO, "t-1"), ["branch", "-D", "t-1"]),
        (
            lambda g: g.worktree_add(REPO, Path("/wt/t-1"), "t-1", "main"),
            ["worktree", "add", "-b", "t-1", "/wt/t-1", "main"],
        ),
        (lambda g: g.worktree_add_detached(REPO, Path("/wt/base"), "main"),
         ["worktree", "add", "--detach", "/wt/base", "main"]),
        (lambda g: g.worktree_prune(REPO), ["worktree", "prune"]),
        (lambda g: g.rebase(REPO, "main"), ["rebase", "main"]),
        (
            lambda g: g.restore(REPO, ["tickets"], source="main"),
            ["restore", "--source", "main", "--staged", "--worktree", "--", "tickets"],
        ),
        (lambda g: g.merge_squash(REPO, "t-1"), ["merge", "--squash", "t-1"]),
        (lambda g: g.push(REPO, "origin", "main"), ["push", "origin", "main"]),
        (lambda g: g.describe(REPO), ["describe", "--tags", "--always", "--dirty"]),
    ],
)
def test_op_argv(call, argv):
    g, exec_ = git()
    run(call(g))
    assert exec_.argvs == [["git", "-C", "/repo", *argv]]
    assert exec_.calls[0][1:] == (REPO, ENV, 30.0)
    if argv[0] == "push":
        g, exec_ = git((1, "output", "refused"))
        with pytest.raises(GitError) as error:
            run(call(g))
        assert error.value.argv == ["git", "-C", "/repo", *argv]
        assert (error.value.rc, error.value.out, error.value.err) == (1, "output", "refused")


def test_rev_parse_and_describe_strip_output():
    g, _ = git((0, "abc123\n", ""), (0, "v1.0-3-gabc-dirty\n", ""))
    assert run(g.rev_parse(REPO, "HEAD")) == "abc123"
    assert run(g.describe(REPO)) == "v1.0-3-gabc-dirty"


def test_ls_files_lists_committed_files_not_untracked_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    g = Git(SubprocessExec(), env=env, timeout=30.0)

    async def scenario():
        await g.init(repo, branch="main")
        (repo / "tracked.txt").write_text("tracked\n")
        await g.add(repo, ["tracked.txt"])
        await g.commit(repo, "base")
        (repo / "untracked.txt").write_text("untracked\n")
        assert "tracked.txt" in await g.ls_files(repo)
        assert "untracked.txt" not in await g.ls_files(repo)

    run(scenario())


def test_diff_names_parses_lines():
    g, exec_ = git((0, "a.py\nsub/b.py\n", ""))
    assert run(g.diff_names(REPO, "main", "t-1")) == ["a.py", "sub/b.py"]
    assert exec_.argvs[0][3:] == ["diff", "--name-only", "main...t-1"]


def test_diff_names_empty():
    g, _ = git((0, "", ""))
    assert run(g.diff_names(REPO, "main", "t-1")) == []


@pytest.mark.parametrize("base, stem", [("-bad", "t-1"), ("main", "--bad")])
def test_diff_stat_refuses_option_shaped_refs(base, stem):
    g, exec_ = git()
    with pytest.raises(ValueError):
        run(g.diff_stat(REPO, base, stem))
    assert exec_.calls == []


def test_nonzero_exit_raises_git_error_with_argv_and_stderr():
    g, _ = git((128, "", "fatal: bad revision\n"))
    with pytest.raises(GitError) as exc:
        run(g.rev_parse(REPO, "nope"))
    assert exc.value.rc == 128
    assert exc.value.argv == ["git", "-C", "/repo", "rev-parse", "--verify", "nope"]
    assert "bad revision" in str(exc.value)


@pytest.mark.parametrize("ref", ["-x", "--exec=evil", ""])
def test_option_shaped_or_empty_refs_are_refused_before_exec(ref):
    g, exec_ = git()
    with pytest.raises(ValueError):
        run(g.branch(REPO, ref, "main"))
    with pytest.raises(ValueError):
        run(g.rebase(REPO, ref))
    with pytest.raises(ValueError):
        run(g.merge_base(REPO, "main", ref))
    with pytest.raises(ValueError):
        run(g.worktree_add_detached(REPO, Path("/wt/base"), ref))
    with pytest.raises(ValueError):
        run(g.push(REPO, ref, "main"))
    with pytest.raises(ValueError):
        run(g.push(REPO, "origin", ref))
    assert exec_.calls == []


def test_worktree_remove_is_remove_then_prune():
    g, exec_ = git()
    run(g.worktree_remove(REPO, Path("/wt/t-1")))
    assert exec_.argvs == [
        ["git", "-C", "/repo", "worktree", "remove", "--force", "/wt/t-1"],
        ["git", "-C", "/repo", "worktree", "prune"],
    ]


def test_refused_rebase_is_aborted_before_refusal_returns():
    g, exec_ = git((1, "", "CONFLICT (content)\n"), (0, "", ""))
    with pytest.raises(RebaseRefused) as exc:
        run(g.rebase(REPO, "main"))
    assert exec_.argvs[-1] == ["git", "-C", "/repo", "rebase", "--abort"]
    assert "CONFLICT" in str(exc.value)


def test_rebase_success_does_not_abort():
    g, exec_ = git((0, "", ""))
    run(g.rebase(REPO, "main"))
    assert len(exec_.calls) == 1


# --- real process-exec seam ---------------------------------------------------


def test_subprocess_exec_unresolvable_binary_fails_closed():
    with pytest.raises(ExecutableNotFound, match="no-such-binary-tempo"):
        run(SubprocessExec().run(["no-such-binary-tempo"], cwd=Path("."), env=dict(os.environ), timeout=5))


def test_subprocess_exec_feeds_stdin_path_and_captures(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("hello")
    rc, out, err = run(
        SubprocessExec().run(
            [sys.executable, "-c", "import sys; d=sys.stdin.read(); print(d.upper()); sys.exit(3)"],
            cwd=tmp_path,
            env=dict(os.environ),
            timeout=10,
            stdin_path=prompt,
        )
    )
    assert (rc, out, err) == (3, "HELLO\n", "")


def _spawns_grandchild(marker: Path) -> list[str]:
    # The grandchild outlives a direct-child kill; only a group kill stops it writing.
    grandchild = "import sys, time; time.sleep(1.0); open(sys.argv[1], 'w').close()"
    script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
        "time.sleep(30)\n"
    )
    return [sys.executable, "-c", script, grandchild, str(marker)]


def test_subprocess_exec_timeout_kills_the_process_group(tmp_path):
    marker = tmp_path / "grandchild-alive"
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        run(SubprocessExec().run(_spawns_grandchild(marker), cwd=tmp_path, env=dict(os.environ), timeout=0.3))
    assert time.monotonic() - started < 5
    time.sleep(1.5)
    assert not marker.exists()


def test_subprocess_exec_outer_cancellation_kills_the_process_group(tmp_path):
    marker = tmp_path / "grandchild-alive"
    call = SubprocessExec().run(_spawns_grandchild(marker), cwd=tmp_path, env=dict(os.environ), timeout=None)
    with pytest.raises(TimeoutError):
        run(asyncio.wait_for(call, 0.3))
    time.sleep(1.5)
    assert not marker.exists()


def test_subprocess_exec_publishes_pgid_for_a_synchronous_group_kill(tmp_path):
    marker = tmp_path / "grandchild-alive"
    seam = SubprocessExec()
    assert isinstance(seam, GroupExec)

    async def scenario():
        spawned = asyncio.Event()
        pgids = []
        task = asyncio.ensure_future(
            seam.run(
                _spawns_grandchild(marker), cwd=tmp_path, env=dict(os.environ), timeout=30,
                on_spawn=lambda pgid: (pgids.append(pgid), spawned.set()),
            )
        )
        await spawned.wait()
        seam.kill_group(pgids[0])
        return await asyncio.wait_for(task, 5)

    rc, _, _ = run(scenario())
    assert rc != 0
    time.sleep(1.5)
    assert not marker.exists()


# --- real repository ----------------------------------------------------------


def test_ticket_lifecycle_against_real_repo(tmp_path):
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    g = Git(SubprocessExec(), env=env, timeout=30.0)
    repo = tmp_path / "repo"
    repo.mkdir()
    wt = tmp_path / "wt" / "t-1"

    async def scenario():
        await g.init(repo, branch="main")
        (repo / "a.txt").write_text("one\n")
        (repo / "tickets").mkdir()
        (repo / "tickets" / "t-1.md").write_text("ticket\n")
        await g.add(repo, ["a.txt", "tickets"])
        await g.commit(repo, "base")
        base = await g.rev_parse(repo, "HEAD")
        assert await g.describe(repo) == base[:7]

        await g.worktree_add(repo, wt, "t-1", "main")
        assert await g.merge_base(repo, "main", "t-1") == base
        detached = tmp_path / "wt" / "base"
        await g.worktree_add_detached(repo, detached, base)
        assert await g.rev_parse(detached, "HEAD") == base
        await g.worktree_remove(repo, detached)
        (wt / "b.txt").write_text("two\n")
        (wt / "tickets" / "t-1.md").unlink()
        await g.add(wt, ["b.txt"])
        await g.commit(wt, "work")
        assert await g.diff_names(repo, "main", "t-1") == ["b.txt"]
        assert "+two" in await g.diff(repo, "main", "t-1")

        # main moves; a conflicting branch is refused and left un-rebased
        (repo / "a.txt").write_text("main\n")
        await g.add(repo, ["a.txt"])
        await g.commit(repo, "main moves")
        await g.branch(repo, "t-2", "HEAD~1")
        wt2 = tmp_path / "wt" / "t-2"
        await g.worktree_add(repo, wt2, "t-3", "t-2")
        (wt2 / "a.txt").write_text("conflict\n")
        await g.add(wt2, ["a.txt"])
        await g.commit(wt2, "clash")
        head = await g.rev_parse(wt2, "HEAD")
        with pytest.raises(RebaseRefused):
            await g.rebase(wt2, "main")
        assert await g.rev_parse(wt2, "HEAD") == head
        assert await g.status_porcelain(wt2) == ""
        await g.worktree_remove(repo, wt2)
        assert not wt2.exists()

        # restore the ticket plane, rebase, squash-merge, clean up
        assert " D tickets/t-1.md" in await g.status_porcelain(wt)
        await g.restore(wt, ["tickets"], source="main")
        assert await g.status_porcelain(wt) == ""
        await g.rebase(wt, "main")
        await g.merge_squash(repo, "t-1")
        await g.commit(repo, "tempo(t-1): work\n\ntempo-ticket: t-1")
        assert (repo / "b.txt").read_text() == "two\n"
        await g.worktree_remove(repo, wt)
        await g.branch_delete(repo, "t-1")
        assert not wt.exists()
        assert await g.status_porcelain(repo) == ""
        assert not (await g.describe(repo)).endswith("-dirty")
        (repo / "a.txt").write_text("dirty\n")
        assert (await g.describe(repo)).endswith("-dirty")

    run(scenario())


def test_rebase_stop_at_conflict_preserves_conflicted_state(tmp_path):
    repo, wt = tmp_path / "repo", tmp_path / "wt"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    g = Git(SubprocessExec(), env=env, timeout=30)

    async def scenario():
        await g.init(repo, branch="main")
        (repo / "file").write_text("base\n")
        await g.add(repo, ["file"])
        await g.commit(repo, "base")
        await g.worktree_add(repo, wt, "ticket", "main")
        (wt / "file").write_text("branch\n")
        await g.commit(wt, "branch", only=["file"])
        head = await g.rev_parse(wt, "HEAD")
        (repo / "file").write_text("main\n")
        await g.commit(repo, "main", only=["file"])
        with pytest.raises(GitError):
            await g.rebase_stop_at_conflict(wt, "main")
        assert await g.conflicted_paths(wt) == ["file"]
        assert "UU file" in await g.status_porcelain(wt)
        await g._call(wt, "rebase", "--abort")
        assert await g.rev_parse(wt, "HEAD") == head
        # A non-conflict refusal still aborts.
        (wt / "file").write_text("dirty\n")
        with pytest.raises(RebaseRefused):
            await g.rebase_stop_at_conflict(wt, "main")
        assert await g.conflicted_paths(wt) == []
    run(scenario())


def test_conflicted_paths_argv_and_output():
    g, exec_ = git((0, "z\0a b\0z\0", ""))
    assert run(g.conflicted_paths(REPO)) == ["a b", "z"]
    assert exec_.argvs == [["git", "-C", "/repo", "diff", "--name-only", "--diff-filter=U", "-z"]]


def test_rebase_continue_is_noninteractive():
    exec_ = FakeExec()
    g = Git(exec_, env={**ENV, "GIT_EDITOR": "must-not-run"}, timeout=30.0)
    run(g.rebase_continue(REPO))
    assert exec_.argvs == [["git", "-C", "/repo", "-c", "core.editor=true", "rebase", "--continue"]]
    assert exec_.calls[0][1:] == (REPO, {**ENV, "GIT_EDITOR": "true"}, 30.0)
    g, exec_ = git((1, "", "conflict"), (0, "file\0", ""))
    with pytest.raises(GitError):
        run(g.rebase_continue(REPO))
    assert not any(a[-1] == "--abort" for a in exec_.argvs)
    g, exec_ = git((1, "", "refusal"), (0, "", ""))
    with pytest.raises(RebaseRefused):
        run(g.rebase_continue(REPO))
    assert exec_.argvs[-1][-2:] == ["rebase", "--abort"]


def test_escape_first_parent_trailers(tmp_path):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    g = Git(SubprocessExec(), env=env, timeout=30)
    async def scenario():
        await g.init(tmp_path, branch="main")
        (tmp_path / "base").write_text("base")
        await g.add(tmp_path, ["base"])
        await g.commit(tmp_path, "base")
        base = await g.rev_parse(tmp_path, "HEAD")
        expected = []
        for name in ("first", "second"):
            path = f"{name} space.py"
            (tmp_path / path).write_text(name)
            await g.add(tmp_path, [path])
            await g.commit(tmp_path, f"code\n\ntempo-ticket: {name}\ntempo-reviewed-sha: {'a' * 40}")
            expected.append({"ticket": name, "commit": await g.rev_parse(tmp_path, "HEAD"),
                             "reviewed_sha": "a" * 40, "paths": [path]})
        assert await g.first_parent_squashes(tmp_path, expected[0]["commit"]) == expected[:1]
        assert await g.first_parent_squashes(tmp_path, f'{base}..{expected[1]["commit"]}') == expected
        assert await g.first_parent_squashes(tmp_path, f'{expected[0]["commit"]}..{expected[1]["commit"]}') == expected[1:]
    run(scenario())
    g, seam = git((0, "a" * 40 + "\n", ""),
                 (0, f"code\n\ntempo-ticket: one\ntempo-reviewed-sha: {'b' * 40}\n", ""),
                 (0, "a b\0", ""))
    assert run(g.first_parent_squashes(REPO, "a" * 40))[0]["paths"] == ["a b"]
    assert all(call[1:] == (REPO, ENV, 30.0) for call in seam.calls)
    assert seam.argvs[0][3:] == ["rev-list", "--first-parent", "--reverse", "HEAD"]


def test_escape_refusals(tmp_path):
    a, b = "a" * 40, "b" * 40
    for value in ("", "--all", "HEAD", a.upper(), a[:39], f"{a}...{b}", f"{a}..HEAD"):
        g, seam = git()
        assert run(g.first_parent_squashes(REPO, value)) == []
        assert seam.calls == []
    for value in ("c" * 40, f"{b}..{a}", f"{a}..{a}", f"{a}..{'c' * 40}"):
        g, seam = git((0, a + "\n" + b + "\n", ""))
        assert run(g.first_parent_squashes(REPO, value)) == []
        assert len(seam.calls) == 1
    valid = f"tempo-ticket: one\ntempo-reviewed-sha: {b}"
    for trailers in ("", "tempo-ticket: one", valid + "\ntempo-ticket: one",
                     valid + f"\ntempo-reviewed-sha: {b}", valid.replace(b, "short"),
                     valid.replace("one", "../bad"), valid.replace("one", ""),
                     valid.replace(b, b.upper()), valid + " ",
                     valid + "\nTempo-Ticket: one", valid.replace("tempo-ticket", "Tempo-Ticket")):
        g, seam = git((0, a + "\n", ""), (0, "process\n\n" + trailers, ""))
        assert run(g.first_parent_squashes(REPO, a)) == []
        assert len(seam.calls) == 2
    g, _ = git((128, "", "repository unreadable"))
    with pytest.raises(GitError):
        run(g.first_parent_squashes(REPO, a))
    # A known commit on a side branch is not a first-parent endpoint.
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    g = Git(SubprocessExec(), env=env, timeout=30)
    async def scenario():
        await g.init(tmp_path, branch="main")
        (tmp_path / "base").write_text("base")
        await g.add(tmp_path, ["base"])
        await g.commit(tmp_path, "base")
        base = await g.rev_parse(tmp_path, "HEAD")
        await g.worktree_add(tmp_path, tmp_path / "side", "side", "main")
        (tmp_path / "side/other").write_text("other")
        await g.add(tmp_path / "side", ["other"])
        await g.commit(tmp_path / "side", "side")
        side = await g.rev_parse(tmp_path / "side", "HEAD")
        assert await g.first_parent_squashes(tmp_path, side) == []
        assert await g.first_parent_squashes(tmp_path, f"{base}..{side}") == []
        await g.worktree_remove(tmp_path, tmp_path / "side")
    run(scenario())
