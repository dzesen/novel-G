"""Describe imported narrative material without running its template engine.

The source remains owned by the interop adapters. This module produces only a
novel-writing projection and an explicit accounting of conversions/exclusions.
It never evaluates a condition or chooses a runtime branch.
"""

from __future__ import annotations

import ast
import html
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Literal

import yaml

ADAPTATION_VERSION = 1
MAX_TEMPLATE_BLOCKS = 2_000
MAX_EXPRESSION_CHARS = 2_000
MAX_EXPRESSION_NODES = 128
MAX_DEPTH = 24
_STRING = r"(?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")"
_TEMPLATE = re.compile(r"<%([\s\S]*?)%>")
_MACRO = re.compile(r"\{\{([^{}]*)\}\}")
_LABELS = {
    "name": "姓名", "age": "年龄", "gender": "性别", "identity": "身份",
    "occupation": "职业", "appearance": "外貌", "personality": "性格",
    "description": "描述", "background": "背景", "relationships": "关系",
    "abilities": "能力", "goals": "目标", "location": "位置", "history": "历史",
    "rules": "规则", "conditions": "条件", "content": "内容", "traits": "特征",
    "likes": "喜好", "dislikes": "厌恶", "motivation": "动机",
}


@dataclass(frozen=True)
class AdaptedContent:
    text: str
    status: Literal["ready", "review_required", "reference_only"]
    counts: dict[str, int]
    notices: tuple[str, ...]

    def summary(self) -> dict:
        return {"version": ADAPTATION_VERSION, "status": self.status,
                "counts": self.counts, "notices": list(self.notices)}


class _UnsupportedTemplate(ValueError):
    pass


@dataclass
class _Report:
    counts: Counter = field(default_factory=Counter)
    notices: list[str] = field(default_factory=list)
    review: bool = False

    def note(self, code: str, *, review: bool = False) -> None:
        if code not in self.notices:
            self.notices.append(code)
        self.review |= review


@dataclass
class _Branches:
    clauses: list[tuple[str | None, list]] = field(default_factory=list)


def _literal(value: str):
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError, MemoryError, RecursionError) as exc:
        raise _UnsupportedTemplate("invalid_literal") from exc


def _state_label(path: str) -> str:
    path = re.sub(r"^(?:stat_data|state|variables)\.", "", path)
    label = re.sub(r"[.\[\]]+", "／", path).strip("／")
    if not label or len(label) > 160 or any(x in label for x in ("<", ">", "{", "}", "\n")):
        raise _UnsupportedTemplate("unsupported_variable")
    return f"「{label}」"


