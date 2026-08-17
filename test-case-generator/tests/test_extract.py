"""单元测试：文档截取功能（辅助函数 + node_extract 节点）

测试覆盖:
- _parse_markdown_outline: Markdown 标题解析
- _match_section_by_number: 数字编号匹配
- _match_section_by_keyword: 关键词模糊匹配
- _extract_text_by_headings: 按标题截取文本
- _extract_auto: 自动截取（标题边界）
- _extract_section_range: 指令关键词提取（中文倒装句等）
- node_extract: 完整节点逻辑（小文档透传 / 自动截取 / 指定章节 / 关键词）
"""

import json
import sys
import pytest
from unittest.mock import patch, MagicMock

# 让 import 能找到 agent.py
sys.path.insert(0, ".")

from agent import (
    _parse_markdown_outline,
    _match_section_by_number,
    _match_section_by_keyword,
    _extract_text_by_headings,
    _extract_auto,
    _extract_section_range,
    node_extract,
    MAX_EXTRACT_CHARS,
    MAX_PARSE_CHARS,
    MAX_DESIGN_CHARS,
    MAX_REVIEW_CHARS,
    MAX_PROMPT_CHARS,
    WorkflowState,
)


# ============================================================
# 测试数据
# ============================================================

SMALL_TEXT = """# 用户管理模块需求

## 1. 用户注册

### 1.1 注册流程

用户通过注册页面创建账号。

### 1.2 注册限制

同一邮箱只能注册一个账号。

## 2. 用户登录

### 2.1 登录方式

用户名+密码登录。

### 2.2 登录安全

连续5次输入错误密码，账号锁定30分钟。
"""

# 构造大文档：5.x 子节内容足够多，总长超过 MAX_EXTRACT_CHARS
LARGE_CHAPTER_PARTS = []
for i in range(1, 60):
    LARGE_CHAPTER_PARTS.append(f"### 5.{i} 小节{i}内容")
    LARGE_CHAPTER_PARTS.append("这是一段测试内容，" * 100)  # 每小节约 600 字符
    LARGE_CHAPTER_PARTS.append("")
LARGE_CHAPTER = "\n".join(LARGE_CHAPTER_PARTS)

LARGE_TEXT = f"""# 税务管理系统需求

## 1. 系统概述

系统概述内容。

## 2. 用户管理

用户管理内容。

## 3. 基础配置

基础配置内容。

## 4. 发票管理

发票管理内容。

## 5. 增值税

{LARGE_CHAPTER}

## 6. 企业所得税

企业所得税内容。

### 6.1 应纳税所得额

应纳税所得额计算。

### 6.2 税率

企业所得税税率。

### 6.2.4 价税分离

价税分离处理。

### 6.2.5 价税分离补充

价税分离补充说明。

### 6.2.6 价税分离计算

价税分离计算方法。

## 7. 印花税

### 7.1 印花税概述

印花税概述内容。

### 7.2 印花税征收范围

印花税征收范围。

### 7.3 印花税计算

印花税计算方法。

## 8. 附加税

附加税内容。
"""

# 确保 LARGE_TEXT 超过 MAX_EXTRACT_CHARS
assert len(LARGE_TEXT) > MAX_EXTRACT_CHARS, (
    f"LARGE_TEXT ({len(LARGE_TEXT)}) must exceed MAX_EXTRACT_CHARS ({MAX_EXTRACT_CHARS})"
)


# ============================================================
# _parse_markdown_outline
# ============================================================

