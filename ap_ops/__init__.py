"""AP Ops — Centro de operaciones de Cuentas por Pagar (coordinador conversacional).

Entry point 1 de los dos que define el plan: un LlmAgent coordinador que expone el
pipeline de triaje como herramientas, mas el gate de human-in-the-loop para todo lo
que mueva dinero. Ver PLAN-AP-OPS.md.
"""

from . import agent  # noqa: F401
