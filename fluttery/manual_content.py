"""Read the Markdown topics shared by GitHub and Fluttery's offline Help."""

from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

MANUAL_DIRECTORY = Path(__file__).with_name('manual')
CONTENTS = 'README.md'
SHORTCUTS = '90-keyboard-shortcuts.md'
SETTINGS = '91-settings-reference.md'
SUPPORT = 'Support Fluttery'
PLATFORMS = {'darwin': 'macos', 'win32': 'windows', 'linux': 'linux'}
PLATFORM_COLUMNS = {'macOS': {'macos'}, 'Windows and Linux': {'windows', 'linux'},
                    'Windows': {'windows'}, 'Linux': {'linux'}}
HEADING = re.compile(r'^(#{1,6})\s+(.+)$')
PLATFORM_START = re.compile(r'^<!-- platform: ([a-z, ]+) -->$')
LINK = re.compile(r'!?\[([^\]]+)\]\(([^)\s]+)\)')
IMAGE = re.compile(r'^!\[([^\]]+)\]\(([^)\s]+)\)$')
WORDS = re.compile(r'[^\W_]+', re.UNICODE)


def plain_text(markdown):
    # The manual uses headings, paragraphs, lists, tables and inline emphasis.
    # Keep literal underscores and punctuation in equipment and file names.
    text = LINK.sub(r'\1', markdown).replace('**', '').replace('`', '')
    return re.sub(r'(?<!\w)\*([^*\n]+)\*(?!\w)', r'\1', text)


def slug(title):
    return re.sub(r'[^\w-]', '', plain_text(title).lower().replace(' ', '-'))


def search_text(text):
    normalized = unicodedata.normalize('NFKD', text.casefold())
    return ' '.join(WORDS.findall(''.join(char for char in normalized if not unicodedata.combining(char))))


def visible_lines(text, platform):
    enabled = True
    for raw in text.splitlines():
        line = raw.strip()
        if (scope := PLATFORM_START.fullmatch(line)) is not None:
            enabled = platform in (name.strip() for name in scope[1].split(','))
        elif line == '<!-- endplatform -->':
            enabled = True
        elif enabled and not line.startswith(('<!--', '<details>', '</details>', '<summary>')):
            yield raw.rstrip()


def table_cells(line):
    return tuple(plain_text(cell.strip().replace('\\|', '|'))
                 for cell in re.split(r'(?<!\\)\|', line.strip().strip('|')))


@dataclass(frozen=True, slots=True)
class ReferenceTable:
    title: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]

    def reading_text(self):
        if len(self.columns) == 2:
            return '\n'.join(f'{key}  —  {value}' for key, value in self.rows)
        return '\n\n'.join('\n'.join((row[0], *(f'{column}  —  {cell}' for column, cell in
                                                  zip(self.columns[1:], row[1:], strict=True)))) for row in self.rows)


@dataclass(frozen=True, slots=True)
class Illustration:
    description: str
    path: Path


@dataclass(frozen=True, slots=True)
class Topic:
    file: str
    anchor: str
    chapter: str
    title: str
    article: str
    tables: tuple[ReferenceTable, ...]
    illustrations: tuple[Illustration, ...]
    links: tuple[tuple[str, str], ...]

    @property
    def name(self):
        return f'{self.chapter} › {self.title}' if self.anchor else self.chapter

    @property
    def address(self):
        return f'{self.file}#{self.anchor}' if self.anchor else self.file


def _section(file, chapter, title, anchor, lines, directory, platform):
    paragraphs, tables, illustrations, links = [], [], [], []
    index, table_title = 0, title
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if (image := IMAGE.fullmatch(line)) is not None:
            path = (directory / image[2]).resolve()
            # Keep locally authored image and navigation paths inside the book.
            if path.is_relative_to(directory) and path.is_file():
                illustrations.append(Illustration(image[1], path))
                paragraphs.append(f'Illustration — {image[1]}')
            continue
        if line.startswith('|') and index < len(lines) and re.fullmatch(r'[|:\-\s]+', lines[index]):
            headers = table_cells(line)
            selected = tuple(i for i, name in enumerate(headers)
                             if name not in PLATFORM_COLUMNS or platform in PLATFORM_COLUMNS[name])
            shown = tuple('Shortcut' if headers[i] in PLATFORM_COLUMNS else headers[i] for i in selected)
            rows = []
            index += 1
            while index < len(lines) and lines[index].lstrip().startswith('|'):
                columns = table_cells(lines[index])
                if len(columns) == len(headers):
                    rows.append(tuple(columns[i] for i in selected))
                index += 1
            if shown:
                table = ReferenceTable(table_title, shown, tuple(rows))
                tables.append(table)
                paragraphs.append(table.reading_text())
            continue
        if (heading := HEADING.fullmatch(line)) is not None:
            line = table_title = plain_text(heading[2])
        else:
            links.extend((plain_text(match[1]), match[2]) for match in LINK.finditer(line))
            line = plain_text(line)
            if line.startswith('- '):
                line = '• ' + line[2:]
        paragraphs.append(line)
    return Topic(file, anchor, chapter, title, '\n'.join(paragraphs).strip(), tuple(tables),
                 tuple(illustrations), tuple(dict.fromkeys(links)))


class Manual:
    def __init__(self, directory=MANUAL_DIRECTORY, platform=None):
        directory = directory.resolve()
        platform = platform or PLATFORMS.get(sys.platform, 'linux')
        files = sorted(directory.glob('*.md'), key=lambda path: (path.name != CONTENTS, path.name))
        topics = []
        for path in files:
            try:
                lines = list(visible_lines(path.read_text(encoding='utf-8'), platform))
            except (OSError, UnicodeError):
                # One damaged or missing resource should leave the rest of Help usable.
                continue
            if not lines or not lines[0].startswith('# '):
                continue
            chapter = plain_text(lines[0][2:])
            starts = [i for i, line in enumerate(lines) if line.startswith('## ')]
            overview = lines[1:starts[0]] if starts else lines[1:]
            topics.append(_section(path.name, chapter, chapter, '', overview, directory, platform))
            for start, end in zip(starts, [*starts[1:], len(lines)]):
                title = plain_text(lines[start][3:])
                # The online chapters repeat the support footer. One searchable
                # support topic keeps the bundled navigation free of duplicate entries.
                if title == SUPPORT and path.name != CONTENTS:
                    continue
                topics.append(_section(path.name, chapter, title, slug(title), lines[start + 1:end], directory, platform))
        self.topics = tuple(topics)
        self._addresses = {topic.address: topic for topic in topics}
        self._search_index = tuple((topic, search_text(topic.name), search_text(topic.article)) for topic in topics)

    def resolve(self, target, current=CONTENTS):
        file, marker, anchor = target.partition('#')
        return self._addresses.get((file or current) + (marker + anchor if marker and anchor else ''))

    def search(self, query):
        terms = search_text(query).split()
        if not terms:
            return self.topics
        matches = []
        for topic, title, article in self._search_index:
            if all(term in title or term in article for term in terms):
                # Titles take precedence. Preserve the printed order for ties.
                matches.append((sum(term in title for term in terms), topic))
        return tuple(topic for _, topic in sorted(matches, key=lambda item: -item[0]))
