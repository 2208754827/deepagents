"""8 个工作流节点 + 脚本执行 + JSON 提取 + 结果消息构建。

依赖：workflow.config（BASE_DIR/logger/_safe_llm_invoke）+ workflow.state
（WorkflowState/MAX_*）+ workflow.parsing（解析辅助）+ workflow.prompts（builder）。

patch-target 路由层（见下）：`_safe_llm_invoke_routed` / `_locate_section_by_ai_routed`
在调用时经 `sys.modules.get("agent")` 转发——pytest 下 `agent` 模块被 patch 则命中 mock，
`python agent.py` 下 `agent` 模块不在 sys.modules（运行入口是 `__main__`）则回落到直连。
仅这两个 helper 需路由；其余解析辅助均为直连导入。
"""

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from workflow.config import BASE_DIR, _safe_llm_invoke, logger
from workflow.parsing import (
    _extract_auto,
    _extract_section_range,
    _extract_text_by_headings,
    _locate_section_by_ai as _direct_locate_section_by_ai,
    _match_section_by_keyword,
    _match_section_by_number,
    _parse_markdown_outline,
)
from workflow.prompts import (
    build_design_prompt,
    build_parse_prompt,
    build_review_prompt,
)
from workflow.state import (
    MAX_DESIGN_CHARS,
    MAX_EXTRACT_CHARS,
    MAX_PARSE_CHARS,
    MAX_REVIEW_CHARS,
    WorkflowState,
)


# ============================================================
# patch-target 路由层
# ============================================================
# 测试做 `from agent import node_parse` 后 `patch("agent._safe_llm_invoke")` /
# `patch("agent._locate_section_by_ai")`，期望 node_parse/node_extract 的内部调用
# 命中 mock。单体 agent.py 下 node_parse 与 _safe_llm_invoke 共享 agent 模块全局
# 命名空间，patch 天然生效。拆分后 node_parse 迁入 workflow.nodes，其裸名
# `_safe_llm_invoke` 解析到 workflow.nodes 全局（自 workflow.config 导入），
# patch("agent.X") 不再生效 → 测试断裂。
# 路由层在调用时经 sys.modules.get("agent") 转发：pytest 下 agent 在 sys.modules
# 且被 patch → 命中 mock；python agent.py 下入口是 __main__，agent 不在
# sys.modules → 回落到直连（自 config/parsing 导入）。无需 backwards import agent。
def _safe_llm_invoke_routed(prompt: str, node_name: str = "") -> str:
    """路由 _safe_llm_invoke：agent 模块在 sys.modules 且被 patch 时走 mock，否则直连。"""
    mod = sys.modules.get("agent")
    if mod is not None and getattr(mod, "_safe_llm_invoke", None) is not None:
        return mod._safe_llm_invoke(prompt, node_name)
    return _safe_llm_invoke(prompt, node_name)


def _locate_section_by_ai_routed(outline: list[dict], keyword: str) -> str:
    """路由 _locate_section_by_ai：agent 模块在 sys.modules 且被 patch 时走 mock，否则直连。"""
    mod = sys.modules.get("agent")
    if mod is not None and getattr(mod, "_locate_section_by_ai", None) is not None:
        return mod._locate_section_by_ai(outline, keyword)
    return _direct_locate_section_by_ai(outline, keyword)


