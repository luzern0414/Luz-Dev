"""入库分块（SPEC §5.2）：300–500 token（按字符近似，默认上限 500 字）、重叠 50、标题路径作为前缀。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;\n])")


@dataclass
class Chunk:
    doc_id: str
    chunk_id: str
    title_path: str
    text: str
    doc_type: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    version: int = 1
    meta: dict = field(default_factory=dict)


def normalize_text(text: str) -> str:
    """去页眉页脚样式的空行与多余空白；表格行转为 "a | b" 文本。"""
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.replace("\r", "").split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln.startswith("|") and ln.endswith("|"):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            ln = " | ".join(c for c in cells if c)
        out.append(ln)
    return "\n".join(out).strip()


def chunk_text(text: str, max_chars: int = 500, overlap: int = 50, min_chars: int = 300) -> list[str]:
    """按句子累积到 [min_chars, max_chars]；下一块以上一块末尾 overlap 字开头。"""
    sentences = [s for s in _SENTENCE_RE.split(text) if s and s.strip()]
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        if len(buf) + len(s) > max_chars and buf:
            chunks.append(buf.strip())
            buf = buf[-overlap:] if overlap > 0 else ""
        while len(s) > max_chars:  # 超长句硬切
            head, s = s[:max_chars - len(buf)], s[max_chars - len(buf):]
            chunks.append((buf + head).strip())
            buf = (buf + head)[-overlap:] if overlap > 0 else ""
        buf += s
    if buf.strip():
        if chunks and len(buf.strip()) < min_chars // 3 and len(chunks[-1]) + len(buf) <= max_chars + overlap:
            chunks[-1] = (chunks[-1] + buf).strip()
        else:
            chunks.append(buf.strip())
    return chunks


def chunk_document(doc_id: str, title_path: str, text: str, *, doc_type: str | None = None,
                   valid_from: str | None = None, valid_to: str | None = None, version: int = 1,
                   max_chars: int = 500, overlap: int = 50) -> list[Chunk]:
    body = normalize_text(text)
    prefix = f"【{title_path}】" if title_path else ""
    pieces = chunk_text(body, max_chars=max_chars - len(prefix), overlap=overlap)
    return [Chunk(doc_id=doc_id, chunk_id=f"{doc_id}#{i}", title_path=title_path, text=prefix + p, doc_type=doc_type,
                  valid_from=valid_from, valid_to=valid_to, version=version) for i, p in enumerate(pieces)]
