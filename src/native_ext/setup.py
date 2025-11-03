from setuptools import setup, Extension
import sys
import numpy as np

ext = Extension(
    "btcore",
    sources=["backtest_core.c"],   # 상대경로
    include_dirs=[np.get_include()],
    extra_compile_args=["-O3", "-fopenmp"] if sys.platform != "win32" else ["/O2"],
    extra_link_args=["-fopenmp"] if sys.platform != "win32" else [],
)

setup(
    name="btcore",
    version="0.1.0",
    description="C accelerated backtest core",
    ext_modules=[ext],
)
