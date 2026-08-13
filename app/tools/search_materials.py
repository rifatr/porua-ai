"""Tool: search the files the student uploaded to this room.

This is the tool the brief describes when it says the student's materials should
be something *"the AI tutor can query as needed"*. It is the difference between a
tutor that knows the subject and one that knows **this student's class** — the
notation their teacher used, the worked example on slide 12, the definition the
handout actually gave.

## Why the room is not an argument

`context.room` is the room the request was made in, resolved from the URL before
the model saw anything. There is no `room_id` parameter for the model to fill in,
so there is no way for it to read another room's material — the same reason
`query_study_history` takes no student.

## Why results carry a citation and not a chunk id

The model is told where each passage came from in the words it should repeat:
`"algebra-handout.pdf, page 4"`. Handing it an opaque id and asking it to render
a reference is a step it can get wrong; handing it the finished string is a step
it cannot.
"""

from typing import ClassVar

from pydantic import BaseModel, Field

from app.services import document as document_service
from app.tools.base import Tool, ToolContext


class SearchMaterialsArgs(BaseModel):
    query: str = Field(
        min_length=2,
        max_length=200,
        description=(
            "What to look for, in plain words. Terms from the student's question "
            "work best — 'photosynthesis light reaction', not a whole sentence. "
            "Quoted \"phrases\" match exactly."
        ),
        examples=["quadratic formula", "photosynthesis light reaction"],
    )
    limit: int = Field(
        default=document_service.DEFAULT_SEARCH_LIMIT,
        ge=1,
        le=document_service.MAX_SEARCH_LIMIT,
        description="How many passages to return. Ask for more only when one "
        "passage is unlikely to be enough.",
    )


class SearchMaterialsTool(Tool):
    name = "search_room_materials"
    description = (
        "Search the documents the student uploaded to this room — their handouts, "
        "lecture slides and notes. Use it whenever the question is about their "
        "course material: 'what does the handout say about X', 'explain the "
        "example on slide 4', or any question where their class may use different "
        "notation or a different definition from the standard one. Prefer what "
        "their own materials say over what you know generally, and cite the page "
        "you took it from. An empty result means they have not uploaded anything "
        "covering it — say so and answer from your own knowledge instead."
    )
    args_model: ClassVar[type[BaseModel]] = SearchMaterialsArgs

    async def run(self, args: SearchMaterialsArgs, context: ToolContext) -> dict:
        hits = await document_service.search(
            context.db, context.room, args.query, limit=args.limit
        )
        return {
            "query": args.query,
            "passages": [
                {"cite": hit.citation, "text": hit.text} for hit in hits
            ],
        }
