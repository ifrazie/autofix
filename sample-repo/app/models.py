"""Domain models."""

from dataclasses import dataclass


@dataclass
class User:
    id: str
    email: str
    is_active: bool = True


@dataclass
class Document:
    id: str
    title: str
    body: str