def _expression(source: str, bindings: dict[str, str], report: _Report) -> str:
    if len(source) > MAX_EXPRESSION_CHARS:
        raise _UnsupportedTemplate("template_complexity")
    token_re = re.compile(_STRING + r"|===|!==|&&|\|\||!=|!|[A-Za-z_$][\w$]*|\s+|.")
    tokens = []
    replacements = {"===": "==", "!==": "!=", "&&": " and ", "||": " or ",
                    "!": " not ", "true": "True", "false": "False", "null": "None"}
    for match in token_re.finditer(source):
        token = match.group()
        tokens.append(replacements.get(token, token))
    try:
        tree = ast.parse("".join(tokens).strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise _UnsupportedTemplate("unsupported_condition") from exc
    if sum(1 for _ in ast.walk(tree)) > MAX_EXPRESSION_NODES:
        raise _UnsupportedTemplate("template_complexity")

    def describe(node: ast.AST, depth: int = 0) -> str:
        if depth > MAX_DEPTH:
            raise _UnsupportedTemplate("template_complexity")
        child = lambda value: describe(value, depth + 1)
        if isinstance(node, ast.Name):
            if node.id in bindings:
                return bindings[node.id]
            report.note("unresolved_state_variable", review=True)
            return _state_label(node.id)
        if isinstance(node, ast.Constant):
            if node.value is True:
                return "是"
            if node.value is False:
                return "否"
            if node.value is None:
                return "空值"
            if isinstance(node.value, (int, float)):
                return str(node.value)
            if isinstance(node.value, str) and len(node.value) <= 160:
                return f"「{node.value}」"
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return f"不满足（{child(node.operand)}）"
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant) and type(node.operand.value) in {int, float}:
            return str(-node.operand.value)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            return ("且" if isinstance(node.op, ast.And) else "或").join(
                f"（{child(value)}）" for value in node.values
            )
        if isinstance(node, ast.Compare):
            labels = {ast.Eq: "为", ast.NotEq: "不为", ast.Gt: "大于", ast.GtE: "不少于",
                      ast.Lt: "小于", ast.LtE: "不多于"}
            left = child(node.left)
            parts = []
            for op, right_node in zip(node.ops, node.comparators, strict=True):
                if type(op) not in labels:
                    raise _UnsupportedTemplate("unsupported_condition")
                right = child(right_node)
                parts.append(f"{left}{labels[type(op)]}{right}")
                left = right
            return "且".join(parts)
        if isinstance(node, ast.Call) and not node.keywords:
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "Math" and func.attr in {"min", "max"} and 1 <= len(node.args) <= 8:
                label = "较小值" if func.attr == "min" else "较大值"
                return f"（{'、'.join(child(arg) for arg in node.args)}的{label}）"
        raise _UnsupportedTemplate("unsupported_condition")

    return describe(tree.body)


def _statements(code: str) -> list[str]:
    # Split only outside strings; this is lexical parsing, never eval/exec.
    result, start = [], 0
    for match in re.finditer(_STRING + r"|;", code):
        if match.group() == ";":
            result.append(code[start:match.start()].strip())
            start = match.end()
    if code[start:].strip():
        result.append(code[start:].strip())
    return [statement for statement in result if statement]


def _declarations(code: str, bindings: dict[str, str], report: _Report) -> None:
    for statement in _statements(code):
        guard = re.match(r"if\s*\(typeof\s+([A-Za-z_]\w*)\s*===?\s*(['\"])undefined\2\)\s*", statement)
        if guard:
            statement = statement[guard.end():]
        assignment = re.fullmatch(r"(?:var|let|const)\s+([A-Za-z_]\w*)\s*=\s*([\s\S]+)", statement)
        if not assignment or (guard and assignment[1] != guard[1]):
            raise _UnsupportedTemplate("unsupported_template_code")
        name, value = assignment.groups()
        source = re.fullmatch(
            r"getvar\(\s*(" + _STRING + r")\s*(?:,\s*\{\s*defaults\s*:\s*(?:-?\d+(?:\.\d+)?|true|false|null|" + _STRING + r")\s*\})?\s*\)", value,
        )
        if source:
            path = _literal(source[1])
            if not isinstance(path, str):
                raise _UnsupportedTemplate("unsupported_variable")
            bindings[name] = _state_label(path)
        else:
            # A declaration is a symbolic state binding, never a local value
            # whose initialization can be promoted to a narrative fact.
            if not re.fullmatch(r"Math\.(?:min|max)\([\s\S]+\)", value):
                raise _UnsupportedTemplate("unsupported_template_code")
            bindings[name] = _expression(value, bindings, report)
        report.counts["variable_bindings"] += 1


