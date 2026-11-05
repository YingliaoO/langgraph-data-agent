"""Optional descriptions for the current SQL schema.

The public demo reads tables, columns and foreign keys from MySQL
INFORMATION_SCHEMA. Historical Recipe schema descriptions are intentionally
not distributed with this snapshot.
"""

from typing import Dict, List, Tuple

TABLE_DESCRIPTIONS: Dict[str, str] = {}
COLUMN_DESCRIPTIONS: Dict[Tuple[str, str], str] = {}
RELATIONSHIP_FACTS: List[Dict[str, str]] = []
DOMAIN_SUMMARY = ""
