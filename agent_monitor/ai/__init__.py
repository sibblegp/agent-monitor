"""Optional AI annotations for a changeset. Entirely opt-in."""

from .client import AiAnnotator, AiUnavailable
from .narrator import Narrator

__all__ = ["AiAnnotator", "AiUnavailable", "Narrator"]
