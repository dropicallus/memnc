from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FakeKV:
    _store: dict[str, bytes] = field(default_factory=dict)

    async def get(self, key: str) -> Any:
        if key not in self._store:
            raise KeyError(key)

        class Entry:
            def __init__(self, v: bytes):
                self.value = v

        return Entry(self._store[key])

    async def put(self, key: str, val: bytes) -> None:
        self._store[key] = val

    async def keys(self) -> list[str]:
        return list(self._store.keys())

    async def create_key_value(self, bucket: Any) -> Any:
        return self

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)
