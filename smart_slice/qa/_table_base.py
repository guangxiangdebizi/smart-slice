# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.


from abc import ABC, abstractmethod


class BaseParseTableHandle(ABC):
    @abstractmethod
    def support(self, file, get_buffer):
        pass

    @abstractmethod
    def handle(self, file, get_buffer,save_image):
        pass

    @abstractmethod
    def get_content(self, file, save_image):
        pass
