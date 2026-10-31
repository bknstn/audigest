"""JSON rendering for canonical transcripts."""

import json

from audigest.models import Transcript


def render_json(transcript: Transcript) -> str:
    """Return the canonical transcript as a standalone JSON object."""
    return json.dumps(
        transcript.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
    )