# ============================================================
# Step 6.1: 脚本执行辅助
# ============================================================
def _run_script(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """运行脚本，返回 (returncode, stdout, stderr)"""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            cwd=str(BASE_DIR),
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


# ============================================================
# Step 6.2: 节点 1 — 入口节点（解析 description）
# ============================================================
def node_entry(state: WorkflowState) -> dict:
    """入口节点: 从 messages 中提取需求文档路径和章节范围

    主 Agent 通过 task() 调用工作流时，description 会被放入 messages。
    例如: task(subagent_type="generate-workflow", description="根据 C:/Users/test.docx 第5章生成测试用例")
    本节点解析 description，提取文件路径、目录路径、章节范围，设置到状态中。
    """
    messages = state.get("messages", [])
    description = ""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            description = msg.content or ""
            break

    requirement_file = ""
    requirement_dir = ""
    section_range = ""

    if description:
        # 尝试提取文件路径（支持 .docx/.doc/.pdf/.md）
        # 路径可能包含中文、空格等字符，用贪婪匹配到已知扩展名
        file_match = re.search(
            r'([a-zA-Z]:[\\\/].+?\.(?:docx|doc|pdf|md))',
            description,
            re.IGNORECASE,
        )
        if file_match:
            requirement_file = file_match.group(1).replace("/", "\\")
        else:
            # 尝试提取目录路径（以 \ 或 / 结尾，或后面跟着空格/中文标点）
            dir_match = re.search(
                r'([a-zA-Z]:[\\\/].+?[\\\/])(?:\s|，|。|$)',
                description,
            )
            if dir_match:
                path = dir_match.group(1)
                if Path(path).is_dir():
                    requirement_dir = path.replace("/", "\\")

        # 提取章节范围
        # 1. 数字编号范围：5-7、5.1-5.3
        range_match = re.search(
            r'(?:第|章节|节)\s*(\d+(?:\.\d+)?\s*[-–]\s*\d+(?:\.\d+)?)',
            description,
        )
        if range_match:
            section_range = range_match.group(1).replace(" ", "")
        else:
            # 2. 单个数字编号：第5章、5.1节
            num_match = re.search(
                r'(?:第\s*(\d+)\s*章|(\d+(?:\.\d+)?)\s*节)',
                description,
            )
            if num_match:
                section_range = num_match.group(1) or num_match.group(2)
            else:
                # 3. 语义关键词：印花税计算章节、价税分离部分
                kw_match = re.search(
                    r'(?:截取|提取|只看|只要|针对|关于|的)\s*["""]?([^"""]+?)["""]?\s*(?:章节|部分|模块|内容|需求)',
                    description,
                )
                if kw_match:
                    section_range = kw_match.group(1).strip()
                else:
                    # 4. 直接跟在路径后面的章节描述
                    #    "生成一下印花税计算的测试用例" → "印花税计算"
                    #    "印花税计算 生成测试用例" → "印花税计算"
                    after_path = description
                    if file_match:
                        after_path = description[file_match.end():]
                    elif dir_match:
                        after_path = description[dir_match.end():]
                    section_range = _extract_section_range(after_path)

    updates = {}
    if requirement_file:
        updates["requirement_file"] = requirement_file
    if requirement_dir:
        updates["requirement_dir"] = requirement_dir
    if section_range:
        updates["section_range"] = section_range

    return updates if updates else {}


# ============================================================
# Step 6.3: 节点 2 — 初始化记忆
# ============================================================
def node_init_memory(state: WorkflowState) -> dict:
    """Phase 0: 初始化 .memory/ 目录"""
    memory_dir = BASE_DIR / ".memory"
    if memory_dir.exists() and (memory_dir / "project-context.json").exists():
        return {"status_messages": ["[初始化] .memory/ 已存在，跳过"]}

    rc, stdout, stderr = _run_script(
        [sys.executable, "scripts/memory_manager.py", "--action", "init", "--project", "."],
    )
    if rc == 0:
        return {"status_messages": ["[初始化] .memory/ 创建成功"]}
    else:
        logger.warning("memory_manager init failed: %s", stderr)
        return {"status_messages": [f"[初始化] memory_manager 失败: {stderr[:200]}"]}


# ============================================================
# Step 6.4: 节点 3 — 预处理需求文档
# ============================================================
def node_preprocess(state: WorkflowState) -> dict:
    """Phase 1: 预处理需求文档

    支持三种输入：
    1. requirement_file: 单个文件路径（.docx/.pdf/.md）
    2. requirement_dir: 目录路径，扫描其中所有文档
    3. 默认: 扫描 requirements/ 目录

    纯 Python 实现：转换非 Markdown 文件、读取文本。
    """
    req_file = state.get("requirement_file", "")
    req_dir = state.get("requirement_dir", "")

    # 优先处理单个文件
    if req_file:
        file_path = Path(req_file)
        if not file_path.exists():
            return {
                "error": f"需求文件不存在: {req_file}",
                "status_messages": [f"[预处理] 错误: 需求文件不存在 {req_file}"],
            }

        # 如果是 .md 文件，直接读取
        if file_path.suffix.lower() == ".md":
            try:
                content = file_path.read_text(encoding="utf-8")
                return {
                    "requirement_text": content.strip(),
                    "status_messages": [f"[预处理] 读取 {file_path.name}，共 {len(content)} 字符"],
                }
            except Exception as e:
                return {
                    "error": f"读取文件失败: {e}",
                    "status_messages": [f"[预处理] 错误: 读取 {req_file} 失败"],
                }

        # 如果是 .docx/.pdf，先转换
        if file_path.suffix.lower() in (".docx", ".doc", ".pdf"):
            # 将文件复制到 requirements/ 目录
            import shutil
            req_dir_path = BASE_DIR / "requirements"
            req_dir_path.mkdir(exist_ok=True)
            dest = req_dir_path / file_path.name
            if not dest.exists():
                shutil.copy2(str(file_path), str(dest))

            # 运行转换脚本
            rc, stdout, stderr = _run_script(
                [sys.executable, "scripts/requirements_preprocessor.py", "--root", str(req_dir_path)],
            )
            if rc != 0:
                logger.warning("preprocessor script failed: %s", stderr)
                return {
                    "error": f"转换文件失败: {stderr[:200]}",
                    "status_messages": [f"[预处理] 错误: 转换 {file_path.name} 失败"],
                }

            # 读取转换后的 .md
            md_file = req_dir_path / (file_path.stem + ".md")
            if md_file.exists():
                content = md_file.read_text(encoding="utf-8")
                return {
                    "requirement_text": content.strip(),
                    "status_messages": [f"[预处理] 转换并读取 {file_path.name}，共 {len(content)} 字符"],
                }
            else:
                return {
                    "error": f"转换后未找到 .md 文件: {md_file}",
                    "status_messages": [f"[预处理] 错误: 转换 {file_path.name} 后未生成 .md"],
                }

        # 其他格式，尝试直接读取
        try:
            content = file_path.read_text(encoding="utf-8")
            return {
                "requirement_text": content.strip(),
                "status_messages": [f"[预处理] 读取 {file_path.name}，共 {len(content)} 字符"],
            }
        except Exception as e:
            return {
                "error": f"不支持的文件格式: {file_path.suffix}",
                "status_messages": [f"[预处理] 错误: 不支持 {file_path.suffix} 格式"],
            }

    # 处理目录
    if not req_dir:
        req_dir = str(BASE_DIR / "requirements")

    req_path = Path(req_dir)
    if not req_path.exists():
        return {
            "error": f"需求目录不存在: {req_dir}",
            "status_messages": [f"[预处理] 错误: 需求目录不存在 {req_dir}"],
        }

    # 1. 转换非 Markdown 文件
    rc, stdout, stderr = _run_script(
        [sys.executable, "scripts/requirements_preprocessor.py", "--root", str(req_path)],
    )
    if rc != 0:
        logger.warning("preprocessor script failed (non-fatal): %s", stderr)

    # 2. 读取所有 .md 文件
    md_files = sorted(req_path.rglob("*.md"))
    if not md_files:
        return {
            "error": f"需求目录中没有 Markdown 文件: {req_dir}",
            "status_messages": [f"[预处理] 错误: 没有找到 .md 文件"],
        }

    texts = []
    for f in md_files:
        try:
            content = f.read_text(encoding="utf-8")
            if content.strip():
                texts.append(content.strip())
        except Exception as e:
            logger.warning("Failed to read %s: %s", f, e)

    full_text = "\n\n---\n\n".join(texts)

    return {
        "requirement_text": full_text,
        "status_messages": [
            f"[预处理] 读取 {len(md_files)} 个需求文件，共 {len(full_text)} 字符"
        ],
    }


# ============================================================
# Step 6.5: 节点 4 — 文档截取
# ============================================================
def node_extract(state: WorkflowState) -> dict:
    """Phase 1.5: 文档截取节点

    对大文档按章节截取，避免 parse 节点硬截断导致内容丢失。
    - 小文档（≤15000字符）：直接透传
    - 大文档 + 指定章节编号：按编号截取
    - 大文档 + 语义关键词：先关键词匹配标题，匹配不到则 AI 兜底
    - 大文档 + 无指定：自动在标题边界截取前 15000 字符
    """
    requirement_text = state.get("requirement_text", "")
    section_range = state.get("section_range", "")

    if not requirement_text:
        return {
            "error": "没有需求文本可截取",
            "status_messages": ["[截取] 错误: 没有需求文本"],
        }

    # 小文档直接透传
    if len(requirement_text) <= MAX_EXTRACT_CHARS:
        return {
            "extracted_text": requirement_text,
            "status_messages": [f"[截取] 文档较小（{len(requirement_text)}字符），无需截取"],
        }

    # 大文档：解析目录
    outline = _parse_markdown_outline(requirement_text)

    # 生成章节目录 JSON
    outline_summary = []
    for h in outline:
        prefix = "  " * (h["level"] - 1)
        num_str = f"{h['number']} " if h["number"] else ""
        outline_summary.append(f"{prefix}{num_str}{h['title']}")
    chapter_outline = json.dumps(outline_summary, ensure_ascii=False)

    # 如果没有标题结构，fallback 到字符截取
    if not outline:
        extracted = requirement_text[:MAX_EXTRACT_CHARS]
        return {
            "extracted_text": extracted,
            "chapter_outline": "[]",
            "status_messages": [
                f"[截取] 文档无标题结构，截取前 {MAX_EXTRACT_CHARS} 字符（共 {len(requirement_text)} 字符）"
            ],
        }

    # 判断 section_range 类型并截取
    if not section_range:
        # 无指定：自动截取
        extracted = _extract_auto(requirement_text, outline)
        # 统计截取了哪些章节
        covered = [h for h in outline if h["start"] < len(extracted)]
        covered_titles = [f"{h['number']} {h['title']}" if h['number'] else h['title'] for h in covered[:5]]
        more = f" 等{len(covered)}个章节" if len(covered) > 5 else ""
        return {
            "extracted_text": extracted,
            "chapter_outline": chapter_outline,
            "status_messages": [
                f"[截取] 自动截取前 {len(extracted)} 字符（共 {len(requirement_text)} 字符），"
                f"覆盖: {', '.join(covered_titles)}{more}"
            ],
        }

    # 判断是数字编号还是语义关键词
    is_number_range = bool(re.match(r'^\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?$', section_range))

    if is_number_range:
        # 数字编号匹配
        matched = _match_section_by_number(outline, section_range)
        if matched:
            extracted = _extract_text_by_headings(requirement_text, matched)
            titles = [f"{h['number']} {h['title']}" for h in matched if h['number']][:5]
            return {
                "extracted_text": extracted,
                "chapter_outline": chapter_outline,
                "status_messages": [
                    f"[截取] 按编号 '{section_range}' 截取，{len(extracted)} 字符，"
                    f"匹配: {', '.join(titles)}"
                ],
            }
        else:
            # 编号匹配不到，fallback 到自动截取
            extracted = _extract_auto(requirement_text, outline)
            return {
                "extracted_text": extracted,
                "chapter_outline": chapter_outline,
                "status_messages": [
                    f"[截取] 编号 '{section_range}' 未匹配到章节，自动截取前 {len(extracted)} 字符"
                ],
            }
    else:
        # 语义关键词匹配
        matched = _match_section_by_keyword(outline, section_range)
        if matched:
            extracted = _extract_text_by_headings(requirement_text, matched)
            titles = [f"{h['number']} {h['title']}" for h in matched if h['number']][:5]
            return {
                "extracted_text": extracted,
                "chapter_outline": chapter_outline,
                "status_messages": [
                    f"[截取] 按关键词 '{section_range}' 截取，{len(extracted)} 字符，"
                    f"匹配: {', '.join(titles)}"
                ],
            }

        # 关键词匹配不到，AI 兜底（路由：pytest 下命中 mock，运行时直连）
        ai_result = _locate_section_by_ai_routed(outline, section_range)
        if ai_result:
            ai_matched = _match_section_by_number(outline, ai_result)
            if ai_matched:
                extracted = _extract_text_by_headings(requirement_text, ai_matched)
                titles = [f"{h['number']} {h['title']}" for h in ai_matched if h['number']][:5]
                return {
                    "extracted_text": extracted,
                    "chapter_outline": chapter_outline,
                    "status_messages": [
                        f"[截取] AI 定位 '{section_range}' → '{ai_result}'，{len(extracted)} 字符，"
                        f"匹配: {', '.join(titles)}"
                    ],
                }

        # AI 也定位不到，fallback 到自动截取
        extracted = _extract_auto(requirement_text, outline)
        return {
            "extracted_text": extracted,
            "chapter_outline": chapter_outline,
            "status_messages": [
                f"[截取] 关键词 '{section_range}' 未匹配到章节，自动截取前 {len(extracted)} 字符。"
                f"可指定章节编号重试（目录见日志）"
            ],
        }


# ============================================================
# Step 6.6: 节点 5 — 解析需求（LLM）
# ============================================================
def node_parse(state: WorkflowState) -> dict:
    """Phase 2: 用 LLM 解析需求文本

    将需求文本 + 解析规范一起发给 LLM，获取结构化解析结果。
    如果需求文本过长，截断到合理长度以避免 API 超时。
    """
    # 优先使用 extract 节点截取后的文本，fallback 到原始需求文本
    requirement_text = state.get("extracted_text", "") or state.get("requirement_text", "")
    if not requirement_text:
        return {
            "error": "没有需求文本可解析",
            "status_messages": ["[解析] 错误: 没有需求文本"],
        }

    # 安全截断：extract 截出的章节仍可能过大，避免 API 超时/断连
    truncation_notice = ""
    if len(requirement_text) > MAX_PARSE_CHARS:
        cutoff = requirement_text.rfind('\n', 0, MAX_PARSE_CHARS)
        if cutoff < MAX_PARSE_CHARS * 0.8:
            cutoff = MAX_PARSE_CHARS  # 找不到合适换行符时硬截
        original_len = len(requirement_text)
        requirement_text = requirement_text[:cutoff]
        truncation_notice = (
            f"\n\n[注意：原文档此章节共 {original_len} 字符，"
            f"因长度限制已截断至 {cutoff} 字符。"
            f"如需完整解析请指定更小的章节范围。]"
        )
        logger.warning(
            "Parse node: text still too large after extract (%d > %d), truncated to %d",
            original_len, MAX_PARSE_CHARS, cutoff,
        )

    # 读取记忆文件
    memory_context = ""
    memory_dir = BASE_DIR / ".memory"
    for fname in ["terminology.json", "ambiguity-decisions.json", "generation-history.json", "user-preferences.json"]:
        fpath = memory_dir / fname
        if fpath.exists():
            try:
                content = fpath.read_text(encoding="utf-8")
                memory_context += f"\n### {fname}\n{content}\n"
            except Exception:
                pass

    # 截断记忆上下文（避免 prompt 过大）
    if len(memory_context) > 3000:
        memory_context = memory_context[:3000] + "\n\n[... 记忆过长，已截断 ...]"

    prompt = build_parse_prompt(memory_context, requirement_text, truncation_notice)

    try:
        content = _safe_llm_invoke_routed(prompt, "parse")

        # 尝试提取 JSON
        parsed = _extract_json(content)
        if parsed is None:
            logger.warning("Parse node: LLM output is not valid JSON, storing raw text")
            parsed = content

        return {
            "parsed_result": parsed if isinstance(parsed, str) else json.dumps(parsed, ensure_ascii=False),
            "status_messages": ["[解析] 需求解析完成"],
        }
    except Exception as e:
        logger.exception("Parse node failed")
        return {
            "error": f"解析失败: {e}",
            "status_messages": [f"[解析] 错误: {e}"],
        }


# ============================================================
# Step 6.7: 节点 6 — 设计测试用例（LLM）
# ============================================================
def node_design(state: WorkflowState) -> dict:
    """Phase 3: 用 LLM 设计测试用例

    将解析结果 + 设计规范一起发给 LLM，生成测试用例。
    """
    parsed_result = state.get("parsed_result", "")
    if not parsed_result:
        return {
            "error": "没有解析结果可设计用例",
            "status_messages": ["[设计] 错误: 没有解析结果"],
        }

    # 如果 review 失败重试，附加 review issues
    review_feedback = ""
    review_result_str = state.get("review_result", "")
    retry_count = state.get("retry_count", 0)
    if retry_count > 0 and review_result_str:
        try:
            review_data = json.loads(review_result_str) if isinstance(review_result_str, str) else review_result_str
            if isinstance(review_data, dict) and review_data.get("issues"):
                issues_text = json.dumps(review_data["issues"], ensure_ascii=False, indent=2)
                review_feedback = f"""

## 审查反馈（第 {retry_count} 次修正）

上一次审查未通过，以下是需要修正的问题：

{issues_text}

请根据以上问题修正测试用例，重新输出完整的用例 JSON。
"""
        except (json.JSONDecodeError, TypeError):
            pass

    # 截断解析结果（避免 prompt 过大导致 API 断连）
    if len(parsed_result) > MAX_DESIGN_CHARS:
        parsed_result = parsed_result[:MAX_DESIGN_CHARS] + "\n\n[... 解析结果过长，已截断 ...]"
        logger.info("Design node: parsed_result truncated to %d chars", MAX_DESIGN_CHARS)

    prompt = build_design_prompt(parsed_result, review_feedback)

    try:
        content = _safe_llm_invoke_routed(prompt, "design")

        parsed = _extract_json(content)
        if parsed is None:
            parsed = content

        return {
            "test_cases": parsed if isinstance(parsed, str) else json.dumps(parsed, ensure_ascii=False),
            "status_messages": ["[设计] 测试用例设计完成"],
        }
    except Exception as e:
        logger.exception("Design node failed")
        return {
            "error": f"设计失败: {e}",
            "status_messages": [f"[设计] 错误: {e}"],
        }


# ============================================================
# Step 6.8: 节点 7 — 质量审查（LLM）
# ============================================================
def node_review(state: WorkflowState) -> dict:
    """Phase 4: 用 LLM 进行质量自检

    返回 passed/failed，如果 failed 则附带 issues 列表。
    """
    test_cases = state.get("test_cases", "")
    if not test_cases:
        return {
            "error": "没有用例可审查",
            "status_messages": ["[审查] 错误: 没有用例"],
        }

    # 截断过长用例（避免 prompt 过大）
    if len(test_cases) > MAX_REVIEW_CHARS:
        test_cases = test_cases[:MAX_REVIEW_CHARS] + "\n\n[... 用例过长，已截断 ...]"
        logger.info("Review node: test_cases truncated to %d chars", MAX_REVIEW_CHARS)

    prompt = build_review_prompt(test_cases)

    try:
        content = _safe_llm_invoke_routed(prompt, "review")

        parsed = _extract_json(content)
        if parsed is None:
            parsed = content

        result_str = parsed if isinstance(parsed, str) else json.dumps(parsed, ensure_ascii=False)

        # 判断审查结果
        status_msg = "[审查] 质量自检完成"
        try:
            result_data = json.loads(result_str) if isinstance(result_str, str) else result_str
            if isinstance(result_data, dict):
                if result_data.get("status") == "passed":
                    status_msg = "[审查] ✅ 质量自检通过"
                else:
                    issue_count = len(result_data.get("issues", []))
                    status_msg = f"[审查] ❌ 质量自检未通过，{issue_count} 个问题需修正"
        except (json.JSONDecodeError, TypeError):
            pass

        return {
            "review_result": result_str,
            "status_messages": [status_msg],
        }
    except Exception as e:
        logger.exception("Review node failed")
        return {
            "error": f"审查失败: {e}",
            "status_messages": [f"[审查] 错误: {e}"],
        }


# ============================================================
# Step 6.9: 节点 8 — 生成文件 + 更新记忆
# ============================================================
def node_generate(state: WorkflowState) -> dict:
    """Phase 5: 生成 XMind/XLSX 文件 + 更新记忆

    纯 Python 实现：保存 JSON、调用脚本、更新记忆。
    """
    test_cases = state.get("test_cases", "")
    if not test_cases:
        return {
            "error": "没有用例可生成文件",
            "status_messages": ["[生成] 错误: 没有用例"],
        }

    # 1. 确保 tmp/ 和 test-docs/ 目录存在
    (BASE_DIR / "tmp").mkdir(exist_ok=True)
    (BASE_DIR / "test-docs").mkdir(exist_ok=True)

    # 2. 获取时间戳
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

    # 3. 保存用例 JSON
    cases_file = BASE_DIR / "tmp" / f"cases_{timestamp}.json"
    try:
        # 尝试美化 JSON
        cases_data = json.loads(test_cases) if isinstance(test_cases, str) else test_cases
        if isinstance(cases_data, dict) and "cases" in cases_data:
            cases_list = cases_data["cases"]
        elif isinstance(cases_data, list):
            cases_list = cases_data
        else:
            cases_list = test_cases

        cases_file.write_text(
            json.dumps(cases_list, ensure_ascii=False, indent=2) if isinstance(cases_list, list) else str(cases_list),
            encoding="utf-8",
        )
    except (json.JSONDecodeError, TypeError):
        # cases 解析失败（如 parse 节点产出非法 JSON）：原样落盘，cases_list 标记为非列表
        cases_list = test_cases
        cases_file.write_text(str(test_cases), encoding="utf-8")

    # 4. 生成 XMind
    xmind_file = BASE_DIR / "test-docs" / f"testcases_{timestamp}.xmind"
    rc_xmind, stdout_xmind, stderr_xmind = _run_script(
        [sys.executable, "scripts/generate_xmind.py",
         "-f", str(cases_file), "-o", str(xmind_file)],
        timeout=30,
    )

    # 5. 生成 XLSX
    xlsx_file = BASE_DIR / "test-docs" / f"testcases_{timestamp}.xlsx"
    rc_xlsx, stdout_xlsx, stderr_xlsx = _run_script(
        [sys.executable, "scripts/generate_xlsx.py",
         "-f", str(cases_file), "-o", str(xlsx_file)],
        timeout=30,
    )

    # 6. 更新记忆
    record = {
        "timestamp": timestamp,
        "total_cases": len(cases_list) if isinstance(cases_list, list) else 0,
        "source_files": [],
    }
    record_json = json.dumps(record, ensure_ascii=False)
    _run_script(
        [sys.executable, "scripts/memory_manager.py",
         "--action", "add-record", "--project", ".", "--data", record_json],
        timeout=15,
    )

    # 构建输出结果
    output = {
        "json_file": str(cases_file),
        "xmind_file": str(xmind_file) if rc_xmind == 0 else None,
        "xlsx_file": str(xlsx_file) if rc_xlsx == 0 else None,
        "xmind_error": stderr_xmind if rc_xmind != 0 else None,
        "xlsx_error": stderr_xlsx if rc_xlsx != 0 else None,
    }

    # 构建摘要
    summary_parts = [f"[生成] 用例 JSON: {cases_file.name}"]
    if rc_xmind == 0:
        summary_parts.append(f"[生成] XMind: {xmind_file.name}")
    else:
        summary_parts.append(f"[生成] XMind 失败: {stderr_xmind[:100]}")
    if rc_xlsx == 0:
        summary_parts.append(f"[生成] XLSX: {xlsx_file.name}")
    else:
        summary_parts.append(f"[生成] XLSX 失败: {stderr_xlsx[:100]}")

    # 构建最终 AIMessage — CompiledSubAgent 框架从 messages 中提取最后一个
    # 有文本的 AIMessage 作为 ToolMessage 内容返回给主 Agent
    final_text = build_final_message(state)
    ai_message = AIMessage(content=final_text)

    return {
        "output_files": json.dumps(output, ensure_ascii=False),
        "status_messages": summary_parts,
        "messages": [ai_message],  # CompiledSubAgent 结果载体
    }


# ============================================================
# Step 7: JSON 提取辅助函数
# ============================================================
def _extract_json(text: str) -> dict | list | None:
    """从 LLM 输出中提取 JSON

    尝试：
    1. 直接解析整个文本
    2. 提取 ```json ... ``` 代码块
    3. 提取第一个 { ... } 或 [ ... ] 对象
    """
    if not text:
        return None

    # 1. 直接解析
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    # 2. 提取 ```json ... ``` 代码块
    match = re.search(r'```(?:json)?\s*\n?(.*?)```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            pass

    # 3. 提取第一个 { ... } 对象
    brace_count = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '{':
            if brace_count == 0:
                start = i
            brace_count += 1
        elif ch == '}':
            brace_count -= 1
            if brace_count == 0 and start >= 0:
                try:
                    return json.loads(text[start:i+1])
                except (json.JSONDecodeError, TypeError):
                    start = -1

    # 4. 提取第一个 [ ... ] 数组
    bracket_count = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '[':
            if bracket_count == 0:
                start = i
            bracket_count += 1
        elif ch == ']':
            bracket_count -= 1
            if bracket_count == 0 and start >= 0:
                try:
                    return json.loads(text[start:i+1])
                except (json.JSONDecodeError, TypeError):
                    start = -1

    return None


# ============================================================
# Step 9: 构建结果消息（给主 Agent 的返回值）
# ============================================================
def build_final_message(state: WorkflowState) -> str:
    """根据工作流状态构建最终返回给主 Agent 的消息"""
    status_msgs = state.get("status_messages", [])
    error = state.get("error", "")

    if error:
        return f"生成失败: {error}\n\n" + "\n".join(status_msgs)

    # 解析统计信息
    test_cases_str = state.get("test_cases", "")
    output_files_str = state.get("output_files", "")

    summary_parts = ["测试用例生成完成！\n"]

    # 用例统计
    try:
        cases_data = json.loads(test_cases_str) if isinstance(test_cases_str, str) else test_cases_str
        if isinstance(cases_data, dict) and "statistics" in cases_data:
            stats = cases_data["statistics"]
            total = stats.get("total_cases", 0)
            by_priority = stats.get("by_priority", {})
            coverage = stats.get("coverage", {})
            summary_parts.append(f"📊 总用例数: {total}")
            summary_parts.append(f"📊 优先级分布: P0={by_priority.get('P0', 0)}, P1={by_priority.get('P1', 0)}, P2={by_priority.get('P2', 0)}, P3={by_priority.get('P3', 0)}")
            summary_parts.append(f"📊 覆盖率: {coverage.get('coverage_rate', 'N/A')}")
    except (json.JSONDecodeError, TypeError):
        pass

    # 输出文件
    try:
        output_data = json.loads(output_files_str) if isinstance(output_files_str, str) else output_files_str
        if isinstance(output_data, dict):
            if output_data.get("json_file"):
                summary_parts.append(f"📄 JSON: {output_data['json_file']}")
            if output_data.get("xmind_file"):
                summary_parts.append(f"🧠 XMind: {output_data['xmind_file']}")
            if output_data.get("xlsx_file"):
                summary_parts.append(f"📊 XLSX: {output_data['xlsx_file']}")
    except (json.JSONDecodeError, TypeError):
        pass

    # 进度消息
    if status_msgs:
        summary_parts.append("\n执行进度:")
        for msg in status_msgs:
            summary_parts.append(f"  {msg}")

    return "\n".join(summary_parts)