def _adapt_templates(text: str, report: _Report) -> str:
    if "<%" not in text and "%>" not in text:
        return text
    matches = list(_TEMPLATE.finditer(text))
    if len(matches) > MAX_TEMPLATE_BLOCKS:
        raise _UnsupportedTemplate("template_complexity")
    roots: list = []
    current = roots
    stack: list[tuple[list, _Branches]] = []
    bindings: dict[str, str] = {}
    previous = 0
    for match in matches:
        literal = text[previous:match.start()]
        if "<%" in literal or "%>" in literal:
            raise _UnsupportedTemplate("damaged_template")
        current.append(literal)
        code = match[1].strip().strip("_-").strip()
        previous = match.end()
        if not code or code.startswith("#"):
            continue
        if code.startswith(("=", "-")):
            report.note("dynamic_output", review=True)
            report.counts["excluded_fragments"] += 1
            continue
        opening = re.fullmatch(r"if\s*\(([\s\S]*)\)\s*\{", code)
        alternate = re.fullmatch(r"}\s*else\s+if\s*\(([\s\S]*)\)\s*\{", code)
        otherwise = re.fullmatch(r"}\s*else\s*\{", code)
        if opening:
            if len(stack) >= MAX_DEPTH:
                raise _UnsupportedTemplate("template_complexity")
            branch = _Branches([(_expression(opening[1], bindings, report), [])])
            current.append(branch)
            stack.append((current, branch))
            current = branch.clauses[-1][1]
        elif alternate or otherwise:
            if not stack or stack[-1][1].clauses[-1][0] is None:
                raise _UnsupportedTemplate("damaged_template")
            branch = stack[-1][1]
            condition = _expression(alternate[1], bindings, report) if alternate else None
            branch.clauses.append((condition, []))
            current = branch.clauses[-1][1]
        elif code == "}":
            if not stack:
                raise _UnsupportedTemplate("damaged_template")
            current, _ = stack.pop()
        else:
            if stack:
                raise _UnsupportedTemplate("unsupported_template_code")
            _declarations(code, bindings, report)
    tail = text[previous:]
    if stack or "<%" in tail or "%>" in tail:
        raise _UnsupportedTemplate("damaged_template")
    current.append(tail)

    def render(nodes: list, conditions: tuple[str, ...] = ()) -> str:
        parts = []
        for node in nodes:
            if isinstance(node, str):
                if node.strip():
                    prefix = f"【适用条件：{'；且'.join(conditions)}】\n" if conditions else ""
                    parts.append(prefix + node.strip())
            else:
                earlier = []
                for condition, body in node.clauses:
                    own = [*(f"不满足（{item}）" for item in earlier)]
                    if condition is not None:
                        own.append(condition)
                    report.counts["conditional_branches"] += 1
                    rendered = render(body, (*conditions, *own))
                    if rendered:
                        parts.append(rendered)
                    if condition is not None:
                        earlier.append(condition)
        return "\n\n".join(parts)

    report.note("conditions_preserved")
    return render(roots)


class _VisibleText(HTMLParser):
    def __init__(self, report: _Report):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []
        self.report = report

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "iframe", "object", "system"}:
            self.hidden.append(tag)
            self.report.counts["code_blocks"] += 1
            self.report.note("runtime_code_isolated")
        elif not self.hidden and tag in {"br", "p", "div", "li", "tr", "td", "th", "section", "details", "summary", "h1", "h2", "h3"}:
            self.parts.append("\n")
        elif not self.hidden and tag == "img":
            self.parts.append(dict(attrs).get("alt") or "")
        self.report.counts["presentation_tags"] += 1

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in {"p", "div", "li", "tr", "td", "th", "section", "details", "summary", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def handle_comment(self, data):
        self.report.counts["code_blocks"] += 1
        self.report.note("runtime_code_isolated")


def _structured_text(text: str) -> str | None:
    if len(text) > 100_000:
        return None
    try:
        data = json.loads(text) if text.lstrip().startswith(("{", "[")) else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError, RecursionError):
        return None
    if not isinstance(data, (dict, list)) or not data:
        return None
    seen: set[int] = set()
    counter = 0

    def render(value, depth=0) -> list[str]:
        nonlocal counter
        counter += 1
        if depth > MAX_DEPTH or counter > 2_000:
            raise ValueError("structured_content_limit")
        if isinstance(value, (dict, list)):
            if id(value) in seen:
                raise ValueError("structured_content_alias")
            seen.add(id(value))
        if isinstance(value, dict):
            lines = []
            for key, item in value.items():
                if not isinstance(key, str) or key in {"$schema", "$defs", "properties"}:
                    raise ValueError("runtime_schema")
                label = _LABELS.get(key, key)
                if isinstance(item, list) and all(isinstance(x, (str, int, float, bool)) for x in item):
                    lines.append(f"{label}：{'、'.join(str(x) for x in item)}")
                elif isinstance(item, (dict, list)):
                    lines.append(f"{label}：")
                    lines.extend("  " + line for line in render(item, depth + 1))
                else:
                    scalar = "是" if item is True else "否" if item is False else "未设置" if item is None else str(item)
                    lines.append(f"{label}：{scalar}")
            return lines
        if isinstance(value, list):
            return [line for item in value for line in render(item, depth + 1)]
        return [str(value)]

    try:
        return "\n".join(render(data))
    except (ValueError, RecursionError):
        return None


