"""TUI prompts: single-select, multi-select, confirm via termios/tty."""

import os
import sys

COLOR_GREEN = '\033[32m'
COLOR_DIM = '\033[2m'
COLOR_RED = '\033[31m'
COLOR_BOLD = '\033[1m'
COLOR_RESET = '\033[0m'
SYM_OK = '\u2713'
SYM_FAIL = '\u2717'
SYM_DOT = '\u00b7'

_colors_inited = False


def _init_colors() -> None:
    """Clear ANSI codes when stdout is not a TTY."""
    global COLOR_GREEN, COLOR_DIM, COLOR_RED, COLOR_BOLD, COLOR_RESET, _colors_inited
    if _colors_inited:
        return
    _colors_inited = True
    if not sys.stdout.isatty():
        COLOR_GREEN = ''
        COLOR_DIM = ''
        COLOR_RED = ''
        COLOR_BOLD = ''
        COLOR_RESET = ''


def is_interactive() -> bool:
    """Return True if stdin is a TTY."""
    return sys.stdin.isatty()


def confirm(prompt: str, default_yes: bool = False) -> bool:
    """Prompt the user for a yes/no answer."""
    _init_colors()
    hint = 'Y/n' if default_yes else 'y/N'
    sys.stdout.write(
        f'{prompt} {COLOR_DIM}[{hint}]{COLOR_RESET} \u203a ')
    sys.stdout.flush()
    try:
        line = input()
    except EOFError:
        return default_yes
    answer = line.strip().lower()
    if not answer:
        return default_yes
    return answer in {'y', 'yes'}


def select_one(prompt: str, options: list[str],
               default_idx: int = 0) -> int:
    """Show a single-select prompt with arrow key navigation."""
    _init_colors()
    if not is_interactive() or not options:
        return default_idx

    try:
        import termios
        import tty
    except ImportError:
        return default_idx

    fd = sys.stdin.fileno()
    try:
        old_settings = termios.tcgetattr(fd)
    except termios.error:
        return default_idx

    cursor = default_idx
    if cursor < 0 or cursor >= len(options):
        cursor = 0

    render_lines = len(options) + 1

    def render(first: bool) -> None:
        if not first:
            sys.stdout.write(f'\033[{render_lines}A')
        sys.stdout.write(
            f'\033[2K  {COLOR_BOLD}{prompt}{COLOR_RESET}'
            f' {COLOR_DIM}(\u2191\u2193 move, enter confirm)'
            f'{COLOR_RESET}\r\n')
        for i, opt in enumerate(options):
            sys.stdout.write('\033[2K')
            if i == cursor:
                sys.stdout.write(
                    f'  {COLOR_GREEN}\u203a{COLOR_RESET} {opt}\r\n')
            else:
                sys.stdout.write(
                    f'    {COLOR_DIM}{opt}{COLOR_RESET}\r\n')
        sys.stdout.flush()

    try:
        tty.setraw(fd)
        render(True)

        while True:
            ch = os.read(fd, 3)
            if not ch:
                break

            if ch[0] in {13, 10}:
                sys.stdout.write(f'\033[{render_lines}A')
                for _ in range(render_lines):
                    sys.stdout.write('\033[2K\r\n')
                sys.stdout.write(f'\033[{render_lines}A')
                sys.stdout.write(
                    f'  {COLOR_GREEN}{SYM_OK}{COLOR_RESET}'
                    f' {prompt}: {options[cursor]}\r\n')
                for _ in range(1, render_lines):
                    sys.stdout.write('\033[2K\r\n')
                sys.stdout.flush()
                return cursor

            if ch[0] == 0x1b and len(ch) == 1:
                return default_idx
            if ch[0] == ord('q'):
                return default_idx
            if ch[0] == 3:
                return default_idx

            if (len(ch) >= 3 and ch[0] == 0x1b
                    and ch[1] == ord('[')):
                if ch[2] == ord('A') and cursor > 0:
                    cursor -= 1
                    render(False)
                elif (ch[2] == ord('B')
                      and cursor < len(options) - 1):
                    cursor += 1
                    render(False)

            elif ch[0] == ord('k') and cursor > 0:
                cursor -= 1
                render(False)
            elif (ch[0] == ord('j')
                  and cursor < len(options) - 1):
                cursor += 1
                render(False)

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    return cursor


