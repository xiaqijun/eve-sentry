"""Screenshot-only client stub for PaddleX's optional PDF dependency."""


class PdfDocument:
    """Reject PDF inputs while satisfying PaddleX's import-time type check."""

    def __init__(self, *_args, **_kwargs) -> None:
        raise RuntimeError("PDF input is not supported by the EVE Sentry client")
