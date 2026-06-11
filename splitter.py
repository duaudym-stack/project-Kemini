"""
모듈 ② Section Splitter
정규식 패턴으로 보고서를 섹션 단위로 분절합니다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Section:
    """분절된 섹션 하나를 나타냅니다."""
    section_id: str
    title: str
    depth: int
    text: str
    char_start: int
    char_end: int
    parent_path: str = ""

    @property
    def section_path(self) -> str:
        """부모 경로를 포함한 전체 섹션 경로"""
        if self.parent_path:
            return f"{self.parent_path} > {self.title}"
        return self.title


# §3 모듈 ② 에 정의된 패턴
SECTION_PATTERNS: list[tuple[str, int]] = [
    (r'^□\s*\(.+?\)', 1),          # □ (제목)
    (r'^\d+\.\s+', 1),             # 1. 제목
    (r'^[가나다라마바사아자차카타파하]\.\s+', 2),  # 가. 제목
    (r'^◦\s+', 3),                 # ◦ 항목
    (r'^☐\s+', 3),                 # ☐ 항목
    (r'^-\s+', 4),                 # - 항목
]

# 컴파일된 패턴
_COMPILED_PATTERNS = [(re.compile(pat, re.MULTILINE), depth) for pat, depth in SECTION_PATTERNS]


def _extract_title(line: str) -> str:
    """섹션 첫 줄에서 제목 추출 (패턴 접두사 제거)"""
    # 패턴 접두사 제거
    title = re.sub(r'^(□\s*\([^)]*\)\s*|'
                   r'\d+\.\s+|'
                   r'[가나다라마바사아자차카타파하]\.\s+|'
                   r'[◦☐]\s+|'
                   r'-\s+)', '', line).strip()
    # 마크다운 헤딩 접두사도 제거
    title = re.sub(r'^#{1,6}\s+', '', title).strip()
    return title or line.strip()


def split_sections(markdown: str) -> list[Section]:
    """
    정규화된 Markdown 문자열을 섹션 단위로 분절합니다.

    Args:
        markdown: 정규화된 Markdown 문자열

    Returns:
        Section 객체 리스트
    """
    if not markdown or not markdown.strip():
        return []

    lines = markdown.split('\n')
    sections: list[Section] = []
    section_id_counter = 0

    # 각 줄에서 섹션 시작점 탐지
    split_points: list[tuple[int, int, int]] = []  # (line_idx, depth, char_pos)

    # 마크다운 헤딩도 섹션 구분에 활용
    md_heading_re = re.compile(r'^(#{1,6})\s+(.+)')

    char_pos = 0
    for line_idx, line in enumerate(lines):
        stripped = line.strip()

        # 마크다운 헤딩 체크
        md_match = md_heading_re.match(stripped)
        if md_match:
            level = len(md_match.group(1))
            split_points.append((line_idx, min(level, 4), char_pos))
            char_pos += len(line) + 1  # +1 for \n
            continue

        # §3 패턴 체크
        matched = False
        for pattern, depth in _COMPILED_PATTERNS:
            if pattern.match(stripped):
                split_points.append((line_idx, depth, char_pos))
                matched = True
                break

        char_pos += len(line) + 1

    # 분할점이 없으면 전체를 하나의 섹션으로
    if not split_points:
        return [Section(
            section_id="S0",
            title="전체 문서",
            depth=0,
            text=markdown,
            char_start=0,
            char_end=len(markdown),
        )]

    # 분할점 기준으로 섹션 생성
    parent_stack: list[str] = []  # 상위 섹션 제목 스택

    for i, (line_idx, depth, char_start) in enumerate(split_points):
        # 다음 분할점까지의 텍스트 범위
        if i + 1 < len(split_points):
            next_line_idx = split_points[i + 1][0]
            char_end = split_points[i + 1][2]
        else:
            next_line_idx = len(lines)
            char_end = len(markdown)

        section_lines = lines[line_idx:next_line_idx]
        section_text = '\n'.join(section_lines).strip()
        first_line = lines[line_idx].strip()
        title = _extract_title(first_line)

        # 부모 경로 구축
        while len(parent_stack) >= depth:
            if parent_stack:
                parent_stack.pop()
            else:
                break

        parent_path = ' > '.join(parent_stack)

        section = Section(
            section_id=f"S{section_id_counter}",
            title=title,
            depth=depth,
            text=section_text,
            char_start=char_start,
            char_end=char_end,
            parent_path=parent_path,
        )
        sections.append(section)
        section_id_counter += 1

        # 현재 섹션을 부모 스택에 추가
        if depth <= len(parent_stack):
            parent_stack = parent_stack[:depth - 1] + [title] if depth > 0 else [title]
        else:
            parent_stack.append(title)

    return sections
