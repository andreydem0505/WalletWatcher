"""Credential value only; encryption and persistence belong to storage."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Credentials:
    address: str
    private_key: str = field(repr=False)
