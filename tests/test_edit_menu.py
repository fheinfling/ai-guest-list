"""Editing shortcut definitions run everywhere; real menu wiring requires PyObjC."""
import pytest

from app import menubar


def test_edit_menu_definitions():
    command, shift = 1 << 20, 1 << 17
    assert menubar.edit_menu_items(command, shift) == (
        ("Undo", "undo:", "z", command, None),
        ("Redo", "redo:", "Z", command | shift, None),
        ("Cut", "cut:", "x", command, None),
        ("Copy", "copy:", "c", command, None),
        ("Paste", "paste:", "v", command, None),
        ("Select All", "selectAll:", "a", command, None),
    )


@pytest.mark.skipif(menubar.objc is None, reason="native menu installation requires PyObjC")
def test_native_edit_menu_preserves_accessory_policy():
    application = menubar.NSApplication.sharedApplication()
    previous_menu = application.mainMenu()
    previous_policy = application.activationPolicy()
    try:
        application.setActivationPolicy_(menubar.NSApplicationActivationPolicyAccessory)
        assert application.activationPolicy() == menubar.NSApplicationActivationPolicyAccessory
        menubar.install_edit_menu(application)
        assert application.activationPolicy() == menubar.NSApplicationActivationPolicyAccessory

        menu = application.mainMenu()
        assert menu.numberOfItems() == 1
        parent = menu.itemAtIndex_(0)
        assert parent.title() == "Edit"
        assert parent.target() is None
        edit = parent.submenu()
        assert edit.title() == "Edit"
        assert edit.numberOfItems() == 6
        expected = menubar.edit_menu_items(
            menubar.NSEventModifierFlagCommand, menubar.NSEventModifierFlagShift)
        for item, (title, selector, key, modifiers, target) in zip(edit.itemArray(), expected):
            assert item.title() == title
            assert item.action() == selector.encode("ascii")
            assert item.keyEquivalent() == key
            assert item.keyEquivalentModifierMask() == modifiers
            assert item.target() is target is None
    finally:
        application.setMainMenu_(previous_menu)
        application.setActivationPolicy_(previous_policy)
