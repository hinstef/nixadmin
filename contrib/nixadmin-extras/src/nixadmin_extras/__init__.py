"""nixadmin-extras — extra modules for common desktop and admin questions.

Each submodule exposes a ``manifest`` registered via the ``nixadmin.modules``
entry point (see pyproject.toml). Installing this package alongside nixadmin makes
the daemon discover these modules automatically — no core changes needed. This is
the reference example of the third-party module pattern.
"""

__version__ = "0.1.0"
