"""M-SHELL 验收：探测、argv、退出码、中文/引号、超时杀树、长输出落盘、错误归一化。

用法：python scripts/check_shell.py
退出码 0 才算交付。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import winjob  # noqa: E402
from tools.errors import (  # noqa: E402
    classify_error,
    clean_text,
    fingerprint,
    normalize_command_for_fingerprint,
)
from tools.shell import (  # noqa: E402
    build_shell_tools,
    needs_artifact,
    reset_retry_state,
    stub_text,
)
from tools.shell_backend import (  # noqa: E402
    EXIT_TAIL,
    UTF8_INPUT,
    UTF8_PREFIX,
    ShellInfo,
    build_command,
    decode_bytes,
    detect_shell,
    find_shell,
    minimal_env,
)


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    reset_retry_state()
    tmp = Path(tempfile.mkdtemp(prefix="asst_shell_check_"))
    outputs = tmp / "tool_outputs"
    workdir = tmp / "work"
    workdir.mkdir()
    python = sys.executable

    worker_tool = build_shell_tools(
        profile="worker",
        workspace_root=tmp,
        tool_outputs_dir=outputs,
        default_cwd=workdir,
    )[0]
    router_tool = build_shell_tools(
        profile="router",
        workspace_root=tmp,
        tool_outputs_dir=outputs,
        default_cwd=workdir,
    )[0]

    def run(command: str, **kwargs):
        return worker_tool.invoke({"command": command, **kwargs})

    try:
        # ---------------------------------------------------------------- 探测与 argv
        info = detect_shell()
        check("detect_shell.pwsh", info.name == "pwsh" and Path(info.exe).is_file(), repr(info))
        check("detect_shell.version", bool(info.version) and info.version[0].isdigit(), info.version)
        check("detect_shell.cached", detect_shell() is info)

        argv = build_command(info, "Write-Output 'x'")
        script_arg = argv[4] if len(argv) == 5 else ""
        check("build_command.argv_len", len(argv) == 5, str(len(argv)))
        check("build_command.single_argument", argv.count(script_arg) == 1 and "\n" not in argv[0])
        check("build_command.prefix", script_arg.startswith(UTF8_PREFIX + UTF8_INPUT), script_arg[:80])
        check("build_command.exit_tail", script_arg.endswith(EXIT_TAIL), script_arg[-40:])
        check("build_command.script_verbatim", "Write-Output 'x'" in script_arg)

        cmd_info = find_shell("cmd")
        if cmd_info is not None:
            cmd_argv = build_command(cmd_info, "echo hi")
            check("build_command.cmd", cmd_argv[1:3] == ["/d", "/c"] and "chcp 65001" in cmd_argv[3])
        else:
            print("SKIP(cmd 不存在)")

        wsl_argv = build_command(ShellInfo("wsl", "wsl.exe", ""), "echo hi")
        check("build_command.wsl", wsl_argv == ["wsl.exe", "-e", "bash", "-lc", "echo hi"])

        env = minimal_env()
        check("minimal_env.systemroot", bool(env.get("SYSTEMROOT")))
        check("minimal_env.utf8", env.get("PYTHONUTF8") == "1" and env.get("PYTHONIOENCODING") == "utf-8")
        os.environ["FEISHU_APP_SECRET"] = "must-not-leak"
        os.environ["LLM_API_KEY"] = "must-not-leak"
        env2 = minimal_env()
        leaked = [k for k in env2 if k.upper() in {"FEISHU_APP_SECRET", "LLM_API_KEY"}]
        check("minimal_env.no_secrets", not leaked, str(leaked))
        for key in ("FEISHU_APP_SECRET", "LLM_API_KEY"):
            os.environ.pop(key, None)

        # ---------------------------------------------------------------- 基本执行
        r = run("Write-Output 'hello-shell'")
        check("echo.ok", r["ok"] is True and r["exit_code"] == 0, str(r))
        check("echo.stdout", "hello-shell" in r["stdout"], r["stdout"][:120])
        check("success.body_keys", {"ok", "exit_code", "stdout", "stderr", "total_lines",
                                    "total_bytes", "artifact", "duration_ms", "shell",
                                    "truncated"} <= set(r))
        check("success.no_error_class", "error_class" not in r)
        check("success.not_truncated", r["truncated"] is False and r["artifact"] is None)

        r = run("Write-Output '你好，世界'")
        check("chinese.stdout", r["ok"] and "你好，世界" in r["stdout"], r["stdout"][:120])

        r = run("Write-Output 'say \"hi\"'")
        check("quotes.single_layer", r["ok"] and 'say "hi"' in r["stdout"], r["stdout"][:120])

        r = run(f"& '{python}' -c 'print(\"native-quote-ok\")'")
        check("quotes.native_args", r["ok"] and "native-quote-ok" in r["stdout"], r["stdout"][:200])

        r = run(f"& '{python}' -c 'print(\"中文原生输出\")'")
        check("chinese.native", r["ok"] and "中文原生输出" in r["stdout"], r["stdout"][:200])

        r = run(f"& '{python}' -c 'import sys; sys.exit(0)'")
        check("python.exit0", r["ok"] is True and r["exit_code"] == 0, str(r.get("exit_code")))

        # ---------------------------------------------------------------- 退出码透传
        r = run("exit 3")
        check("exit_code.direct", r["exit_code"] == 3 and r["ok"] is False, str(r.get("exit_code")))
        check("exit_code.class", r.get("error_class") == "exit_nonzero", str(r.get("error_class")))

        r = run("& $env:COMSPEC /c exit 7")
        check("exit_code.last_exit_code", r["exit_code"] == 7, str(r.get("exit_code")))

        # ---------------------------------------------------------------- cwd
        sub = workdir / "sub"
        sub.mkdir()
        r = run("(Get-Location).Path", cwd=str(sub))
        check("cwd.absolute", r["ok"] and str(sub).lower() in r["stdout"].lower().replace("/", "\\"),
              r["stdout"][:200])
        r = run("(Get-Location).Path", cwd="sub")
        check("cwd.relative", r["ok"] and str(sub).lower() in r["stdout"].lower().replace("/", "\\"),
              r["stdout"][:200])
        r = run("Write-Output 'should-not-run'", cwd=str(tmp / "missing-dir"))
        check("cwd.missing", r["ok"] is False and r.get("error_class") == "not_found_path",
              str(r.get("error_class")))

        # ---------------------------------------------------------------- 错误分类（真机）
        r = run("__asst_missing_command_xyz__")
        check("error.command_not_found", r.get("error_class") == "command_not_found",
              str(r.get("error_class")) + " / " + str(r.get("message"))[:120])
        check("error.fingerprint_shape",
              isinstance(r.get("fingerprint"), str) and len(r["fingerprint"]) == 12,
              str(r.get("fingerprint")))

        r = run(r"Get-Content 'C:\__asst_missing_dir__\nope.txt'")
        check("error.not_found_path", r.get("error_class") == "not_found_path",
              str(r.get("error_class")) + " / " + str(r.get("message"))[:120])

        r = run("& $env:COMSPEC /c exit 9")
        check("error.exit_nonzero", r.get("error_class") == "exit_nonzero", str(r.get("error_class")))
        check("error.failure_fields",
              {"error_class", "fingerprint", "hint", "retryable", "attempt", "same_fp_count",
               "message"} <= set(r), str(sorted(r)))

        ps51 = find_shell("powershell")
        if ps51 is not None:
            r = run("Write-Output 'ps51-ok'", shell="powershell")
            check("powershell.explicit", r["ok"] and "ps51-ok" in r["stdout"], str(r)[:200])
            r = run("echo a && echo b", shell="powershell")
            check("powershell.amp_parse_error", r.get("error_class") == "parse_error",
                  str(r.get("error_class")) + " / " + str(r.get("message"))[:120])
            check("powershell.amp_hint", "5.1" in str(r.get("hint", "")) or ";" in str(r.get("hint", "")),
                  str(r.get("hint"))[:120])
        else:
            print("SKIP(powershell 5.1 不存在)")

        if cmd_info is not None:
            r = run("echo cmd-ok", shell="cmd")
            check("cmd.exec", r["ok"] and "cmd-ok" in r["stdout"], str(r)[:200])
        else:
            print("SKIP(cmd 执行)")

        # ---------------------------------------------------------------- 分类函数单测
        cases = [
            ("clean.rc0", classify_error(0, "fine"), None),
            ("clean.parse", classify_error(
                1, "The token '&&' is not a valid statement separator in this version.",
                command="echo a && echo b", shell_name="powershell"), "parse_error"),
            ("clean.command_not_found", classify_error(
                1, "xxx : The term 'xxx' is not recognized as a name of a cmdlet."), "command_not_found"),
            ("clean.command_not_found.zh", classify_error(
                1, "无法将“node”项识别为 cmdlet、函数、脚本文件或可运行程序的名称。"), "command_not_found"),
            ("clean.permission", classify_error(1, "Access is denied."), "permission"),
            ("clean.permission.zh", classify_error(1, "拒绝访问。"), "permission"),
            ("clean.encoding", classify_error(1, "'gbk' codec can't decode byte 0xb0"), "encoding"),
            ("clean.timeout", classify_error(None, "", timed_out=True), "timeout"),
            ("clean.network", classify_error(1, "Connection refused"), "network"),
            ("clean.not_found_path", classify_error(1, "Cannot find path 'C:\\x' because it does not exist."),
             "not_found_path"),
            ("clean.exit_nonzero", classify_error(2, "boom"), "exit_nonzero"),
        ]
        for name, got, expected in cases:
            actual = None if got is None else got.error_class
            check(name, actual == expected, f"{actual} != {expected}")
        amp = classify_error(1, "not a valid statement separator", command="a && b",
                             shell_name="powershell")
        check("clean.amp_hint_key", amp is not None and amp.hint_key == "amp_ps5")

        garbled = "这是一段足够长的正常文本，用来验证替换字符比例检测是否生效。" + "\ufffd" * 10
        got = classify_error(0, garbled)
        check("clean.replacement_ratio", got is not None and got.error_class == "encoding")
        check("clean.ansi", clean_text("\x1b[31mred\x1b[0m") == "red")
        clixml = (
            '#< CLIXML\n<Objs Version="1.1.0.1" '
            'xmlns="http://schemas.microsoft.com/powershell/2004/04">'
            '<S S="Error">boom_x000D__x000A_</S></Objs>'
        )
        cleaned = clean_text(clixml)
        check("clean.clixml", "boom" in cleaned and "CLIXML" not in cleaned, repr(cleaned))

        # ---------------------------------------------------------------- 指纹
        n1 = normalize_command_for_fingerprint('rm "C:\\a b.txt"')
        n2 = normalize_command_for_fingerprint("rm 'C:\\c.txt'")
        check("fingerprint.normalize", n1 == n2 == "rm <lit>", f"{n1!r} / {n2!r}")
        f1 = fingerprint("pwsh", 'rm "C:\\a.txt"', "exit_nonzero", "exit_nonzero")
        f2 = fingerprint("pwsh", "rm 'D:\\b.txt'", "exit_nonzero", "exit_nonzero")
        f3 = fingerprint("pwsh", 'rm "C:\\a.txt"', "permission", "permission")
        check("fingerprint.stable", f1 == f2 and len(f1) == 12, f"{f1} / {f2}")
        check("fingerprint.class_sensitive", f1 != f3)

        # ---------------------------------------------------------------- 超时杀进程树
        marker = tmp / "marker.txt"
        child = tmp / "child.py"
        child.write_text(
            "import time, pathlib\n"
            "time.sleep(4)\n"
            f"pathlib.Path({str(marker)!r}).write_text('alive', encoding='utf-8')\n",
            encoding="utf-8",
        )
        kill_cmd = (
            f"Start-Process -FilePath '{python}' -ArgumentList '{child}' -NoNewWindow; "
            "Start-Sleep -Seconds 20"
        )
        t0 = time.perf_counter()
        r = run(kill_cmd, timeout_ms=1500)
        elapsed = time.perf_counter() - t0
        check("timeout.class", r.get("error_class") == "timeout", str(r.get("error_class")))
        check("timeout.exit_none", r.get("exit_code") is None, str(r.get("exit_code")))
        check("timeout.fast", elapsed < 12, f"{elapsed:.1f}s")
        time.sleep(6)
        check("timeout.kills_tree", not marker.exists(), "孙进程仍写入了 marker")

        # ---------------------------------------------------------------- 长输出落盘
        r = run("1..3000 | ForEach-Object { 'L{0:D4}' -f $_ }")
        artifact = Path(r["artifact"]) if r.get("artifact") else None
        check("long.lines_truncated", r["truncated"] is True, str(r.get("truncated")))
        check("long.lines_count", r["total_lines"] == 3000, str(r.get("total_lines")))
        check("long.lines_artifact", artifact is not None and artifact.is_file(), str(artifact))
        check("long.lines_abs", artifact is not None and artifact.is_absolute(), str(artifact))
        check("long.lines_name", artifact is not None and artifact.name.startswith("sh_")
              and artifact.name.endswith(".txt"), artifact.name if artifact else "")
        if artifact is not None and artifact.is_file():
            full = artifact.read_text(encoding="utf-8")
            check("long.lines_complete", len(full.splitlines()) == 3000
                  and "L0500" in full and "L3000" in full)
        check("long.lines_head", "L0001" in r["stdout"] and "L0030" in r["stdout"], r["stdout"][:200])
        check("long.lines_tail", "L2941" in r["stdout"] and "L3000" in r["stdout"], r["stdout"][-300:])
        check("long.lines_no_middle", "L0500" not in r["stdout"])
        check("long.lines_hint", "Grep" in r["stdout"] and "Read" in r["stdout"])
        check("long.lines_stderr_empty", r["stderr"] == "", repr(r["stderr"][:80]))

        r = run("1..100 | ForEach-Object { 'X' * 1000 }")
        check("long.bytes_truncated", r["truncated"] is True, str(r.get("truncated")))
        check("long.bytes_count", r["total_lines"] == 100 and r["total_bytes"] > 64 * 1024,
              f"{r.get('total_lines')} / {r.get('total_bytes')}")
        check("long.bytes_artifact", bool(r.get("artifact")) and Path(r["artifact"]).is_file())

        check("long.threshold_logic",
              needs_artifact("\n".join(["x"] * 2001), max_lines=2000, max_bytes=64 * 1024)
              and not needs_artifact("\n".join(["x"] * 2000), max_lines=2000, max_bytes=64 * 1024)
              and needs_artifact("x" * (64 * 1024 + 1), max_lines=2000, max_bytes=64 * 1024))

        stub = stub_text("a\nb\nc", artifact=str(tmp / "a.txt"), header="[shell] exit=0")
        check("stub.format", "[artifact]" in stub and "--- head" in stub and "--- tail" in stub
              and "Grep" in stub and "Read" in stub)

        # ---------------------------------------------------------------- 超时上限
        r = router_tool.invoke({"command": "Write-Output 'nope'", "timeout_ms": 60000})
        check("router.limit_reject", r.get("error_class") == "router_long_command",
              str(r.get("error_class")))
        check("router.limit_hint", "create_session" in str(r.get("hint", "")), str(r.get("hint"))[:120])
        check("router.limit_not_executed", r["duration_ms"] < 1000 and r["stdout"] == "",
              str(r.get("duration_ms")))
        r = run("Write-Output 'router-default-ok'")
        check("worker.default_timeout", r["ok"], str(r.get("error_class")))
        r = worker_tool.invoke({"command": "Write-Output 'nope'", "timeout_ms": 600001})
        check("worker.limit_reject", r.get("error_class") == "parse_error", str(r.get("error_class")))
        r = run("Write-Output 'nope'", timeout_ms=0)
        check("timeout.invalid", r.get("error_class") == "parse_error", str(r.get("error_class")))
        r = run("   ")
        check("command.empty", r.get("error_class") == "parse_error", str(r.get("error_class")))

        # ---------------------------------------------------------------- Job Object 单测
        job = winjob.create_kill_on_close_job()
        proc = subprocess.Popen(
            [python, "-c", "import time; time.sleep(30)"],
            shell=False,
            env=minimal_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        winjob.assign_process(job, proc)
        winjob.terminate_job(job)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        check("winjob.terminate", proc.poll() is not None, f"poll={proc.poll()}")
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        winjob.close_job(job)

        # ---------------------------------------------------------------- 解码
        utf16 = "wsl 警告".encode("utf-16-le")
        check("decode.wsl_utf16", "wsl" in decode_bytes(utf16, "wsl"))
        check("decode.utf8", decode_bytes("中文".encode("utf-8"), "pwsh") == "中文")

        # ---------------------------------------------------------------- 连续失败计数
        reset_retry_state()
        first = run("__asst_repeat_missing__")
        second = run("__asst_repeat_missing__")
        check("retry.fingerprint_stable", first["fingerprint"] == second["fingerprint"])
        check("retry.counts", second["attempt"] == 2 and second["same_fp_count"] == 2,
              f"{second.get('attempt')}/{second.get('same_fp_count')}")
        required = {
            "ok", "exit_code", "stdout", "stderr", "total_lines", "total_bytes",
            "artifact", "duration_ms", "shell", "truncated", "error_class",
            "fingerprint", "hint", "retryable", "attempt", "same_fp_count", "message",
        }
        check("failure.body_keys", required <= set(second), str(sorted(second)))
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
