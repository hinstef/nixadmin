"""nixadmin — ambient system intelligence daemon for NixOS.

Keep this module import-light: clients and module authors import submodules
(:mod:`nixadmin.protocol`, :mod:`nixadmin.sdk`) directly and must not be forced
to pull in the daemon's heavy dependencies by importing the package.
"""

__version__ = "0.1.0"
