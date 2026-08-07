"""Entry point for the frozen backend.

PyInstaller freezes a *script*, not a module, so `python -m agent_monitor` has
no equivalent here. This is that script: it does nothing but hand control to the
same `main()` the console script uses, so the frozen binary and the source
checkout take identical code paths.
"""

import multiprocessing
import sys

from agent_monitor.__main__ import main

if __name__ == "__main__":
    # Frozen apps that spawn processes re-execute this binary; without this the
    # child re-runs the server instead of the worker and forks endlessly.
    multiprocessing.freeze_support()
    sys.exit(main())
