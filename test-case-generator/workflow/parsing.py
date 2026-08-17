"""Markdown 标题解析、章节截取、指令关键词提取。

依赖：workflow.state（MAX_EXTRACT_CHARS）+ workflow.config（llm/logger）。
"""

import re

from langchain_core.messages import HumanMessage

from workflow.config import llm, logger
from workflow.state import MAX_EXTRACT_CHARS

_RE_MD_HEADING = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)
# 数字编号标题正则：如 "5.1 企业所得税"、"7.3.1 印花税计算"
_RE_NUM_HEADING = re.compile(r'^(\d+(?:\.\d+)*)\.?\s+(.+)$', re.MULTILINE)


def _parse_markdown_outline(text: str) -> list[dict]:
    """解析 Markdown 文本的标题结构，返回章节目录。

    返回: [{"level": 1, "title": "...", "number": "5.1", "start": 0, "end": 1500}, ...]
    start/end 是该章节内容在原文中的字符位置（含标题行到下一标题行之前）。
    """
    headings: list[dict] = []

    # 1. 收集所有标题行及其位置
    for m in _RE_MD_HEADING.finditer(text):
        level = len(m.group(1))
        title = m.group(2).strip()
        headings.append({
            "level": level,
            "title": title,
            "number": "",
            "start": m.start(),
        })

    # 2. 对于 # 标题，尝试从标题文本中提取数字编号
    #    如 "# 5.1 企业所得税" → number="5.1"
    for h in headings:
        num_match = _RE_NUM_HEADING.match(h["title"])
        if num_match:
            h["number"] = num_match.group(1)

    # 3. 计算 end 位置（到下一个同级或更高级标题之前）
    for i, h in enumerate(headings):
        # 找下一个同级或更高级标题
        end = len(text)
        for j in range(i + 1, len(headings)):
            if headings[j]["level"] <= h["level"]:
                end = headings[j]["start"]
                break
        h["end"] = end

    return headings


def _match_section_by_number(outline: list[dict], section_range: str) -> list[dict]:
    """按数字编号匹配章节。

    section_range 格式:
    - "5" → 匹配 number 以 "5" 开头的所有章节（含子节）
    - "5-7" → 匹配 number 在 5~7 范围的一级章节（含子节）
    - "5.1-5.3" → 匹配 number 在 5.1~5.3 范围的章节
    """
    if not section_range or not outline:
        return []

    # 解析范围
    range_match = re.match(r'^(\d+(?:\.\d+)*)\s*[-–]\s*(\d+(?:\.\d+)*)$', section_range)
    if range_match:
        start_num = range_match.group(1)
        end_num = range_match.group(2)
    else:
        start_num = section_range.strip()
        end_num = None

    matched = []
    for h in outline:
        num = h.get("number", "")
        if not num:
            continue

        # 单个编号匹配：前缀匹配（"5" 匹配 "5"、"5.1"、"5.2.3" 等）
        if end_num is None:
            if num == start_num or num.startswith(start_num + "."):
                matched.append(h)
        else:
            # 范围匹配：比较编号前缀
            # "5-7" → 匹配一级编号 5,6,7 及其子节
            # "5.1-5.3" → 匹配 5.1, 5.2, 5.3 及其子节
            start_parts = start_num.split(".")
            end_parts = end_num.split(".")
            num_parts = num.split(".")

            # 比较到范围的最深层级
            depth = max(len(start_parts), len(end_parts))
            in_range = True
            for d in range(depth):
                n_val = int(num_parts[d]) if d < len(num_parts) else 0
                s_val = int(start_parts[d]) if d < len(start_parts) else 0
                e_val = int(end_parts[d]) if d < len(end_parts) else 999

                if n_val < s_val or n_val > e_val:
                    in_range = False
                    break
                # 如果在范围内，继续比较更深层级
                if n_val > s_val and n_val < e_val:
                    # 已经在范围中间，子节都算
                    in_range = True
                    break

            if in_range:
                matched.append(h)

    return matched


def _match_section_by_keyword(outline: list[dict], keyword: str) -> list[dict]:
    """按关键词在标题中匹配章节。

    匹配策略（精确优先）：
    1. 精确匹配：标题文本 == keyword → 若同名标题出现在多个层级，取最深层级
       （最里面的子章节）那一个及其子节；只有一个时取它及其子节
    2. 模糊匹配：无精确命中时，标题文本包含 keyword → 取所有命中标题及其子节

    这样当文档中同时存在 "印花税计算"（二级大章节）和 "印花税计算"（三级小节）时，
    用户输入 "印花税计算" 只会命中三级小节（最深层级），而不会把整个二级大章节
    也拉进来，也不会把 "印花税计算结果" 等子串命中的标题拉进来。
    """
    if not keyword or not outline:
        return []

    keyword_lower = keyword.lower()

    # 1. 尝试精确匹配
    exact_matches = [h for h in outline if h["title"].lower() == keyword_lower]

    if exact_matches:
        # 同名标题出现在多个层级时，取最深层级（最里面的子章节）那一个
        target = max(exact_matches, key=lambda h: h["level"])
        matched = [target]
        for sub in outline:
            if sub["level"] > target["level"] and sub["start"] >= target["start"] and sub["start"] < target["end"]:
                matched.append(sub)
        return matched

    # 2. 无精确命中，fallback 到子串模糊匹配
    primary_matches = [h for h in outline if keyword_lower in h["title"].lower()]

    if not primary_matches:
        return []

    matched = []
    for h in primary_matches:
        matched.append(h)
        for sub in outline:
            if sub["level"] > h["level"] and sub["start"] >= h["start"] and sub["start"] < h["end"]:
                matched.append(sub)

    return matched


