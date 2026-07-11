"""The deterministic query-compiler core (Task 6).

``spec.py``   — the ``SearchSpec`` model + the depart/nights DSL parsers.
``planner.py``— ``compile(spec) -> CallPlan`` (pure) and ``execute(plan)``.

Everything an agent, a saved search, or the CLI produces reduces to a
``SearchSpec``; the planner compiles it to a typed, inspectable ``CallPlan``
(``plan`` command, no network) and runs it (``run`` command).
"""
