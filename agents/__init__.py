# SPDX-License-Identifier: Apache-2.0
"""Container-ready reference agents for the frozen TradeEvolve runner API.

Modules are intentionally not imported eagerly: ``python -m agents.server`` and
``python -m agents.prompt`` must start without loading provider or policy code
that the selected entrypoint does not need.
"""
