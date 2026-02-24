"""Markdown memory block ejection."""

from pathlib import Path

MARKER_START = '<!-- mnemon:start -->'
MARKER_END = '<!-- mnemon:end -->'


def eject_memory_block(file_path: str) -> bool:
    """Remove everything between mnemon markers inclusive."""
    try:
        content = Path(file_path).read_text()
    except (OSError, FileNotFoundError):
        return False

    start_idx = content.find(MARKER_START)
    if start_idx < 0:
        return False
    end_idx = content.find(MARKER_END)
    if end_idx < 0:
        return False
    end_idx += len(MARKER_END)

    if start_idx > 0 and content[start_idx - 1] == '\n':
        start_idx -= 1
    if end_idx < len(content) and content[end_idx] == '\n':
        end_idx += 1

    result = content[:start_idx] + content[end_idx:]
    result = result.strip()

    if not result:
        try:
            Path(file_path).unlink()
        except FileNotFoundError:
            pass
        return True

    result += '\n'
    Path(file_path).write_text(result)
    return True