def _extract_text_by_headings(text: str, headings: list[dict]) -> str:
    """根据匹配到的标题列表，从原文中截取对应文本。

    合并所有匹配章节的文本范围，去重排序后截取。
    """
    if not headings:
        return ""

    # 合并重叠的范围
    ranges = sorted([(h["start"], h["end"]) for h in headings], key=lambda x: x[0])
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # 截取并拼接
    parts = [text[start:end] for start, end in merged]
    return "\n\n".join(parts).strip()


def _extract_auto(text: str, outline: list[dict], max_chars: int = MAX_EXTRACT_CHARS) -> str:
    """自动截取：在标题边界处截取前 max_chars 字符，保证末章节完整。"""
    if len(text) <= max_chars:
        return text

    # 找到不超过 max_chars 的最后一个标题边界
    cut_pos = max_chars
    for h in outline:
        if h["start"] > max_chars:
            # 回退到前一个标题的开始位置
            cut_pos = h["start"]
            break
    else:
        # 所有标题都在 max_chars 内，直接截取
        cut_pos = max_chars

    # 如果 cut_pos 还是太大（没有标题在 max_chars 之后），用最后一个标题的 end
    if cut_pos > max_chars and outline:
        # 找 start <= max_chars 的最后一个标题
        last_heading = None
        for h in outline:
            if h["start"] <= max_chars:
                last_heading = h
            else:
                break
        if last_heading:
            cut_pos = min(last_heading["end"], len(text))
        else:
            cut_pos = max_chars

    return text[:cut_pos].strip()


def _locate_section_by_ai(outline: list[dict], keyword: str) -> str:
    """AI 兜底：只发目录 JSON 给 LLM，让它定位章节编号。

    返回定位到的 section_range 字符串（如 "6.2.4-6.2.6"），空字符串表示定位失败。
    """
    # 构建精简目录
    outline_items = []
    for h in outline:
        prefix = "  " * (h["level"] - 1)
        num_str = f"{h['number']} " if h["number"] else ""
        outline_items.append(f"{prefix}{num_str}{h['title']}")

    outline_text = "\n".join(outline_items)

    # 截断目录（避免目录本身过长）
    if len(outline_text) > 4000:
        outline_text = outline_text[:4000] + "\n[... 目录过长，已截断 ...]"

    prompt = f"""以下是一份需求文档的章节目录。请找到与"{keyword}"最相关的章节编号。

只输出章节编号范围，格式示例：
- 单个章节：5.1
- 章节范围：6.2.4-6.2.6
- 整章：5

如果找不到相关章节，输出空字符串。

## 章节目录

{outline_text}"""

    try:
        response = llm.invoke([HumanMessage(content=prompt)])
        content = response.content.strip() if isinstance(response.content, str) else str(response.content)
        # 提取编号范围
        match = re.match(r'^(\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?)$', content)
        if match:
            return match.group(1)
        # 尝试从输出中提取
        match = re.search(r'(\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?)', content)
        if match:
            return match.group(1)
        return ""
    except Exception as e:
        logger.warning("AI section locate failed: %s", e)
        return ""


# ============================================================
# Step 5.6: 指令关键词提取辅助函数
# ============================================================

# 指令动词（复合动词优先匹配，避免"写出"被"出"抢先匹配）
_INSTR_COMPOUND_VERBS = r'(?:生成|写出|做出|跑出|运行)'
_INSTR_SINGLE_VERBS = r'(?:写|做|跑|出)'
_INSTR_SUFFIXES = r'(?:一下|一些|一下下|些)'
_INSTR_TARGETS = r'(?:测试用例|用例|测试|需求文档|文档|测试文档)'


def _extract_section_range(after_path: str) -> str:
    """从路径后的文本中提取章节关键词。

    剥离指令词策略：先删除末尾的"测试用例"等目标词，
    再删除开头/末尾的指令动词，剩余即为关键词。

    Examples:
        "生成一下印花税计算的测试用例" → "印花税计算"
        "印花税计算 生成测试用例" → "印花税计算"
        "写出登录安全部分的测试用例" → "登录安全部分"
        "生成测试用例" → ""
    """
    cleaned = after_path.strip().strip('"').strip()
    if not cleaned:
        return ""

    # 1. 删除末尾的指令目标词（的测试用例 / 测试用例 / 测试）
    cleaned = re.sub(r'(?:的)?' + _INSTR_TARGETS + r'\s*$', '', cleaned)

    # 2. 删除开头的复合指令动词+后缀（生成一下 / 写出 / 做一下）
    cleaned = re.sub(r'^' + _INSTR_COMPOUND_VERBS + _INSTR_SUFFIXES + r'?\s*', '', cleaned)
    # 3. 删除开头的单字指令动词+后缀（写一下 / 做一下）
    cleaned = re.sub(r'^' + _INSTR_SINGLE_VERBS + _INSTR_SUFFIXES + r'?\s*', '', cleaned)

    # 4. 删除末尾的指令动词（印花税计算 生成 → 印花税计算）
    cleaned = re.sub(r'\s*' + _INSTR_COMPOUND_VERBS + _INSTR_SUFFIXES + r'?\s*$', '', cleaned)
    cleaned = re.sub(r'\s*' + _INSTR_SINGLE_VERBS + _INSTR_SUFFIXES + r'?\s*$', '', cleaned)

    # 5. 删除末尾的"的"
    cleaned = re.sub(r'的$', '', cleaned).strip()

    return cleaned if (cleaned and len(cleaned) <= 30) else ""
