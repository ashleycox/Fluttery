"""Keep the online and offline editions in sync as topics and figures change."""

from pathlib import Path

import pytest
from PIL import Image as PILImage

from fluttery.manual_content import (
    CONTENTS,
    IMAGE,
    MANUAL_DIRECTORY,
    SHORTCUTS,
    SUPPORT,
    Manual,
    visible_lines,
)
from fluttery.settings_ui import SettingsPage

requires_manual = pytest.mark.skipif(not (MANUAL_DIRECTORY / CONTENTS).is_file(), reason='manual files are unavailable')


def test_manual_can_read_other_topics_when_a_resource_is_corrupt(tmp_path):
    (tmp_path / '01-damaged.md').write_bytes(b'\xff')
    (tmp_path / CONTENTS).write_text('# Contents\n\n## Reading\n\n| Windows |\n| --- |\n| Control+Q |\n\nHelp.',
                                     encoding='utf-8')
    manual = Manual(tmp_path, platform='macos')
    topic = manual.resolve(f'{CONTENTS}#reading')
    assert topic is not None and topic.article == 'Help.' and not topic.tables


@requires_manual
@pytest.mark.parametrize(('platform', 'modifier'), [('macos', 'Command'), ('windows', 'Control'), ('linux', 'Control')])
def test_manual_topics_search_and_platform_shortcuts(platform, modifier):
    manual = Manual(platform=platform)
    contents = manual.resolve(CONTENTS)
    assert contents is not None
    assert {name for _, name in contents.links} == {file.name for file in MANUAL_DIRECTORY.glob('*.md')} - {CONTENTS}
    assert manual.search('crosstalk')[0].anchor == 'crosstalk-and-azimuth'
    assert any(topic.address.startswith('91-settings-reference') for topic in manual.search('spectrogram colour'))
    assert not manual.search('qzxqzxp')
    shortcuts = manual.resolve(SHORTCUTS + '#open-tools-and-manage-windows')
    assert shortcuts.tables[0].columns == ('Command', 'Shortcut')
    instruments = (
        'Wow & Flutter', 'Oscilloscope', 'Turntable', 'Distortion Analyzer', 'Signal Generator',
        'Session Results', 'Settings',
    )
    assert shortcuts.tables[0].rows[:len(instruments)] == tuple(
        (label, f'{modifier}+{number}') for number, label in enumerate(instruments, start=1)
    )
    measurement = manual.resolve(SHORTCUTS + '#measure-and-read-current-values')
    toggle = 'Command+Shift+Space' if platform == 'macos' else 'Control+Space'
    assert ('Start or stop a wow and flutter measurement', toggle) in measurement.tables[0].rows
    assert ('Reset Counter in Wow & Flutter', f'{modifier}+Shift+T') in measurement.tables[0].rows
    assert ('Clear wow and flutter history', f'{modifier}+R') in measurement.tables[0].rows
    sessions = manual.resolve(SHORTCUTS + '#sessions-printing-and-text-editing')
    assert ('Clear Session…', f'{modifier}+Shift+E') in sessions.tables[0].rows
    wrong_modifier = 'Control' if platform == 'macos' else 'Command'
    assert all(f'{wrong_modifier}+' not in topic.article for topic in manual.topics)
    expected_sections = {'General', 'Audio I/O', 'Measurement', 'Calibration', 'Graphs', 'Reports', 'Diagnostics'}
    assert expected_sections <= {topic.title for topic in manual.topics if topic.file == '91-settings-reference.md'}
    assert len(expected_sections) == len(SettingsPage.SECTIONS)
    for topic in manual.topics:
        for _, target in topic.links:
            if not target.startswith('https://'):
                assert manual.resolve(target, topic.file) is not None, (topic.address, target)

    for _, file in contents.links:
        if file != SHORTCUTS:
            assert_chapter_shortcuts(manual, file)
    assert_chapter_shortcuts(manual, CONTENTS)


def assert_chapter_shortcuts(manual, file):
    topics = [topic for topic in manual.topics if topic.file == file and topic.title != SUPPORT]
    shortcuts = manual.resolve(f'{file}#keyboard-shortcuts')
    assert shortcuts is not None and topics[-1] == shortcuts
    table, = shortcuts.tables
    assert table.columns == ('Command', 'Shortcut') and table.rows
    assert all(table.title != 'Keyboard shortcuts' for topic in topics[:-1] for table in topic.tables)


@requires_manual
def test_support_fluttery_footer_in_each_chapter():
    manual = Manual()
    support, = (topic for topic in manual.topics if topic.title == SUPPORT)
    assert support.file == CONTENTS and len(support.links) == 1
    _, donation = support.links[0]
    block = (MANUAL_DIRECTORY / CONTENTS).read_text(encoding='utf-8').rsplit('## Support Fluttery\n', 1)[1].strip()
    assert 'paypal.com/donate/' in donation
    for chapter in MANUAL_DIRECTORY.glob('*.md'):
        body = chapter.read_text(encoding='utf-8').rstrip()
        assert body.endswith(f'## Support Fluttery\n\n{block}'), chapter.name


@requires_manual
@pytest.mark.parametrize('platform', ['macos', 'windows', 'linux'])
def test_manual_illustrations_have_working_relative_paths_and_text(platform):
    manual = Manual(platform=platform)
    images = set()
    for chapter in MANUAL_DIRECTORY.glob('*.md'):
        for line in visible_lines(chapter.read_text(encoding='utf-8'), platform):
            if (image := IMAGE.fullmatch(line)) is not None:
                path = MANUAL_DIRECTORY / image[2]
                assert path.is_file() and image[1], (chapter.name, line)
                assert Path(image[2]).parts[1] in (platform, 'shared')
                images.add(path)
    assert len(images) >= 20
    assert {figure.path for topic in manual.topics for figure in topic.illustrations} == images
    for path in images:
        with PILImage.open(path) as image:
            # The compact image options dialog uses its native content width.
            minimum_width = 320 if path.stem == 'plot-image-export' else 525
            assert image.format == 'PNG' and image.width >= minimum_width
            assert 'icc_profile' not in image.info  # Capture converts profiles before bundling.
            image.verify()