class TestParseMarkdownOutline:

    def test_basic_headings(self):
        outline = _parse_markdown_outline(SMALL_TEXT)
        titles = [h["title"] for h in outline]
        assert "用户管理模块需求" in titles
        assert "1. 用户注册" in titles
        assert "1.1 注册流程" in titles

    def test_level_detection(self):
        outline = _parse_markdown_outline(SMALL_TEXT)
        by_title = {h["title"]: h for h in outline}
        assert by_title["用户管理模块需求"]["level"] == 1
        assert by_title["1. 用户注册"]["level"] == 2
        assert by_title["1.1 注册流程"]["level"] == 3

    def test_number_extraction(self):
        outline = _parse_markdown_outline(SMALL_TEXT)
        by_title = {h["title"]: h for h in outline}
        # "1. 用户注册" → number="1"
        assert by_title["1. 用户注册"]["number"] == "1"
        # "1.1 注册流程" → number="1.1"
        assert by_title["1.1 注册流程"]["number"] == "1.1"
        # 无数字编号的标题
        assert by_title["用户管理模块需求"]["number"] == ""

    def test_start_end_positions(self):
        outline = _parse_markdown_outline(SMALL_TEXT)
        assert outline[0]["start"] >= 0
        for h in outline:
            assert h["end"] > h["start"]

    def test_empty_text(self):
        outline = _parse_markdown_outline("")
        assert outline == []

    def test_no_headings(self):
        outline = _parse_markdown_outline("纯文本内容，没有标题。")
        assert outline == []

    def test_large_text_outline(self):
        """大文档应解析出完整的标题结构"""
        outline = _parse_markdown_outline(LARGE_TEXT)
        numbers = [h["number"] for h in outline if h["number"]]
        # 应包含各章节编号
        assert "1" in numbers
        assert "5" in numbers
        assert "7" in numbers
        assert "7.3" in numbers


# ============================================================
# _match_section_by_number
# ============================================================

class TestMatchSectionByNumber:

    @pytest.fixture
    def outline(self):
        return _parse_markdown_outline(LARGE_TEXT)

    def test_single_chapter(self, outline):
        matched = _match_section_by_number(outline, "5")
        numbers = [h["number"] for h in matched]
        # "5" 本身应匹配
        assert "5" in numbers
        # 5 的子节也应包含
        has_sub = any(n.startswith("5.") for n in numbers)
        assert has_sub

    def test_chapter_range(self, outline):
        matched = _match_section_by_number(outline, "5-7")
        numbers = [h["number"] for h in matched]
        # 应包含 5, 6, 7 及其子节
        assert any(n == "5" for n in numbers)
        assert any(n == "6" for n in numbers)
        assert any(n == "7" for n in numbers)

    def test_subsection_range(self, outline):
        matched = _match_section_by_number(outline, "6.2-6.3")
        numbers = [h["number"] for h in matched]
        assert any(n.startswith("6.2") for n in numbers)

    def test_single_subsection(self, outline):
        matched = _match_section_by_number(outline, "7.3")
        numbers = [h["number"] for h in matched]
        assert "7.3" in numbers

    def test_nonexistent_number(self, outline):
        matched = _match_section_by_number(outline, "999")
        assert matched == []

    def test_empty_range(self, outline):
        matched = _match_section_by_number(outline, "")
        assert matched == []

    def test_empty_outline(self):
        matched = _match_section_by_number([], "5")
        assert matched == []


# ============================================================
# _match_section_by_keyword
# ============================================================

class TestMatchSectionByKeyword:

    @pytest.fixture
    def outline(self):
        return _parse_markdown_outline(LARGE_TEXT)

    def test_exact_keyword(self, outline):
        matched = _match_section_by_keyword(outline, "印花税计算")
        titles = [h["title"] for h in matched]
        assert any("印花税计算" in t for t in titles)

    def test_partial_keyword(self, outline):
        matched = _match_section_by_keyword(outline, "印花税")
        titles = [h["title"] for h in matched]
        assert any("印花税" in t for t in titles)

    def test_includes_subsections(self, outline):
        matched = _match_section_by_keyword(outline, "印花税")
        # "7. 印花税" 应包含其子节 7.1, 7.2, 7.3
        numbers = [h["number"] for h in matched]
        assert any(n.startswith("7.") for n in numbers)

    def test_keyword_in_title(self, outline):
        matched = _match_section_by_keyword(outline, "增值税")
        numbers = [h["number"] for h in matched]
        assert "5" in numbers

    def test_no_match(self, outline):
        matched = _match_section_by_keyword(outline, "不存在的关键词")
        assert matched == []

    def test_empty_keyword(self, outline):
        matched = _match_section_by_keyword(outline, "")
        assert matched == []

    def test_duplicate_title_picks_deepest(self):
        """同名标题出现在多个层级时，只取最深层级及其子节"""
        text = (
            "# 文档\n\n"
            "## 印花税计算\n\n二级内容。\n\n"
            "### 印花税计算\n\n"
            "#### 功能描述\n\n三级内容。\n"
        )
        outline = _parse_markdown_outline(text)
        matched = _match_section_by_keyword(outline, "印花税计算")
        levels = [h["level"] for h in matched]
        # 只命中三级（最深），不包含二级
        assert 3 in levels
        assert 2 not in levels
        # 子节（四级）应包含
        assert 4 in levels


