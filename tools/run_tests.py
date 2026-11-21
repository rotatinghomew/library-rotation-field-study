"""隔离模式测试入口：``python3 -I tools/run_tests.py``。

``-I`` 不把当前目录加入 sys.path，这里显式加入仓库根后再调用 unittest，
保证读取不受信资料时代码路径仍受隔离保护。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
