import os
import sys
import argparse
import datetime
import glob
import platform
from typing import List, Optional, Tuple

IS_WINDOWS = platform.system() == 'Windows'

if IS_WINDOWS:
    try:
        import pywintypes
        import win32file
        import win32con
        WIN32_AVAILABLE = True
    except ImportError:
        WIN32_AVAILABLE = False
else:
    WIN32_AVAILABLE = False


def parse_time_string(time_str: str) -> datetime.datetime:
    formats = [
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M',
        '%Y-%m-%d',
        '%Y/%m/%d %H:%M:%S',
        '%Y/%m/%d %H:%M',
        '%Y/%m/%d',
    ]
    for fmt in formats:
        try:
            return datetime.datetime.strptime(time_str, fmt)
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
        seconds = int(offset_str.replace('s', ''))

    return datetime.timedelta(
        days=sign * days,
        hours=sign * hours,
        minutes=sign * minutes,
        seconds=sign * seconds
    )


def get_files(paths: List[str], recursive: bool = False) -> List[str]:
    files = []
    for path in paths:
        if os.path.isfile(path):
            files.append(os.path.abspath(path))
        elif os.path.isdir(path):
            if recursive:
                for root, _, filenames in os.walk(path):
                    for filename in filenames:
                        files.append(os.path.abspath(os.path.join(root, filename)))
            else:
                for entry in os.listdir(path):
                    full_path = os.path.join(path, entry)
                    if os.path.isfile(full_path):
                        files.append(os.path.abspath(full_path))
        else:
            matched = glob.glob(path)
            for match in matched:
                if os.path.isfile(match):
                    files.append(os.path.abspath(match))
    return list(set(files))


def set_windows_ctime(filepath: str, creation_time: datetime.datetime) -> bool:
    if not WIN32_AVAILABLE:
        return False
    try:
        handle = win32file.CreateFile(
            filepath,
            win32con.GENERIC_WRITE,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
            None,
            win32con.OPEN_EXISTING,
            0,
            None
        )
        ctime = pywintypes.Time(creation_time.timetuple())
        win32file.SetFileTime(handle, ctime, None, None)
        win32file.CloseHandle(handle)
        return True
    except Exception as e:
        print(f"  设置创建时间失败: {e}")
        return False


def modify_file_time(
    filepath: str,
    atime: Optional[datetime.datetime] = None,
    mtime: Optional[datetime.datetime] = None,
    ctime: Optional[datetime.datetime] = None,
    offset: Optional[datetime.timedelta] = None
) -> Tuple[bool, str]:
    try:
        stat = os.stat(filepath)
        current_atime = datetime.datetime.fromtimestamp(stat.st_atime)
        current_mtime = datetime.datetime.fromtimestamp(stat.st_mtime)
        current_ctime = datetime.datetime.fromtimestamp(stat.st_ctime)

        if offset:
            new_atime = current_atime + offset if atime is None else atime
            new_mtime = current_mtime + offset if mtime is None else mtime
            new_ctime = current_ctime + offset if ctime is None else ctime
        else:
            new_atime = atime if atime is not None else current_atime
            new_mtime = mtime if mtime is not None else current_mtime
            new_ctime = ctime if ctime is not None else current_ctime

        atime_ts = new_atime.timestamp()
        mtime_ts = new_mtime.timestamp()

        os.utime(filepath, (atime_ts, mtime_ts))

        ctime_ok = True
        if ctime or offset:
            if IS_WINDOWS:
                ctime_ok = set_windows_ctime(filepath, new_ctime)
            else:
                print(f"  警告: 非 Windows 系统无法修改创建时间")

        return True, f"成功 - atime: {new_atime}, mtime: {new_mtime}" + (f", ctime: {new_ctime}" if ctime_ok else "")

    except Exception as e:
        return False, f"失败: {str(e)}"


def batch_modify(
    paths: List[str],
    recursive: bool = False,
    atime: Optional[datetime.datetime] = None,
    mtime: Optional[datetime.datetime] = None,
    ctime: Optional[datetime.datetime] = None,
    offset: Optional[datetime.timedelta] = None,
    dry_run: bool = False
) -> None:
    files = get_files(paths, recursive)

    if not files:
        print("未找到任何文件")
        return

    print(f"找到 {len(files)} 个文件:")
    print("-" * 60)

    for i, filepath in enumerate(files, 1):
        filename = os.path.basename(filepath)
        print(f"[{i}/{len(files)}] {filename}")

        if dry_run:
            stat = os.stat(filepath)
            current_atime = datetime.datetime.fromtimestamp(stat.st_atime)
            current_mtime = datetime.datetime.fromtimestamp(stat.st_mtime)
            current_ctime = datetime.datetime.fromtimestamp(stat.st_ctime)
            print(f"  当前 - atime: {current_atime}, mtime: {current_mtime}, ctime: {current_ctime}")
            if offset:
                print(f"  偏移后 - atime: {current_atime + offset}, mtime: {current_mtime + offset}, ctime: {current_ctime + offset}")
            else:
                if atime:
                    print(f"  新 atime: {atime}")
                if mtime:
                    print(f"  新 mtime: {mtime}")
                if ctime and IS_WINDOWS:
                    print(f"  新 ctime: {ctime}")
            print()
            continue

        success, message = modify_file_time(filepath, atime, mtime, ctime, offset)
        print(f"  {message}")
        print()

    print("-" * 60)
    if dry_run:
        print("预览模式完成，未实际修改任何文件")
    else:
        print(f"批量修改完成，共处理 {len(files)} 个文件")


def main():
    parser = argparse.ArgumentParser(
        description='批量修改文件时间属性（创建时间、修改时间、访问时间）',
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
        dry_run=args.dry_run
    )


if __name__ == '__main__':
    main()
