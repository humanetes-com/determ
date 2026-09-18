"""application.tasks — one module per pipeline task (use case).

Each task is a plain function taking its inputs plus a frozen ``*Deps`` record of injected
collaborators. No task imports ``pipeline.py``; collaborators flow in from the composition
root. Tasks migrate out of ``pipeline.py`` one at a time, `bash check.sh` green
at every step.
"""
