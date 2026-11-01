"""Content sizing is testable without constructing an AppKit application."""
import pytest

from app.menubar import popover_height, resize_popover


@pytest.mark.parametrize("content, expected", [
    (0, 220), (219, 220), (220, 220), (284.2, 285),
    (513, 513), (600, 600), (601, 600), (100_000, 600), (-10, 220),
])
def test_content_height_is_rounded_and_bounded(content, expected):
    assert popover_height(content) == expected


@pytest.mark.parametrize("content", [None, True, False, "400", [], {}, float("nan"),
                                      float("inf"), float("-inf")])
def test_invalid_content_height_is_ignored(content):
    assert popover_height(content) is None


def test_resize_handler_keeps_width_and_can_shrink_after_expansion():
    class Popover:
        def __init__(self):
            self.sizes = []

        def setContentSize_(self, size):
            self.sizes.append(size)

    popover = Popover()
    for value in (450.1, 890, 280, "bad", None, float("nan"), 100):
        resize_popover(popover, value, lambda w, h: (w, h))
    assert popover.sizes == [(376, 451), (376, 600), (376, 280), (376, 220)]
