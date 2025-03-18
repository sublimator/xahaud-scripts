import pyperclip


def get_clipboard() -> str:
    """Get the contents of the clipboard."""
    return pyperclip.paste()
