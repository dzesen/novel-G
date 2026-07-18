"""大纲预览的纯校验函数：不碰数据库、不抛异常，返回问题列表供调用方 report 或 raise。

分层理由与 context_builder 的 assemble/fetch 分离同源：校验逻辑必须能脱离 MongoDB
全覆盖单测，且预览端点（report 模式）与 accept 服务（raise 模式）共用同一实现。
"""

from __future__ import annotations

from typing import Any, Dict, List


def validate_chapter_ranges(volumes: List[Dict[str, Any]], number_of_chapters: int) -> List[str]:
    """校验分卷 chapter_range：互不重叠、无缝隙、恰好覆盖 1..number_of_chapters。

    Args:
        volumes: 每项含 chapter_range={start,end}（全书章号）。
        number_of_chapters: 全书章数；末卷 end 必须恰好等于它。

    Returns:
        问题描述列表；空列表表示合法。不抛异常——缺字段/类型错也转成问题项，
        因为 accept 的前置守卫依赖它"返回问题"而不是"崩溃"。
    """
    problems: List[str] = []
    if not volumes:
        return ["分卷列表为空，无法建卷"]
    if number_of_chapters <= 0:
        problems.append(f"小说 number_of_chapters={number_of_chapters} 无效，无法校验区间")

    parsed: List[tuple] = []
    for idx, vol in enumerate(volumes, start=1):
        rng = vol.get("chapter_range") if isinstance(vol, dict) else None
        if not isinstance(rng, dict) or "start" not in rng or "end" not in rng:
            problems.append(f"第 {idx} 卷缺少 chapter_range")
            continue
        try:
            start, end = int(rng["start"]), int(rng["end"])
        except (TypeError, ValueError):
            problems.append(f"第 {idx} 卷 chapter_range 不是整数：{rng}")
            continue
        if start < 1:
            problems.append(f"第 {idx} 卷 start={start} 必须 >= 1")
        if end < start:
            problems.append(f"第 {idx} 卷 end={end} 小于 start={start}")
        parsed.append((start, end))

    # 有结构性问题时不再做跨卷检查——顺序已不可靠，避免叠加噪音。
    if problems:
        return problems

    parsed.sort(key=lambda r: r[0])
    if parsed[0][0] != 1:
        problems.append(f"首卷必须从第 1 章开始，实际为第 {parsed[0][0]} 章")
    for (prev_start, prev_end), (cur_start, cur_end) in zip(parsed, parsed[1:]):
        if cur_start > prev_end + 1:
            problems.append(f"第 {prev_end} 章与第 {cur_start} 章之间有缝隙（缺 {prev_end + 1}..{cur_start - 1}）")
        elif cur_start <= prev_end:
            problems.append(f"区间重叠：{cur_start}..{prev_end} 被两卷同时覆盖")
    if number_of_chapters > 0 and parsed[-1][1] != number_of_chapters:
        problems.append(f"末卷须覆盖到第 {number_of_chapters} 章，实际到第 {parsed[-1][1]} 章")
    return problems
