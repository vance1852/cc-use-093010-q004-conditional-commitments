"""规范 JSON、摘要与十进制小工具。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP


UNIT_QUANTUM = Decimal("0.001")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def quantize(value: Decimal) -> Decimal:
    return value.quantize(UNIT_QUANTUM, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")
