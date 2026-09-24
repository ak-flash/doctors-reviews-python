import subprocess
import sys


if __name__ == "__main__":
    args = sys.argv[1:]
    live = "-live" in args or "--live" in args
    args = [arg for arg in args if arg not in {"-live", "--live"}]
    command = [sys.executable, "-m", "pytest", "tests/test_sentiment.py", "-k", "live"]
    if live:
        command.insert(4, "--live")
    else:
        command.append("--collect-only")
    raise SystemExit(subprocess.call(command + args))
