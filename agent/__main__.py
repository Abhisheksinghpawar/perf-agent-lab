"""python -m agent <doctor|demo|run|test> - one friendly entry point."""
import subprocess
import sys

HELP = """perf-agent-lab

  python -m agent doctor        check this machine (Java, Python, ports, network) and say how to fix problems
  python -m agent demo          full pipeline incl. both agents with a scripted LLM - no API key needed
  python -m agent run [flags]   the real pipeline (flags: --force --no-llm --demo-heal --set section.key=value)
  python -m agent test          run the unit tests

First time here? Run `doctor`, then `demo`.
"""


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    rest = sys.argv[2:]
    if cmd == "doctor":
        from .doctor import main as m
        return m(rest)
    if cmd == "demo":
        from .demo import main as m
        return m(rest)
    if cmd == "run":
        from .run import main as m
        return m(rest)
    if cmd == "test":
        return subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", "tests", *(rest or ["-v"])])
    print(HELP)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main())
