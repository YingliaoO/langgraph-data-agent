"""Text2Cypher component exports.

Exports are lazy so importing the legacy SQL compatibility module under this
package does not initialize every Graph/Text2Cypher dependency.
"""

from importlib import import_module

_EXPORTS = {
    "create_text2cypher_correction_node": ".correction",
    "create_text2cypher_execution_node": ".execution",
    "create_text2cypher_generation_node": ".generation",
    "create_text2cypher_validation_node": ".validation",
    "get_text2cypher_schema": ".schema",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module_name, __name__), name)
