"""Drop ``temperature`` from VLM requests to Claude models on v0.4.20 (IMPR-1311).

``OpenAIVLM._apply_completion_params`` sets ``kwargs["temperature"] = self.temperature``
for every non-OpenAI-reasoning model, and ``VLMConfig.temperature`` is a ``float``
(default 0.0), so the config cannot leave it out. Claude Haiku 5.5 on Anthropic's
OpenAI-compatible endpoint rejects any request carrying it: ``400 invalid_request_error
`temperature` is deprecated for this model``. Sending ``temperature: null`` is accepted,
but the config validator refuses ``null``.

This wrapper runs the original, then removes ``temperature`` when the request's model
starts with ``claude-``. Every other model (``gemma4:vlm`` today) is untouched, so the
patch is inert until the VLM config names a Claude model. ``thinking: disabled`` and
``max_tokens`` need no patch: ``vlm.extra_request_body`` and ``vlm.max_tokens`` already
reach the request.

Version/source guarded on ``OpenAIVLM._apply_completion_params``;
``OV_CLAUDE_PARAMS_PATCH=0`` rolls back. Remove it once upstream omits ``temperature``
for models that reject it, or ``VLMConfig.temperature`` accepts ``null``.
"""

import functools
import hashlib
import inspect
import logging
import os

EXPECTED_VERSION = "v0.4.20"
EXPECTED_SHA256 = "9775efe39413dbef927ce723db147f4165144ec31fd74b5f681ec0267a7e87cb"
DROPPED = ("temperature",)
logger = logging.getLogger("ov_claude_params_patch")


def is_claude_model(model):
    return isinstance(model, str) and model.lower().startswith("claude-")


def dropping_claude_params(original):
    @functools.wraps(original)
    def _apply_completion_params(self, kwargs, *args, **kw):
        result = original(self, kwargs, *args, **kw)
        if is_claude_model(kwargs.get("model")):
            for key in DROPPED:
                kwargs.pop(key, None)
        return result

    _apply_completion_params._ov_claude_params_patch = True
    return _apply_completion_params


def apply(module):
    if os.environ.get("OV_CLAUDE_PARAMS_PATCH", "1") == "0":
        logger.warning("ov-claude-params-patch: disabled by OV_CLAUDE_PARAMS_PATCH=0")
        return False
    import openviking

    cls = getattr(module, "OpenAIVLM", None)
    original = getattr(cls, "_apply_completion_params", None)
    if original is None:
        logger.warning(
            "ov-claude-params-patch: NOT applied (OpenAIVLM._apply_completion_params missing)"
        )
        return False
    if getattr(original, "_ov_claude_params_patch", False):
        return True
    try:
        digest = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()
    except (OSError, TypeError):
        digest = None
    version = getattr(openviking, "__version__", None)
    if version != EXPECTED_VERSION or digest != EXPECTED_SHA256:
        logger.warning(
            "ov-claude-params-patch: NOT applied (version=%r sha256=%s)",
            version,
            digest,
        )
        return False
    cls._apply_completion_params = dropping_claude_params(original)
    logger.warning(
        "ov-claude-params-patch: applied; %s dropped for claude-* models (pid %d)",
        ", ".join(DROPPED),
        os.getpid(),
    )
    return True
