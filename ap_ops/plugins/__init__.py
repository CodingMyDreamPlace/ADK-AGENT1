"""Plugins de AP Ops: el eje de operaciones."""

from __future__ import annotations

from .auditoria import PluginAuditoriaAP
from .ops_advisor import PLANTILLAS, recomendar

__all__ = ["PluginAuditoriaAP", "PLANTILLAS", "recomendar"]
