#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试 —— 在提交前抓住「语法合法但语义错误」的问题。

**为什么需要它**

真实案例：`_memory_mb()` 被误加了 `@contextmanager` 装饰器。
`py_compile` 完全通过（装饰器语义错误在语法上合法），
但运行时返回的是 `_GeneratorContextManager` 对象而不是 int，
于是 `avail < 700` 抛 TypeError —— 而这个函数只在**实际跑浏览器时**才被调用，
也就是说：**只有部署到 VPS 上才会暴露**。

这类错误的共同特征：
  1. 语法合法，静态检查查不出来
  2. 只有真正调用才暴露
  3. 暴露得很晚（部署后、甚至真实任务跑到一半）

所以这里对关键函数做**实际调用**，而不只是语法检查。

用法：
    python3 smoke_test.py
"""

from __future__ import annotations

import ast
import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

FAILED = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILED.append(name)


def test_no_contextmanager_on_plain_functions() -> None:
    """普通函数不能带 @contextmanager。

    @contextmanager 会让函数返回「待进入的上下文」而不是返回值，
    任何拿它做数值/字符串操作的地方都会炸。
    """
    print("【1】装饰器语义检查")
    targets = ["browser_engine.py", "agent/panstar_agent.py",
               "turnstile_solver.py", "panstar_sign.py"]
    bad = []
    for rel in targets:
        path = os.path.join(APP_DIR, rel)
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            decs = [ast.unparse(d) for d in node.decorator_list]
            if "contextmanager" not in decs:
                continue
            # contextmanager 里必须用 yield，不能 return <值>
            has_yield = any(isinstance(n, (ast.Yield, ast.YieldFrom))
                            for n in ast.walk(node))
            returns_value = any(isinstance(n, ast.Return) and n.value is not None
                                for n in ast.walk(node))
            if returns_value and not has_yield:
                bad.append(f"{rel}:{node.lineno} {node.name}()")
            elif not has_yield:
                bad.append(f"{rel}:{node.lineno} {node.name}() 没有 yield")
    check("没有 contextmanager 误用", not bad, "; ".join(bad))


def test_memory_helpers() -> None:
    """内存相关函数必须真的返回 int/bool。"""
    print("【2】内存辅助函数")
    try:
        from browser_engine import _low_memory_mode, _memory_mb
    except Exception as exc:  # noqa: BLE001
        check("导入 browser_engine", False, str(exc))
        return

    try:
        avail = _memory_mb()
    except Exception as exc:  # noqa: BLE001
        check("_memory_mb() 可调用", False, f"{type(exc).__name__}: {exc}")
        return
    check("_memory_mb() 返回 int", isinstance(avail, int),
          f"实际 {type(avail).__name__} = {avail!r}")

    try:
        low = _low_memory_mode()
    except Exception as exc:  # noqa: BLE001
        check("_low_memory_mode() 可调用", False, f"{type(exc).__name__}: {exc}")
        return
    check("_low_memory_mode() 返回 bool", isinstance(low, bool),
          f"实际 {type(low).__name__} = {low!r}")

    # 曾经真实踩过的坑：avail < 700
    try:
        _ = avail < 700
        check("avail < 700 可比较", True, f"{avail} < 700")
    except TypeError as exc:
        check("avail < 700 可比较", False, str(exc))


def test_env_parsing() -> None:
    """配置解析：空值不占位、最后一个非空值生效。"""
    print("【3】配置解析")
    try:
        from panstar_sign import load_env_file
    except Exception as exc:  # noqa: BLE001
        check("导入 panstar_sign", False, str(exc))
        return

    import tempfile

    cases = [
        ("空值在前真值在后", "K=\nK=REAL\n", "REAL"),
        ("真值在前空值在后", "K=REAL\nK=\n", "REAL"),
        ("两个真值取最后一个", "K=OLD\nK=NEW\n", "NEW"),
        ("只有空值", "K=\n", None),
    ]
    for name, content, expect in cases:
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(content)
            path = fh.name
        os.environ.pop("K", None)
        try:
            load_env_file(path)
            got = os.environ.get("K")
            check(name, got == expect, f"期望 {expect!r}，实际 {got!r}")
        finally:
            os.environ.pop("K", None)
            try:
                os.unlink(path)
            except OSError:
                pass


def test_agent_helpers() -> None:
    """agent 的鉴权逻辑。"""
    print("【4】agent 鉴权")
    sys.path.insert(0, os.path.join(APP_DIR, "agent"))
    try:
        import panstar_agent
    except Exception as exc:  # noqa: BLE001
        check("导入 panstar_agent", False, str(exc))
        return

    # auth_ok 依赖 Flask 的 request 上下文，要用 test_request_context
    orig = panstar_agent.AGENT_TOKEN
    try:
        panstar_agent.AGENT_TOKEN = "secret"
        with panstar_agent.app.test_request_context(
            headers={"Authorization": "Bearer secret"}
        ):
            check("token 正确时鉴权通过", panstar_agent.auth_ok() is True)
        with panstar_agent.app.test_request_context(
            headers={"Authorization": "Bearer wrong"}
        ):
            check("token 错误时鉴权拒绝", panstar_agent.auth_ok() is False)
        with panstar_agent.app.test_request_context():
            check("没带 token 时鉴权拒绝", panstar_agent.auth_ok() is False)

        panstar_agent.AGENT_TOKEN = ""
        with panstar_agent.app.test_request_context():
            check("未配 token 时放开", panstar_agent.auth_ok() is True)
    except Exception as exc:  # noqa: BLE001
        check("agent 鉴权测试", False, f"{type(exc).__name__}: {exc}")
    finally:
        panstar_agent.AGENT_TOKEN = orig


def test_playwright_api_signatures() -> None:
    """Playwright 调用不能传多余的参数。

    真实案例：`page.add_init_script` 的签名是 `(script, path)` ——
    只接受 2 个参数，不支持像 `evaluate` 那样往脚本里传变量。
    把 evaluate 的写法套上去就会炸：
      TypeError: Page.add_init_script() takes from 1 to 2 positional
                 arguments but 4 were given
    而且只在实际跑浏览器时才暴露（和 @contextmanager 那个坑一样）。

    这里用 AST 静态检查几个容易搞混的 API 的**位置参数个数**。
    """
    print("【5】Playwright API 参数检查")

    # 方法名 -> (最大位置参数个数, 说明)
    LIMITS = {
        "add_init_script": (2, "签名是 (script, path)，不能传变量"),
        "goto": (2, "签名是 (url, **kwargs)"),
        "evaluate": (2, "签名是 (expression, arg)"),
        "evaluate_handle": (2, "签名是 (expression, arg)"),
        "set_content": (2, "签名是 (html, **kwargs)"),
        "query_selector": (2, "签名是 (selector)"),
        "wait_for_selector": (2, "签名是 (selector, **kwargs)"),
        "fill": (3, "签名是 (selector, value, **kwargs)"),
    }

    targets = ["agent/panstar_agent.py", "browser_engine.py",
               "turnstile_solver.py"]
    problems = []
    for rel in targets:
        path = os.path.join(APP_DIR, rel)
        if not os.path.exists(path):
            continue
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            name = func.attr
            if name not in LIMITS:
                continue
            limit, why = LIMITS[name]
            n = len(node.args)
            if n > limit:
                problems.append(
                    f"{rel}:{node.lineno} {name}() 传了 {n} 个位置参数"
                    f"（上限 {limit}，{why}）"
                )
    check("Playwright API 参数个数正确", not problems, "; ".join(problems))


def main() -> int:
    print("=" * 62)
    print("冒烟测试")
    print("=" * 62)
    test_no_contextmanager_on_plain_functions()
    test_memory_helpers()
    test_env_parsing()
    test_agent_helpers()
    test_playwright_api_signatures()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：")
        for name in FAILED:
            print(f"   - {name}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
