from contextvars import ContextVar

# Shared by the analyzer and the common DataFetcher path. Startup prewarm
# can disable historical writes without disabling ordinary API persistence.
snapshot_persistence_enabled = ContextVar(
    "snapshot_persistence_enabled",
    default=True,
)
