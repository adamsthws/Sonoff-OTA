"""Autoflash: a plug converted over the air, between its stock firmware,
Tasmota and ESPHome.

The modules here orchestrate the reviewed tools in tools/; they do not
reimplement any validator, Berry phase or safety gate.
"""

# The tool's own version, x.y.z - not any firmware's. The one place it is set:
# a release tags it from here (.github/workflows/release.yml), with its notes
# from CHANGELOG.md.
__version__ = "2.1.3"
