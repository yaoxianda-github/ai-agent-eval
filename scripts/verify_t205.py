"""T205 判定：index.csv 中每个 dest_path 都存在，且归档目录 YYYY/MM 与源图 EXIF 时间一致。

用法：python scripts/verify_t205.py <工作目录>
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ws = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")

try:
    from PIL import Image
except ImportError:
    print("SKIP: 缺少 Pillow，无法校验 EXIF")
    sys.exit(2)

index = ws / "output" / "index.csv"
rows = list(csv.DictReader(index.open(encoding="utf-8")))
if not rows:
    print("FAIL: index.csv 为空")
    sys.exit(1)

for r in rows:
    src = ws / r["file"]
    if not src.exists():
        print(f"FAIL: 源文件不存在 {r['file']}")
        sys.exit(1)
    exif = Image.open(src).getexif()
    ts = exif.get(0x9003, "")
    if not ts:
        print(f"FAIL: {r['file']} 缺少 EXIF DateTimeOriginal")
        sys.exit(1)
    yy, mm = ts.split(" ")[0].split(":")[:2]
    expect_prefix = f"output/archive/{yy}/{mm}"
    dest = r["dest_path"].replace("\\", "/")
    if not dest.startswith(expect_prefix):
        print(f"FAIL: {r['file']} 归档 {dest} 与 EXIF {yy}/{mm} 不一致")
        sys.exit(1)
    if not (ws / dest).exists():
        print(f"FAIL: 归档文件不存在 {dest}")
        sys.exit(1)

print(f"PASS: {len(rows)} 条归档路径与 EXIF 时间一致")
