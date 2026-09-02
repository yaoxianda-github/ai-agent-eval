"""生成全部任务 fixtures（幂等，可重复运行）。

用法：
    python scripts/gen_fixtures.py

说明：
    - 文本类 fixtures（T001 / T102 / T401 / T502）由本脚本写入；
    - T205 的 JPG 图片需要 Pillow，安装：pip install pillow；
    - 已提交到仓库的 fixtures 与本脚本输出一致，可直接使用。
"""

from __future__ import annotations

import io
import random
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# ---- 文本 fixtures（与仓库中已提交内容保持一致）----

T001_NOTES = """# 会议笔记

记录于 2026/9/2，主要讨论 Q3 目标。

- Q3 目标确认：截止 2026/12/25 前上线
- 发布计划：2026/3/5 前完成内测
- 回访安排在 2026/11/11

备注：以上日期需统一为 ISO 格式。
"""

T001_MEETING = """# 周会纪要

- 会议时间：2026/1/15
- 上线时间定为 2026/2/28
- 灰度窗口 2026/3/1 至 2026/3/7
"""

T001_README = """# 项目说明

版本 2026/7/30 发布，回滚窗口至 2026/8/2。
下次迭代计划 2026/9/15。
"""

T102_SALES = """date,category,amount
2026-08-01,数码,3200.00
2026-08-01,家居,1500.00
2026-08-02,数码,2100.00
2026-08-02,食品,300.00
2026-08-03,服饰,800.00
2026-08-03,数码,1800.00
2026-08-04,家居,2600.00
2026-08-04,图书,200.00
2026-08-05,食品,450.00
2026-08-05,家居,1200.00
2026-08-06,数码,990.00
2026-08-06,服饰,1200.00
2026-08-07,食品,260.00
2026-08-07,图书,150.00
2026-08-08,家居,1400.00
2026-08-08,食品,520.00
2026-08-09,数码,1560.00
2026-08-09,食品,380.00
"""

T401_SCRIPT = '''"""从 input/data.csv 找出金额最高的记录，写入 output/result.txt。

故意注入的缺陷（请修复）：
  1) 读取路径错误：读的是 "./data.csv"，应为 "input/data.csv"
  2) 取最大值用错了列：比较的是 "date"，应为 "amount"
"""

import csv
import os

os.makedirs("output", exist_ok=True)

with open("./data.csv", encoding="utf-8") as f:  # BUG 1: 路径错误
    rows = list(csv.DictReader(f))

best = max(rows, key=lambda r: r["date"])  # BUG 2: 应比较 amount
line = f"{best['date']},{best['category']},{best['amount']}\\n"

with open("output/result.txt", "w", encoding="utf-8") as f:
    f.write(line)

print("done")
'''

T401_DATA = """date,category,amount
2026-08-05,数码,1899.00
2026-08-06,家居,399.00
2026-08-07,数码,5299.00
2026-08-08,食品,120.50
2026-08-09,服饰,459.00
"""

T502_LOGS = """# 项目群一周聊天记录（模拟）

## 周一
- 小林：早，本周主线是订单导出功能联调。
- 阿澈：收到，我这边接口文档 10 点前给到。
- 小林：@阿澈 接口里的分页参数命名要统一一下，现在是 page/size。
- 阿澈：OK，我改成一页 page_size。
- 测试：导出功能的测试用例我已经列了 12 条，下午过一遍。

## 周二
- 小林：联调环境今天 14:00 可以开始用，大家把改动合并到 dev 分支。
- 阿澈：导出接口已联调通过，TPS 大概 200。
- 测试：12 条用例跑了 10 条，其中 1 条大文件导出偶发超时，正在复现。
- 小林：超时问题标记为 P1，今天内给结论。

## 周三
- 测试：大文件导出超时复现了，是内存没释放，连续导出 5 次后 GC 压力大。
- 阿澈：收到，我加个分批写盘，晚上出修复版。
- 小林：好，P1 今天必须关掉。
- 产品：导出列顺序要按配置中心顺序，不要写死。

## 周四
- 阿澈：修复版已上 dev，分批写盘后连续导出 20 次稳定。
- 测试：P1 复测通过，剩下 2 条用例今天跑完。
- 小林：周五 18:00 发版，发版清单让测试确认。
- 测试：发版清单已确认：导出功能 + 分页参数 + 配置列顺序。

## 周五
- 小林：今天发版，线上监控盯到 20:00。
- 测试：回归全过，可以发。
- 阿澈：导出接口压测数据已更新到文档。
- 小林：下周计划：开始做报表中心，需求评审定在周二上午。

## 风险/阻塞
- 大文件导出内存问题（已修复，需线上观察 1 周）
- 接口文档与实现偶有不同步，需统一维护入口
"""

TEXT_FIXTURES: dict[str, dict[str, str]] = {
    "T001": {
        "input/notes.md": T001_NOTES,
        "input/meeting.md": T001_MEETING,
        "input/readme.md": T001_README,
    },
    "T102": {"input/sales.csv": T102_SALES},
    "T401": {"input/script.py": T401_SCRIPT, "input/data.csv": T401_DATA},
    "T502": {"input/chat_logs.md": T502_LOGS},
}


def write_text_fixtures() -> None:
    for task_id, files in TEXT_FIXTURES.items():
        for rel, content in files.items():
            p = REPO / "tasks" / task_id / "fixtures" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            print(f"[text] {p.relative_to(REPO)}")


def gen_t205_images(n: int = 20, seed: int = 42) -> bool:
    """生成 n 张带 EXIF DateTimeOriginal 的 JPG 图片。"""
    try:
        from PIL import Image
    except ImportError:
        print("[skip] 缺少 Pillow，跳过 T205 图片生成。请先: pip install pillow")
        return False

    out = REPO / "tasks" / "T205" / "fixtures" / "input"
    out.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)
    base = datetime(2026, 1, 1)
    for i in range(n):
        taken = base + timedelta(days=rng.randint(0, 300))
        img = Image.new(
            "RGB",
            (64, 64),
            color=(rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)),
        )
        exif = Image.Exif()
        exif[0x9003] = taken.strftime("%Y:%m:%d %H:%M:%S")  # DateTimeOriginal
        buf = io.BytesIO()
        img.save(buf, format="JPEG", exif=exif)
        (out / f"photo_{i:02d}.jpg").write_bytes(buf.getvalue())
    print(f"[image] 生成 {n} 张带 EXIF 图片到 tasks/T205/fixtures/input/")
    return True


def main() -> None:
    write_text_fixtures()
    gen_t205_images()
    print("完成。")


if __name__ == "__main__":
    main()
