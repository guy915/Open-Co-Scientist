from __future__ import annotations


class CacheablePrompt(str):
    # Offsets come from the renderer, never from delimiter text supplied by a user.
    run_end: int
    item_end: int

    def __new__(cls, text: str, *, run_end: int, item_end: int) -> CacheablePrompt:
        if type(run_end) is not int or type(item_end) is not int:
            raise ValueError("Cache boundaries must be integer offsets")
        if not 0 < run_end < item_end <= len(text):
            raise ValueError("Cache boundaries must follow the rendered blocks")
        value = super().__new__(cls, text)
        value.run_end = run_end
        value.item_end = item_end
        return value

    def __add__(self, suffix: str) -> CacheablePrompt:
        return CacheablePrompt(str(self) + suffix, run_end=self.run_end, item_end=self.item_end)

    def __getnewargs_ex__(self) -> tuple[tuple[str], dict[str, int]]:
        return (str(self),), {"run_end": self.run_end, "item_end": self.item_end}
