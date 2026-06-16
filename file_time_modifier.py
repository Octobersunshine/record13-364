import os
import sys
import argparse
import datetime
import glob
import platform
import tempfile
import fnmatch
from typing import List, Optional, Tuple, Dict, Iterable

IS_WINDOWS = platform.system() == 'Windows'

if IS_WINDOWS:
    try:
        import pywintypes
        import win32file
        import win32con
        import ctypes
        from ctypes import wintypes
        WIN32_AVAILABLE = True
    except ImportError:
        WIN32_AVAILABLE = False
else:
    WIN32_AVAILABLE = False

NSEC_PER_SEC = 1_000_000_000
NSEC_PER_MSEC = 1_000_000
NSEC_PER_USEC = 1_000
USEC_PER_SEC = 1_000_000


class FilesystemPrecision:
    NANOSECOND = 1
    MICROSECOND = NSEC_PER_USEC
    MILLISECOND = NSEC_PER_MSEC
    SECOND = NSEC_PER_SEC
    TWO_SECONDS = 2 * NSEC_PER_SEC


FS_PRECISION_LABELS = {
    FilesystemPrecision.NANOSECOND: '纳秒(ns)',
    FilesystemPrecision.MICROSECOND: '微秒(μs)',
    FilesystemPrecision.MILLISECOND: '毫秒(ms)',
    FilesystemPrecision.SECOND: '秒(s)',
    FilesystemPrecision.TWO_SECONDS: '2秒(s)',
}


def datetime_to_ns(dt: datetime.datetime) -> int:
    return int(dt.timestamp() * NSEC_PER_SEC)


