"""Skills: a prompt, plus code that checks the model's work.

    base.py          what a skill is, and the check-then-repair loop they share
    verify.py        algebra checked with sympy
    quiz_builder.py  a quiz whose four named failure modes are handled
    step_solver.py   working where every step has been verified
    registry.py      the fixed list

`app/services/skill.py` runs them and saves the result.
"""
