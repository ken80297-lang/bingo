def test_desktop_imports_do_not_create_tk_root():
    import tkinter as tk

    import desktop
    import desktop.app
    import desktop.core.data_repository
    import desktop.ui.main_window

    assert desktop is not None
    assert desktop.app is not None
    assert desktop.core.data_repository is not None
    assert tk._default_root is None

