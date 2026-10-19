from __future__ import annotations

from .base import StandardAdapter
from .sseries import OEMAdapter, S1000DAdapter, S2000MAdapter, S3000LAdapter

_ADAPTERS: dict[str, StandardAdapter] = {}


def register(adapter: StandardAdapter) -> None:
    _ADAPTERS[adapter.family] = adapter


for _a in (S1000DAdapter(), S2000MAdapter(), S3000LAdapter(), OEMAdapter()):
    register(_a)


def adapter_for(family: str) -> StandardAdapter | None:
    return _ADAPTERS.get(family.upper())


def all_adapters() -> list[StandardAdapter]:
    return list(_ADAPTERS.values())
