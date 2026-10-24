"""Failures the tools turn into ok:false instead of crashing."""


class ToolFailure(Exception):
    """A known problem with a plain-English message for the agent."""

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        # Paystack retries the webhook when this is true.
        self.retryable = retryable
