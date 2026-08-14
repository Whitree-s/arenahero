"""世界生成确定性测试：同一 seed 必须生成逐字节一致的地图。

背景（2026-08-07 架构评审 P0#1）：_break_large_clusters 曾使用模块级
random.sample，受进程/调用顺序影响——多进程进化时同一 seed 的地图
不确定，GA 的 (基因, seed) 缓存失去语义，评估噪声被放大。

运行：.venv/bin/python tests/test_world_determinism.py
"""

import hashlib
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ahsim.world import World


def _fingerprint(w):
    """terrain 逐字节哈希 + 资源点集合 + dirty_chunks。"""
    h = hashlib.sha256()
    for row in w.terrain:
        h.update(bytes(row))
    res = sorted(w.resources)
    return h.hexdigest(), tuple(res), frozenset(w.dirty_chunks)


def _gen(size=256, seed=42, density=0.15):
    return World(size=size, seed=seed, obstacle_density=density)


def test_same_seed_same_map():
    a, b = _gen(), _gen()
    assert _fingerprint(a) == _fingerprint(b)


def test_same_seed_after_other_worlds():
    """调用顺序不应影响结果（模块级 random 的典型污染模式）。"""
    _gen(seed=7)
    _gen(seed=99)
    a = _gen(seed=42)
    _gen(seed=1234)
    b = _gen(seed=42)
    assert _fingerprint(a) == _fingerprint(b)


def test_different_seed_different_map():
    a, b = _gen(seed=42), _gen(seed=43)
    assert _fingerprint(a) != _fingerprint(b)


def test_plain_world_deterministic():
    a = World(size=64, seed=5, plain=True)
    b = World(size=64, seed=5, plain=True)
    assert _fingerprint(a) == _fingerprint(b)


def test_subprocess_same_map():
    """跨进程确定性（进化用进程池，worker 间地图必须一致）。"""
    code = (
        "import sys; sys.path.insert(0, '.'); "
        "from ahsim.world import World; "
        "import hashlib; "
        "w = World(size=256, seed=42, obstacle_density=0.15); "
        "h = hashlib.sha256(); "
        "[h.update(bytes(r)) for r in w.terrain]; "
        "print(h.hexdigest(), sorted(w.resources)[:3])"
    )
    outs = set()
    for _ in range(2):
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, check=True)
        outs.add(r.stdout.strip())
    assert len(outs) == 1, f"跨进程地图不一致: {outs}"


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
