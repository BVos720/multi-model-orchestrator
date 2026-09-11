"""PyInstaller entry point - a plain script `orchestrator.cli:main`
resolves reliably for the same reason `python -m orchestrator.cli` does but
a pip console-script wrapper sometimes doesn't under PyInstaller's static
import analysis."""

from orchestrator.cli import main

if __name__ == "__main__":
    main()
