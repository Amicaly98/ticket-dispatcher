"""根 conftest：把仓库根放进 sys.path，让 `import src` / `import tests` 在 pytest 下可用。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
