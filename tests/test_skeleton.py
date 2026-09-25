"""骨架占位测试：保证测试框架可运行。"""


def test_skeleton_imports():
    import app.audit  # noqa: F401
    import app.errors  # noqa: F401
    import app.guard  # noqa: F401
    import app.identity  # noqa: F401
    import app.quota  # noqa: F401
