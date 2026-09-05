"""Shared exceptions for one-submit production adapters."""


class AdapterError(RuntimeError):
    """A deterministic adapter refusal that is safe to expose in a receipt."""


class AmbiguousSubmit(RuntimeError):
    """The adapter cannot tell whether its single submit reached the target."""
