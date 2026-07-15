"""Frozen-binary entry shim for the reelradar-worker sidecar.

PyInstaller must NOT run reelradar/worker/sidecar.py directly as __main__ — that breaks its
package-relative imports (`from ..core ...`, `from . import ...`). Instead this shim imports
the sidecar as a PACKAGE module and calls the SAME main() the console_script uses, so the
frozen binary and `pip install -e engine && reelradar-worker` are behaviorally identical.

It ALSO routes the supervised job-child re-exec. The Phase-6 supervisor spawns each job in a
killable child via ``[sys.executable, "-m", "reelradar.worker.job_child", "--spec-file", X]``.
Under a real interpreter `-m` runs that module; but in a FROZEN binary ``sys.executable`` is
THIS binary and PyInstaller's bootloader ignores `-m` — so without this dispatch the "child"
would boot a SECOND sidecar (competing register/control-surface bind) and get killed (rc=-9),
leaving the job stuck `queued` forever. Here we detect that argv and run job_child.main instead.
"""
import sys

_JOB_CHILD_MODULE = "reelradar.worker.job_child"


def _main() -> int:
    argv = sys.argv[1:]
    # Supervised job-child re-exec: `<binary> -m reelradar.worker.job_child --spec-file X`.
    if argv[:2] == ["-m", _JOB_CHILD_MODULE]:
        from reelradar.worker.job_child import main as job_child_main
        return job_child_main(argv[2:])
    from reelradar.worker.sidecar import main
    return main()


if __name__ == "__main__":
    sys.exit(_main())