def adapt_content(text: str, *, character_name: str | None = None) -> AdaptedContent:
    """Produce readable, non-executable material, retaining uncertainty visibly."""
    report = _Report()
    text = html.unescape(text)
    if re.search(r"</?[A-Za-z][^>]*>", text):
        parser = _VisibleText(report)
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
        report.note("presentation_removed")

    def fence(match):
        language, body = match.groups()
        structured = _structured_text(body) if language.lower() in {"", "json", "yaml", "yml"} else None
        if structured is not None:
            report.counts["structured_blocks"] += 1
            report.note("structured_data_readable")
            return structured
        report.counts["code_blocks"] += 1
        report.note("runtime_code_isolated")
        return ""

    text = re.sub(r"```([A-Za-z0-9_-]*)[^\S\n]*\n?([\s\S]*?)```", fence, text)
    if "```" in text:
        text = text.split("```", 1)[0]
        report.note("damaged_code_block", review=True)
        report.counts["excluded_fragments"] += 1
    try:
        text = _adapt_templates(text, report)
    except _UnsupportedTemplate as exc:
        report.note(str(exc), review=True)
        return AdaptedContent("", "reference_only", dict(report.counts), tuple(report.notices))

    lines = []
    for line in text.splitlines():
        unknown = False

        def macro(match):
            nonlocal unknown
            name = match[1].strip().lower()
            if name in {"user", "char"}:
                report.counts["role_macros"] += 1
                report.note("narrative_roles")
                return "主角" if name == "user" else character_name or "该角色"
            unknown = True
            return ""

        converted = _MACRO.sub(macro, line)
        if unknown or "{{" in converted or "}}" in converted:
            report.note("unknown_macro", review=True)
            report.counts["excluded_fragments"] += 1
            continue
        if re.match(r"^\s*(?:(?:const|let|var)\s+\w+\s*=|(?:import|export)\s|function\s*\w*\s*\(|(?:document|window|console)\.)", converted):
            report.note("runtime_code_isolated", review=True)
            report.counts["code_blocks"] += 1
            # Without a fence we cannot prove where the code body ends.
            return AdaptedContent("", "reference_only", dict(+report.counts), tuple(report.notices))
        lines.append(converted)
    text = "\n".join(lines).strip()
    if report.counts["presentation_tags"]:
        text = re.sub(r"\n{3,}", "\n\n", text)
    if not report.counts["conditional_branches"] and (text.startswith(("{", "[")) or len(re.findall(r"^\s*[\w\u4e00-\u9fff]+:\s", text, re.M)) >= 2):
        structured = _structured_text(text)
        if structured is not None:
            text = structured
            report.counts["structured_blocks"] += 1
            report.note("structured_data_readable")
    status = "review_required" if report.review else "ready"
    if report.notices and not re.search(r"[^\W_]", text, re.UNICODE):
        text = ""
        status = "reference_only"
    return AdaptedContent(text, status, dict(+report.counts), tuple(report.notices))
