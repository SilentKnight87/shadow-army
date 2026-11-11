"""Native, profile-owned cross-family completion receipts."""


def register(ctx):
    from .bridge import register_backend
    register_backend(ctx)
