"""Keep pipecat-rumik importable on newer pipecat.

pipecat-rumik 0.1.4 (the newest release) imports ``_NotGiven`` from
``pipecat.services.settings``. Pipecat moved its sentinel to
``pipecat.utils.types.NotGiven`` and dropped that private name, so the
import fails. Rumik only uses it as a type annotation, so aliasing the
public class back under the old name is enough. Import this module before
``pipecat_rumik``.
"""

import pipecat.services.settings as _settings
from pipecat.utils.types import NotGiven

if not hasattr(_settings, "_NotGiven"):
    _settings._NotGiven = NotGiven