# ============================================================
# _extract_text_by_headings
# ============================================================

class TestExtractTextByHeadings:

    def test_extract_single_section(self):
        outline = _parse_markdown_outline(SMALL_TEXT)
        target = [h for h in outline if h["number"] == "1"]
        extracted = _extract_text_by_headings(SMALL_TEXT, target)
        assert "用户注册" in extracted
        # 1 的子节 1.1, 1.2 也应包含（因为 end 包含子节内容）
        assert "注册流程" in extracted

    def test_extract_multiple_sections(self):
        outline = _parse_markdown_outline(SMALL_TEXT)
        targets = [h for h in outline if h["number"] in ("1", "2")]
        extracted = _extract_text_by_headings(SMALL_TEXT, targets)
        assert "用户注册" in extracted
        assert "用户登录" in extracted

    def test_empty_headings(self):
        extracted = _extract_text_by_headings(SMALL_TEXT, [])
        assert extracted == ""

    def test_overlapping_ranges_merged(self):
        outline = _parse_markdown_outline(SMALL_TEXT)
        parent = [h for h in outline if h["number"] == "1"][0]
        child = [h for h in outline if h["number"] == "1.1"][0]
        extracted = _extract_text_by_headings(SMALL_TEXT, [parent, child])
        assert "用户注册" in extracted


# ============================================================
# _extract_auto
# ============================================================

class TestExtractAuto:

    def test_small_text_passthrough(self):
        outline = _parse_markdown_outline(SMALL_TEXT)
        result = _extract_auto(SMALL_TEXT, outline)
        assert result.strip() == SMALL_TEXT.strip()

    def test_large_text_truncated_at_boundary(self):
        outline = _parse_markdown_outline(LARGE_TEXT)
        result = _extract_auto(LARGE_TEXT, outline)
        # 截取结果不应远超 MAX_EXTRACT_CHARS
        assert len(result) <= MAX_EXTRACT_CHARS + 2000  # 标题边界可能超出
        # 结果不应为空
        assert len(result) > 0

    def test_no_outline_fallback(self):
        text = "纯文本内容" * 5000
        result = _extract_auto(text, [])
        assert len(result) <= MAX_EXTRACT_CHARS

    def test_result_is_stripped(self):
        outline = _parse_markdown_outline(LARGE_TEXT)
        result = _extract_auto(LARGE_TEXT, outline)
        assert result == result.strip()


# ============================================================
# node_extract
# ============================================================

