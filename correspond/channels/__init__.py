"""The built-in channel adapters, one module each.

The registry imports a module only when its channel is first used, so an adapter's
dependencies (none, for the v0.1 adapters) are never paid for by code that does not use
that channel.
"""
