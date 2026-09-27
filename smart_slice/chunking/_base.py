# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.


from abc import ABC, abstractmethod
from typing import List


class IChunkHandle(ABC):
    @abstractmethod
    def handle(self, chunk_list: List[str], chunk_size: int = 256):
        pass