class TestNodeExtract:

    def test_small_document_passthrough(self):
        """小文档直接透传，不做截取"""
        state = WorkflowState(
            messages=[],
            requirement_text=SMALL_TEXT,
            section_range="",
        )
        result = node_extract(state)
        assert result["extracted_text"] == SMALL_TEXT
        assert "无需截取" in result["status_messages"][0]

    def test_auto_extract_large_document(self):
        """大文档无指定章节时，自动截取"""
        state = WorkflowState(
            messages=[],
            requirement_text=LARGE_TEXT,
            section_range="",
        )
        result = node_extract(state)
        extracted = result["extracted_text"]
        assert len(extracted) > 0
        assert "chapter_outline" in result
        assert "截取" in result["status_messages"][0]

    def test_extract_by_number(self):
        """按数字编号截取"""
        state = WorkflowState(
            messages=[],
            requirement_text=LARGE_TEXT,
            section_range="7",
        )
        result = node_extract(state)
        extracted = result["extracted_text"]
        assert "印花税" in extracted

    def test_extract_by_number_range(self):
        """按数字编号范围截取"""
        state = WorkflowState(
            messages=[],
            requirement_text=LARGE_TEXT,
            section_range="7-8",
        )
        result = node_extract(state)
        extracted = result["extracted_text"]
        assert "印花税" in extracted
        assert "附加税" in extracted

    def test_extract_by_keyword(self):
        """按语义关键词截取"""
        state = WorkflowState(
            messages=[],
            requirement_text=LARGE_TEXT,
            section_range="印花税",
        )
        result = node_extract(state)
        extracted = result["extracted_text"]
        assert "印花税" in extracted

    def test_extract_no_text(self):
        """无需求文本时报错"""
        state = WorkflowState(
            messages=[],
            requirement_text="",
        )
        result = node_extract(state)
        assert "error" in result
        assert "没有需求文本" in result["error"]

    def test_extract_no_headings_fallback(self):
        """无标题结构的大文档 fallback 到字符截取"""
        text = "纯文本内容" * 5000
        state = WorkflowState(
            messages=[],
            requirement_text=text,
            section_range="",
        )
        result = node_extract(state)
        extracted = result["extracted_text"]
        assert len(extracted) <= MAX_EXTRACT_CHARS

    def test_extract_number_not_found_fallback(self):
        """指定编号不存在时，fallback 到自动截取"""
        state = WorkflowState(
            messages=[],
            requirement_text=LARGE_TEXT,
            section_range="999",
        )
        result = node_extract(state)
        extracted = result["extracted_text"]
        assert len(extracted) > 0
        assert "自动截取" in result["status_messages"][0]

    def test_keyword_not_found_ai_fallback(self):
        """关键词匹配不到时，AI 兜底定位"""
        # "税务核算" 不在标题中，会走到 AI 兜底
        with patch("agent._locate_section_by_ai", return_value="6.2.4-6.2.6"):
            state = WorkflowState(
                messages=[],
                requirement_text=LARGE_TEXT,
                section_range="税务核算",
            )
            result = node_extract(state)
            extracted = result["extracted_text"]
            assert "价税分离" in extracted
            assert "AI 定位" in result["status_messages"][0]

    def test_keyword_not_found_ai_also_fails(self):
        """关键词和 AI 都匹配不到时，fallback 到自动截取"""
        with patch("agent._locate_section_by_ai", return_value=""):
            state = WorkflowState(
                messages=[],
                requirement_text=LARGE_TEXT,
                section_range="不存在的关键词xyz",
            )
            result = node_extract(state)
            extracted = result["extracted_text"]
            assert len(extracted) > 0
            assert "自动截取" in result["status_messages"][0]

    def test_chapter_outline_generated(self):
        """大文档应生成 chapter_outline"""
        state = WorkflowState(
            messages=[],
            requirement_text=LARGE_TEXT,
            section_range="",
        )
        result = node_extract(state)
        outline = json.loads(result["chapter_outline"])
        assert isinstance(outline, list)
        assert len(outline) > 0


# ============================================================
# node_parse 使用 extracted_text
# ============================================================

class TestNodeParseUsesExtractedText:
    """验证 node_parse 优先使用 extracted_text 而非硬截断"""

    def test_parse_prefers_extracted_text(self):
        """node_parse 应优先使用 extracted_text"""
        from agent import node_parse

        extracted = "这是截取后的短文本"
        state = WorkflowState(
            messages=[],
            extracted_text=extracted,
            requirement_text=LARGE_TEXT,
        )
        with patch("agent._safe_llm_invoke") as mock_safe:
            mock_safe.return_value = '{"status": "ok"}'

            result = node_parse(state)
            call_args = mock_safe.call_args
            prompt = call_args[0][0]
            assert extracted in prompt
            assert "文本过长，已截断" not in prompt

    def test_parse_fallback_to_requirement_text(self):
        """没有 extracted_text 时，fallback 到 requirement_text"""
        from agent import node_parse

        state = WorkflowState(
            messages=[],
            requirement_text=SMALL_TEXT,
        )
        with patch("agent._safe_llm_invoke") as mock_safe:
            mock_safe.return_value = '{"status": "ok"}'

            result = node_parse(state)
            call_args = mock_safe.call_args
            prompt = call_args[0][0]
            assert SMALL_TEXT.strip() in prompt