def select_multi(title: str, options: list[str],
                 defaults: list[bool]) -> list[bool]:
    """Show a multi-select prompt with arrow key navigation."""
    _init_colors()
    if not is_interactive():
        return _select_multi_fallback(title, options, defaults)

    try:
        import termios
        import tty
    except ImportError:
        return _select_multi_fallback(title, options, defaults)

    fd = sys.stdin.fileno()
    try:
        old_settings = termios.tcgetattr(fd)
    except termios.error:
        return _select_multi_fallback(title, options, defaults)

    selected = list(defaults)
    cursor = 0
    render_lines = len(options) + 2

    def render(first: bool) -> None:
        if not first:
            sys.stdout.write(f'\033[{render_lines}A')
        sys.stdout.write(
            f'\033[2K  {COLOR_BOLD}{title}{COLOR_RESET}'
            f' {COLOR_DIM}(\u2191\u2193 move, space toggle,'
            f' enter confirm){COLOR_RESET}\r\n')
        for i, opt in enumerate(options):
            sys.stdout.write('\033[2K')
            pointer = '    '
            if i == cursor:
                pointer = f'  {COLOR_GREEN}\u203a{COLOR_RESET} '
            if selected[i]:
                sys.stdout.write(
                    f'{pointer}{COLOR_GREEN}[x]{COLOR_RESET}'
                    f' {opt}\r\n')
            else:
                sys.stdout.write(
                    f'{pointer}{COLOR_DIM}[ ]{COLOR_RESET}'
                    f' {COLOR_DIM}{opt}{COLOR_RESET}\r\n')
        sys.stdout.write('\033[2K')
        count = sum(selected)
        if count > 0:
            sys.stdout.write(
                f'  {COLOR_DIM}{count} selected{COLOR_RESET}\r\n')
        else:
            sys.stdout.write(
                f'  {COLOR_DIM}None selected{COLOR_RESET}\r\n')
        sys.stdout.flush()

    try:
        tty.setraw(fd)
        render(True)

        while True:
            ch = os.read(fd, 3)
            if not ch:
                break

            if ch[0] in {13, 10}:
                sys.stdout.write(f'\033[{render_lines}A')
                for _ in range(render_lines):
                    sys.stdout.write('\033[2K\r\n')
                sys.stdout.write(f'\033[{render_lines}A')
                names = [
                    opt for opt, sel in zip(options, selected)
                    if sel]
                if names:
                    sys.stdout.write(
                        f'  {COLOR_GREEN}{SYM_OK}{COLOR_RESET}'
                        f' Selected: {", ".join(names)}\r\n')
                else:
                    sys.stdout.write(
                        f'  {COLOR_DIM}{SYM_DOT}'
                        f' None selected{COLOR_RESET}\r\n')
                for _ in range(1, render_lines):
                    sys.stdout.write('\033[2K\r\n')
                sys.stdout.flush()
                return selected

            if ch[0] == 0x1b and len(ch) == 1:
                return list(defaults)
            if ch[0] == ord('q'):
                return list(defaults)
            if ch[0] == 3:
                return list(defaults)

            if ch[0] == ord(' '):
                selected[cursor] = not selected[cursor]
                render(False)

            elif (len(ch) >= 3 and ch[0] == 0x1b
                  and ch[1] == ord('[')):
                if ch[2] == ord('A') and cursor > 0:
                    cursor -= 1
                    render(False)
                elif (ch[2] == ord('B')
                      and cursor < len(options) - 1):
                    cursor += 1
                    render(False)

            elif ch[0] == ord('k') and cursor > 0:
                cursor -= 1
                render(False)
            elif (ch[0] == ord('j')
                  and cursor < len(options) - 1):
                cursor += 1
                render(False)

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    return selected


def _select_multi_fallback(title: str, options: list[str],
                           defaults: list[bool]) -> list[bool]:
    """Non-interactive number-input fallback for multi-select."""
    selected = list(defaults)

    while True:
        print(f'\n{COLOR_BOLD}{title}{COLOR_RESET}'
              f' {COLOR_DIM}(toggle: 1-{len(options)},'
              f' confirm: Enter){COLOR_RESET}')
        for i, opt in enumerate(options):
            if selected[i]:
                print(f'  {i+1}. {COLOR_GREEN}[x]{COLOR_RESET}'
                      f' {opt}')
            else:
                print(f'  {i+1}. {COLOR_DIM}[ ]{COLOR_RESET}'
                      f' {COLOR_DIM}{opt}{COLOR_RESET}')
        sys.stdout.write('\u203a ')
        sys.stdout.flush()
        try:
            line = input()
        except EOFError:
            break
        text = line.strip()
        if not text:
            break
        try:
            num = int(text)
        except ValueError:
            print(f'  {COLOR_RED}{SYM_FAIL} invalid:'
                  f' {text}{COLOR_RESET}')
            continue
        if num < 1 or num > len(options):
            print(f'  {COLOR_RED}{SYM_FAIL} invalid:'
                  f' {text}{COLOR_RESET}')
            continue
        selected[num - 1] = not selected[num - 1]

    return selected


def status_ok(step: int, total: int,
              label: str, detail: str) -> None:
    """Print a green checkmark status line."""
    _init_colors()
    print(f'  {COLOR_GREEN}{SYM_OK}{COLOR_RESET}'
          f' {label:<12s} {COLOR_DIM}{detail}{COLOR_RESET}')


def status_updated(step: int, total: int,
                   label: str, detail: str) -> None:
    """Print a green checkmark with 'updated' note."""
    _init_colors()
    print(f'  {COLOR_GREEN}{SYM_OK}{COLOR_RESET}'
          f' {label:<12s} {COLOR_DIM}{detail}{COLOR_RESET}'
          f'  {COLOR_GREEN}updated{COLOR_RESET}')


def status_skipped(step: int, total: int,
                   label: str, detail: str) -> None:
    """Print a dimmed dot status line."""
    _init_colors()
    print(f'  {COLOR_DIM}{SYM_DOT} {label:<12s}'
          f' {detail}{COLOR_RESET}')


def status_error(step: int, total: int,
                 label: str, err: object) -> None:
    """Print a red cross status line."""
    _init_colors()
    print(f'  {COLOR_RED}{SYM_FAIL}{COLOR_RESET}'
          f' {label:<12s} {COLOR_RED}{err}{COLOR_RESET}')


def detection_line(detected: bool, display: str,
                   version: str, path: str) -> None:
    """Print a detection result line."""
    _init_colors()
    from mnemon.setup.detect import home_dir
    display_path = path.replace(home_dir(), '~', 1)
    if detected:
        print(f'  {COLOR_GREEN}{SYM_OK}{COLOR_RESET}'
              f' {display:<14s} {COLOR_DIM}{version:<12s}'
              f' {display_path}{COLOR_RESET}')
    else:
        print(f'  {COLOR_DIM}{SYM_DOT} {display:<14s}'
              f' (not found){COLOR_RESET}')
