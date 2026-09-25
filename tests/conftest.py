"""Test-session guard: keep derived artifacts out of reference/**.

`bumble` is installed editable from reference/upstream/bumble, so importing it
would otherwise write __pycache__/*.pyc underneath the immutable evidence tree.
Disabling bytecode writing here (before any test module imports bumble) keeps
`reference/**` byte-identical across test runs. Verifier scripts run outside
pytest should be invoked with PYTHONDONTWRITEBYTECODE=1 for the same reason.
"""

import sys

sys.dont_write_bytecode = True
