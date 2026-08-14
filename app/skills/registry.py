"""The list of skills that exist. Fixed at import, never built from input.

Same property as the tool registry, for the same reason: a skill name arrives in
a URL, from a client we do not control, and a dict lookup is the only thing that
turns it into code. No `getattr`, no `importlib`, no naming convention. An
unknown name misses and becomes a 404.
"""

from app.skills.base import Skill
from app.skills.quiz_builder import QuizBuilderSkill
from app.skills.step_solver import StepSolverSkill


def _build() -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for skill_class in (QuizBuilderSkill, StepSolverSkill):
        instance = skill_class()
        if instance.name in skills:  # pragma: no cover - guards a typo at import
            raise RuntimeError(f"Two skills are both called {instance.name!r}")
        skills[instance.name] = instance
    return skills


# Built once at import. Skills are stateless, so one instance each is enough.
_SKILLS: dict[str, Skill] = _build()


def resolve(name: str) -> Skill | None:
    return _SKILLS.get(name)


def names() -> tuple[str, ...]:
    return tuple(_SKILLS)