def ns_to_datetime(ns: int, tz: Optional[datetime.timezone] = None) -> datetime.datetime:
    sec = ns // NSEC_PER_SEC
    nsec = ns % NSEC_PER_SEC
    if tz is not None:
        base = datetime.datetime.fromtimestamp(sec, tz=tz)
    else:
        base = datetime.datetime.fromtimestamp(sec)
    return base.replace(microsecond=nsec // NSEC_PER_USEC)


def timedelta_to_ns(td: datetime.timedelta) -> int:
    return int(td.total_seconds() * NSEC_PER_SEC)


def align_to_precision(ns: int, precision_ns: int) -> int:
    if precision_ns <= 0:
        return ns
    return (ns // precision_ns) * precision_ns


def detect_filesystem_precision(path: str) -> int:
    dir_path = os.path.dirname(os.path.abspath(path))
    if not dir_path:
        dir_path = '.'
    dir_path = os.path.abspath(dir_path)

    fd, test_file = tempfile.mkstemp(dir=dir_path)
    try:
        os.close(fd)

        test_times = [
            1_700_000_000 * NSEC_PER_SEC + 123_456_789,
            1_700_000_001 * NSEC_PER_SEC + 987_654_321,
        ]

        actual_precision = FilesystemPrecision.NANOSECOND

        for test_ns in test_times:
            try:
                os.utime(test_file, ns=(test_ns, test_ns))
            except (OSError, OverflowError, ValueError):
                try:
                    os.utime(test_file, (test_ns / NSEC_PER_SEC, test_ns / NSEC_PER_SEC))
                except Exception:
                    pass

            try:
                stat = os.stat(test_file)
                actual_ns = stat.st_mtime_ns
                diff = abs(test_ns - actual_ns)

                if diff >= FilesystemPrecision.TWO_SECONDS:
                    actual_precision = max(actual_precision, FilesystemPrecision.TWO_SECONDS)
                elif diff >= FilesystemPrecision.SECOND:
                    actual_precision = max(actual_precision, FilesystemPrecision.SECOND)
                elif diff >= FilesystemPrecision.MILLISECOND:
                    actual_precision = max(actual_precision, FilesystemPrecision.MILLISECOND)
                elif diff >= FilesystemPrecision.MICROSECOND:
                    actual_precision = max(actual_precision, FilesystemPrecision.MICROSECOND)
            except Exception:
                pass
    finally:
        try:
            os.unlink(test_file)
        except Exception:
            pass

    return actual_precision


def _get_stat_times_ns(stat_result: os.stat_result) -> Tuple[int, int, int]:
    atime_ns = getattr(stat_result, 'st_atime_ns', None)
    mtime_ns = getattr(stat_result, 'st_mtime_ns', None)
    ctime_ns = getattr(stat_result, 'st_ctime_ns', None)

    if atime_ns is None:
        atime_ns = int(stat_result.st_atime * NSEC_PER_SEC)
    if mtime_ns is None:
        mtime_ns = int(stat_result.st_mtime * NSEC_PER_SEC)
    if ctime_ns is None:
        ctime_ns = int(stat_result.st_ctime * NSEC_PER_SEC)

    return atime_ns, mtime_ns, ctime_ns


def _utime_with_precision(filepath: str, atime_ns: int, mtime_ns: int, precision_ns: int) -> None:
    aligned_atime = align_to_precision(atime_ns, precision_ns)
    aligned_mtime = align_to_precision(mtime_ns, precision_ns)

    try:
        os.utime(filepath, ns=(aligned_atime, aligned_mtime))
        return
    except (OSError, OverflowError, ValueError, NotImplementedError):
        pass

    try:
        atime_f = aligned_atime / NSEC_PER_SEC
        mtime_f = aligned_mtime / NSEC_PER_SEC
        os.utime(filepath, (atime_f, mtime_f))
        return
    except Exception:
        pass

    atime_s = aligned_atime // NSEC_PER_SEC
    mtime_s = aligned_mtime // NSEC_PER_SEC
    os.utime(filepath, (atime_s, mtime_s))


def _set_windows_ctime_ns(filepath: str, creation_time_ns: int, precision_ns: int) -> bool:
    if not WIN32_AVAILABLE:
        return False

    aligned_ctime_ns = align_to_precision(creation_time_ns, precision_ns)

    try:
        aligned_ctime_dt = ns_to_datetime(aligned_ctime_ns)

        handle = win32file.CreateFile(
            filepath,
            win32con.GENERIC_WRITE,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
            None,
            win32con.OPEN_EXISTING,
            0,
            None
        )

        try:
            if hasattr(ctypes, 'windll') and hasattr(ctypes.windll, 'kernel32'):
                try:
                    class FILETIME(ctypes.Structure):
                        _fields_ = [("dwLowDateTime", wintypes.DWORD),
                                    ("dwHighDateTime", wintypes.DWORD)]

                    # FILETIME is 100-nanosecond intervals since Jan 1, 1601
                    # Unix epoch is Jan 1, 1970 = 11644473600 seconds since Jan 1, 1601
                    EPOCH_DIFFERENCE = 11644473600
                    total_100ns = (aligned_ctime_ns // 100) + (EPOCH_DIFFERENCE * NSEC_PER_SEC // 100)

                    ft = FILETIME()
                    ft.dwLowDateTime = total_100ns & 0xFFFFFFFF
                    ft.dwHighDateTime = (total_100ns >> 32) & 0xFFFFFFFF

                    SetFileTime = ctypes.windll.kernel32.SetFileTime
                    SetFileTime.argtypes = [
                        wintypes.HANDLE,
                        ctypes.POINTER(FILETIME),
                        ctypes.POINTER(FILETIME),
                        ctypes.POINTER(FILETIME)
                    ]
                    SetFileTime.restype = wintypes.BOOL

                    result = SetFileTime(handle.handle, ctypes.byref(ft), None, None)
                    if result:
                        win32file.CloseHandle(handle)
                        return True
                except Exception:
                    pass

            ctime = pywintypes.Time(aligned_ctime_dt.timetuple())
            win32file.SetFileTime(handle, ctime, None, None)
            win32file.CloseHandle(handle)
            return True

        except Exception:
            win32file.CloseHandle(handle)
            raise

    except Exception as e:
        print(f"  设置创建时间失败: {e}")
        return False


def _times_close_enough(expected_ns: int, actual_ns: int, tolerance_ns: int) -> bool:
    return abs(expected_ns - actual_ns) <= tolerance_ns


def _verify_modification(
    filepath: str,
    expected_atime_ns: int,
    expected_mtime_ns: int,
    expected_ctime_ns: Optional[int],
    precision_ns: int,
    check_ctime: bool
) -> Tuple[bool, List[str]]:
    warnings = []
    tolerance = max(precision_ns, FilesystemPrecision.MILLISECOND)

    try:
        stat = os.stat(filepath)
        actual_atime_ns, actual_mtime_ns, actual_ctime_ns = _get_stat_times_ns(stat)

        if not _times_close_enough(align_to_precision(expected_atime_ns, precision_ns),
                                   actual_atime_ns, tolerance):
            expected = ns_to_datetime(expected_atime_ns)
            actual = ns_to_datetime(actual_atime_ns)
            warnings.append(f"访问时间精度警告: 期望 {expected}, 实际 {actual} (差异 {abs(expected_atime_ns - actual_atime_ns)}ns)")

        if not _times_close_enough(align_to_precision(expected_mtime_ns, precision_ns),
                                   actual_mtime_ns, tolerance):
            expected = ns_to_datetime(expected_mtime_ns)
            actual = ns_to_datetime(actual_mtime_ns)
            warnings.append(f"修改时间精度警告: 期望 {expected}, 实际 {actual} (差异 {abs(expected_mtime_ns - actual_mtime_ns)}ns)")

        if check_ctime and expected_ctime_ns is not None and IS_WINDOWS:
            if not _times_close_enough(align_to_precision(expected_ctime_ns, precision_ns),
                                       actual_ctime_ns, tolerance):
                expected = ns_to_datetime(expected_ctime_ns)
                actual = ns_to_datetime(actual_ctime_ns)
                warnings.append(f"创建时间精度警告: 期望 {expected}, 实际 {actual} (差异 {abs(expected_ctime_ns - actual_ctime_ns)}ns)")

    except Exception as e:
        warnings.append(f"验证修改结果时出错: {e}")

    return (len(warnings) == 0), warnings


def parse_time_string(time_str: str) -> datetime.datetime:
    formats = [
        '%Y-%m-%d %H:%M:%S.%f',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M',
        '%Y-%m-%d',
        '%Y/%m/%d %H:%M:%S.%f',
        '%Y/%m/%d %H:%M:%S',
        '%Y/%m/%d %H:%M',
        '%Y/%m/%d',
    ]
    for fmt in formats:
        try:
            dt = datetime.datetime.strptime(time_str, fmt)
            return dt
        except ValueError:
            continue
    raise ValueError(f"无法解析时间字符串: {time_str}")


def parse_offset(offset_str: str) -> datetime.timedelta:
    sign = 1
    if offset_str.startswith('-'):
        sign = -1
        offset_str = offset_str[1:]
    elif offset_str.startswith('+'):
        offset_str = offset_str[1:]

    days = 0
    hours = 0
    minutes = 0
    seconds = 0
    microseconds = 0

    if 'd' in offset_str:
        parts = offset_str.split('d')
        days = int(parts[0])
        offset_str = parts[1] if len(parts) > 1 else ''
    if 'h' in offset_str:
        parts = offset_str.split('h')
        hours = int(parts[0])
        offset_str = parts[1] if len(parts) > 1 else ''
    if 'm' in offset_str:
        parts = offset_str.split('m')
        minutes = int(parts[0])
        offset_str = parts[1] if len(parts) > 1 else ''
    if 's' in offset_str:
        sec_part = offset_str.split('s')[0]
        if '.' in sec_part:
            whole, frac = sec_part.split('.', 1)
            seconds = int(whole)
            frac = frac.ljust(6, '0')[:6]
            microseconds = int(frac)
        else:
            seconds = int(sec_part)

    return datetime.timedelta(
        days=sign * days,
        hours=sign * hours,
        minutes=sign * minutes,
        seconds=sign * seconds,
        microseconds=sign * microseconds
    )


def normalize_pattern(pattern: str) -> str:
    if IS_WINDOWS:
        return pattern.lower()
    return pattern


def match_any_pattern(filename: str, patterns: Iterable[str]) -> bool:
    filename_norm = normalize_pattern(filename)
    for pattern in patterns:
        pattern_norm = normalize_pattern(pattern)
        if fnmatch.fnmatch(filename_norm, pattern_norm):
            return True
    return False


def match_extension(filename: str, extensions: Iterable[str]) -> bool:
    _, ext = os.path.splitext(filename)
    ext_norm = normalize_pattern(ext)
    for target_ext in extensions:
        if not target_ext.startswith('.'):
            target_ext = '.' + target_ext
        target_norm = normalize_pattern(target_ext)
        if ext_norm == target_norm:
            return True
    return False


def should_include_file(
    filepath: str,
    include_patterns: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
    include_extensions: Optional[List[str]] = None,
    exclude_extensions: Optional[List[str]] = None
) -> bool:
    filename = os.path.basename(filepath)

    if exclude_patterns and match_any_pattern(filename, exclude_patterns):
        return False

    if exclude_extensions and match_extension(filename, exclude_extensions):
        return False

    if include_patterns and not match_any_pattern(filename, include_patterns):
        return False

    if include_extensions and not match_extension(filename, include_extensions):
        return False

    return True


def get_files(
    paths: List[str],
    recursive: bool = False,
    include_patterns: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
    include_extensions: Optional[List[str]] = None,
    exclude_extensions: Optional[List[str]] = None
) -> List[str]:
    files = []
    for path in paths:
        if os.path.isfile(path):
            abs_path = os.path.abspath(path)
            if should_include_file(abs_path, include_patterns, exclude_patterns,
                                   include_extensions, exclude_extensions):
                files.append(abs_path)
        elif os.path.isdir(path):
            if recursive:
                for root, _, filenames in os.walk(path):
                    for filename in filenames:
                        full_path = os.path.abspath(os.path.join(root, filename))
                        if should_include_file(full_path, include_patterns, exclude_patterns,
                                               include_extensions, exclude_extensions):
                            files.append(full_path)
            else:
                for entry in os.listdir(path):
                    full_path = os.path.join(path, entry)
                    if os.path.isfile(full_path):
                        abs_path = os.path.abspath(full_path)
                        if should_include_file(abs_path, include_patterns, exclude_patterns,
                                               include_extensions, exclude_extensions):
                            files.append(abs_path)
        else:
            matched = glob.glob(path)
            for match in matched:
                if os.path.isfile(match):
                    abs_path = os.path.abspath(match)
                    if should_include_file(abs_path, include_patterns, exclude_patterns,
                                           include_extensions, exclude_extensions):
                        files.append(abs_path)
    return list(set(files))


def modify_file_time(
    filepath: str,
    atime: Optional[datetime.datetime] = None,
    mtime: Optional[datetime.datetime] = None,
    ctime: Optional[datetime.datetime] = None,
    offset: Optional[datetime.timedelta] = None,
    fs_precision: Optional[int] = None
) -> Tuple[bool, str]:
    try:
        stat = os.stat(filepath)
        current_atime_ns, current_mtime_ns, current_ctime_ns = _get_stat_times_ns(stat)

        if fs_precision is None:
            fs_precision = detect_filesystem_precision(filepath)

        if atime is not None:
            target_atime_ns = datetime_to_ns(atime)
        else:
            target_atime_ns = current_atime_ns

        if mtime is not None:
            target_mtime_ns = datetime_to_ns(mtime)
        else:
            target_mtime_ns = current_mtime_ns

        if ctime is not None:
            target_ctime_ns = datetime_to_ns(ctime)
        else:
            target_ctime_ns = current_ctime_ns

        if offset:
            offset_ns = timedelta_to_ns(offset)
            if atime is None:
                target_atime_ns = current_atime_ns + offset_ns
            if mtime is None:
                target_mtime_ns = current_mtime_ns + offset_ns
            if ctime is None:
                target_ctime_ns = current_ctime_ns + offset_ns

        _utime_with_precision(filepath, target_atime_ns, target_mtime_ns, fs_precision)

        ctime_ok = True
        check_ctime = False
        if ctime is not None or offset is not None:
            check_ctime = True
            if IS_WINDOWS:
                ctime_ok = _set_windows_ctime_ns(filepath, target_ctime_ns, fs_precision)
            else:
                ctime_ok = False

        verify_ok, warnings = _verify_modification(
            filepath, target_atime_ns, target_mtime_ns,
            target_ctime_ns, fs_precision, check_ctime
        )

        result_atime = ns_to_datetime(align_to_precision(target_atime_ns, fs_precision))
        result_mtime = ns_to_datetime(align_to_precision(target_mtime_ns, fs_precision))
        precision_label = FS_PRECISION_LABELS.get(fs_precision, f'{fs_precision}ns')

        msg_parts = [f"成功 (文件系统精度: {precision_label}) - atime: {result_atime}, mtime: {result_mtime}"]
        if ctime_ok and (ctime is not None or offset is not None) and IS_WINDOWS:
            result_ctime = ns_to_datetime(align_to_precision(target_ctime_ns, fs_precision))
            msg_parts.append(f", ctime: {result_ctime}")
        elif not IS_WINDOWS and (ctime is not None or offset is not None):
            msg_parts.append(" (创建时间: 非 Windows 系统不支持修改)")

        if warnings:
            for w in warnings:
                msg_parts.append(f"\n  ⚠ {w}")

        return True, ''.join(msg_parts)

    except Exception as e:
        return False, f"失败: {str(e)}"


def batch_modify(
    paths: List[str],
    recursive: bool = False,
    atime: Optional[datetime.datetime] = None,
    mtime: Optional[datetime.datetime] = None,
    ctime: Optional[datetime.datetime] = None,
    offset: Optional[datetime.timedelta] = None,
    dry_run: bool = False,
    include_patterns: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
    include_extensions: Optional[List[str]] = None,
    exclude_extensions: Optional[List[str]] = None
) -> None:
    files = get_files(paths, recursive, include_patterns, exclude_patterns,
                      include_extensions, exclude_extensions)

    filter_info = []
    if include_patterns:
        filter_info.append(f"包含模式: {', '.join(include_patterns)}")
    if exclude_patterns:
        filter_info.append(f"排除模式: {', '.join(exclude_patterns)}")
    if include_extensions:
        filter_info.append(f"包含扩展名: {', '.join(include_extensions)}")
    if exclude_extensions:
        filter_info.append(f"排除扩展名: {', '.join(exclude_extensions)}")
    if recursive:
        filter_info.append("递归处理子目录")
    if filter_info:
        print(f"[筛选条件] {', '.join(filter_info)}")

    if not files:
        print("未找到任何匹配的文件")
        return

    fs_cache: Dict[str, int] = {}

    def get_cached_precision(filepath: str) -> int:
        drive = os.path.splitdrive(os.path.abspath(filepath))[0]
        if drive not in fs_cache:
            fs_cache[drive] = detect_filesystem_precision(filepath)
            prec_label = FS_PRECISION_LABELS.get(fs_cache[drive], f'{fs_cache[drive]}ns')
            print(f"[信息] 检测到文件系统精度 ({drive}): {prec_label}")
        return fs_cache[drive]

    print(f"找到 {len(files)} 个文件:")
    print("-" * 60)

    success_count = 0
    fail_count = 0

    for i, filepath in enumerate(files, 1):
        filename = os.path.basename(filepath)
        print(f"[{i}/{len(files)}] {filename}")

        fs_precision = get_cached_precision(filepath)

        if dry_run:
            stat = os.stat(filepath)
            current_atime_ns, current_mtime_ns, current_ctime_ns = _get_stat_times_ns(stat)
            current_atime = ns_to_datetime(current_atime_ns)
            current_mtime = ns_to_datetime(current_mtime_ns)
            current_ctime = ns_to_datetime(current_ctime_ns)
            precision_label = FS_PRECISION_LABELS.get(fs_precision, f'{fs_precision}ns')
            print(f"  文件系统精度: {precision_label}")
            print(f"  当前 - atime: {current_atime}, mtime: {current_mtime}, ctime: {current_ctime}")

            if offset:
                offset_ns = timedelta_to_ns(offset)
                new_atime_ns = current_atime_ns + offset_ns if atime is None else datetime_to_ns(atime)
                new_mtime_ns = current_mtime_ns + offset_ns if mtime is None else datetime_to_ns(mtime)
                new_ctime_ns = current_ctime_ns + offset_ns if ctime is None else (datetime_to_ns(ctime) if ctime else None)
            else:
                new_atime_ns = datetime_to_ns(atime) if atime else current_atime_ns
                new_mtime_ns = datetime_to_ns(mtime) if mtime else current_mtime_ns
                new_ctime_ns = datetime_to_ns(ctime) if ctime else None

            new_atime = ns_to_datetime(align_to_precision(new_atime_ns, fs_precision))
            new_mtime = ns_to_datetime(align_to_precision(new_mtime_ns, fs_precision))
            print(f"  修改后 - atime: {new_atime}, mtime: {new_mtime}")
            if new_ctime_ns is not None and IS_WINDOWS:
                new_ctime = ns_to_datetime(align_to_precision(new_ctime_ns, fs_precision))
                print(f"  修改后 ctime: {new_ctime}")
            print()
            continue

        success, message = modify_file_time(filepath, atime, mtime, ctime, offset, fs_precision)
        if success:
            success_count += 1
        else:
            fail_count += 1
        print(f"  {message}")
        print()

    print("-" * 60)
    if dry_run:
        print("预览模式完成，未实际修改任何文件")
    else:
        print(f"批量修改完成，成功: {success_count}, 失败: {fail_count}, 共处理 {len(files)} 个文件")


def _parse_comma_list(value: str) -> List[str]:
    return [item.strip() for item in value.split(',') if item.strip()]


def main():
    parser = argparse.ArgumentParser(
        description='批量修改文件时间属性（创建时间、修改时间、访问时间） - 支持跨文件系统高精度',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 查看当前文件时间（预览模式）
  python file_time_modifier.py ./test_files --dry-run

  # 设置统一时间
  python file_time_modifier.py ./test_files --mtime "2024-01-01 12:00:00"

  # 设置所有时间为同一时间
  python file_time_modifier.py ./test_files --time "2024-01-01 12:00:00"

  # 统一提前 7 天
  python file_time_modifier.py ./test_files --offset "-7d"

  # 统一延后 3 天 12 小时
  python file_time_modifier.py ./test_files --offset "+3d12h"

  # 递归处理子目录
  python file_time_modifier.py ./test_files -r --offset "-7d"

  # 使用通配符
  python file_time_modifier.py ./test_files/*.txt --time "2024-01-01"

  # 按扩展名筛选（仅修改 txt 文件）
  python file_time_modifier.py ./test_files --ext ".txt" --time "2024-01-01"

  # 按扩展名筛选（多个扩展名）
  python file_time_modifier.py ./test_files --ext "txt,pdf,doc" --time "2024-01-01"

  # 按文件名模式匹配
  python file_time_modifier.py ./test_files --include "report_*.pdf" --time "2024-01-01"

  # 多个包含模式
  python file_time_modifier.py ./test_files --include "*.txt,*.pdf" --time "2024-01-01"

  # 排除模式（排除临时文件）
  python file_time_modifier.py ./test_files --exclude "*.tmp,*.log" --offset "-7d"

  # 组合筛选：递归 + 扩展名 + 排除模式
  python file_time_modifier.py ./docs -r --ext ".txt,.md" --exclude "*draft*" --offset "+3d"

  # 仅修改创建时间（Windows 系统）
  python file_time_modifier.py ./test_files --ctime "2024-06-01 00:00:00"
        """
    )

    parser.add_argument('paths', nargs='+', help='文件或目录路径，支持通配符')
    parser.add_argument('-r', '--recursive', action='store_true', help='递归处理子目录')
    parser.add_argument('--dry-run', action='store_true', help='预览模式，不实际修改')

    parser.add_argument('--time', type=str, help='统一设置所有时间（优先级低于单独指定）')
    parser.add_argument('--atime', type=str, help='访问时间，格式: YYYY-MM-DD [HH:MM:SS]')
    parser.add_argument('--mtime', type=str, help='修改时间，格式: YYYY-MM-DD [HH:MM:SS]')
    parser.add_argument('--ctime', type=str, help='创建时间（仅 Windows），格式: YYYY-MM-DD [HH:MM:SS]')

    parser.add_argument('--offset', type=str, help='时间偏移量，如 -7d, +3d12h, -2h30m')

    parser.add_argument('--include', type=_parse_comma_list, metavar='PATTERNS',
                        help='包含文件名模式，多个用逗号分隔，如: *.txt,report_*.pdf')
    parser.add_argument('--exclude', type=_parse_comma_list, metavar='PATTERNS',
                        help='排除文件名模式，多个用逗号分隔，如: *.tmp,*.log')
    parser.add_argument('--ext', type=_parse_comma_list, metavar='EXTENSIONS',
                        help='包含文件扩展名，多个用逗号分隔，如: .txt,pdf,doc')
    parser.add_argument('--exclude-ext', type=_parse_comma_list, metavar='EXTENSIONS',
                        help='排除文件扩展名，多个用逗号分隔，如: .tmp,log')

    args = parser.parse_args()

    if not any([args.time, args.atime, args.mtime, args.ctime, args.offset]):
        print("错误: 必须指定至少一个时间参数（--time, --atime, --mtime, --ctime, --offset）")
        parser.print_help()
        sys.exit(1)

    unified_time = parse_time_string(args.time) if args.time else None
    atime = parse_time_string(args.atime) if args.atime else (unified_time if unified_time else None)
    mtime = parse_time_string(args.mtime) if args.mtime else (unified_time if unified_time else None)
    ctime = parse_time_string(args.ctime) if args.ctime else (unified_time if unified_time and IS_WINDOWS else None)

    offset = parse_offset(args.offset) if args.offset else None

    if IS_WINDOWS and ctime and not WIN32_AVAILABLE:
        print("警告: 未安装 pywin32，无法修改创建时间。请运行: pip install pywin32")

    batch_modify(
        paths=args.paths,
        recursive=args.recursive,
        atime=atime,
        mtime=mtime,
        ctime=ctime,
        offset=offset,
        dry_run=args.dry_run,
        include_patterns=args.include,
        exclude_patterns=args.exclude,
        include_extensions=args.ext,
        exclude_extensions=args.exclude_ext
    )


if __name__ == '__main__':
    main()