# ============================================================
# _extract_section_range
# ============================================================

class TestExtractSectionRange:
    """测试指令关键词提取：从中文指令中剥离动词/目标词，提取章节关键词"""

    def test_verb_first_with_suffix(self):
        """生成一下印花税计算的测试用例 → 印花税计算"""
        assert _extract_section_range("生成一下印花税计算的测试用例") == "印花税计算"

    def test_topic_first(self):
        """印花税计算 生成测试用例 → 印花税计算"""
        assert _extract_section_range("印花税计算 生成测试用例") == "印花税计算"

    def test_compound_verb(self):
        """写出登录安全部分的测试用例 → 登录安全部分"""
        assert _extract_section_range("写出登录安全部分的测试用例") == "登录安全部分"

    def test_no_topic(self):
        """生成测试用例 → 空"""
        assert _extract_section_range("生成测试用例") == ""

    def test_do_verb_with_suffix(self):
        """做一下增值税的测试用例 → 增值税"""
        assert _extract_section_range("做一下增值税的测试用例") == "增值税"

    def test_run_verb(self):
        """跑一下登录安全的测试 → 登录安全"""
        assert _extract_section_range("跑一下登录安全的测试") == "登录安全"

    def test_empty_input(self):
        """空输入 → 空"""
        assert _extract_section_range("") == ""

    def test_whitespace_only(self):
        """纯空格 → 空"""
        assert _extract_section_range("   ") == ""

    def test_quotes_stripped(self):
        """引号被去除"""
        assert _extract_section_range('"印花税计算"') == "印花税计算"

    def test_topic_too_long(self):
        """超过 30 字符的关键词被忽略"""
        long_topic = "A" * 31
        assert _extract_section_range(long_topic + "的测试用例") == ""

    def test_partial_verb_not_stripped(self):
        """动词字符不出现在正常关键词中时不应被误删"""
        # "出" 不在开头/末尾指令位置，不应被删
        assert _extract_section_range("数据导出功能") == "数据导出功能"

    def test_with_leading_quotes(self):
        """带引号的路径后文本"""
        assert _extract_section_range('" 生成一下印花税计算的测试用例') == "印花税计算"


# ============================================================
# node_parse 安全截断
# ============================================================

class TestNodeParseSafetyTruncation:
    """验证 node_parse 对超大 extracted_text 的安全截断"""

    def test_parse_truncates_large_extracted_text(self):
        """extracted_text 超过 MAX_PARSE_CHARS 时被截断"""
        from agent import node_parse

        # 构造超过 MAX_PARSE_CHARS 的文本
        large_text = "需求内容行\n" * 6000  # ~36K chars > MAX_PARSE_CHARS(15000)
        state = WorkflowState(
            messages=[],
            extracted_text=large_text,
        )
        with patch("agent._safe_llm_invoke") as mock_safe:
            mock_safe.return_value = '{"status": "ok"}'

            result = node_parse(state)
            # _safe_llm_invoke 被调用，参数是 prompt 和 node_name
            call_args = mock_safe.call_args
            prompt = call_args[0][0]  # 第一个位置参数是 prompt
            # prompt 中的需求文本部分不应超过 MAX_PARSE_CHARS
            requirement_section = prompt.split("## 需求文本")[-1] if "## 需求文本" in prompt else prompt
            assert len(requirement_section) < MAX_PARSE_CHARS + 500  # 加上截断提示
            # 应包含截断提示
            assert "长度限制已截断" in prompt

    def test_parse_no_truncation_for_small_text(self):
        """小文本不截断"""
        from agent import node_parse

        state = WorkflowState(
            messages=[],
            extracted_text=SMALL_TEXT,
        )
        with patch("agent._safe_llm_invoke") as mock_safe:
            mock_safe.return_value = '{"status": "ok"}'

            result = node_parse(state)
            call_args = mock_safe.call_args
            prompt = call_args[0][0]
            assert "长度限制已截断" not in prompt
