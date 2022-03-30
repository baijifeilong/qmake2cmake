#!/usr/bin/env python3
#############################################################################
##
## Copyright (C) 2018 The Qt Company Ltd.
## Contact: https://www.qt.io/licensing/
##
## This file is part of the plugins of the Qt Toolkit.
##
## $QT_BEGIN_LICENSE:GPL-EXCEPT$
## Commercial License Usage
## Licensees holding valid commercial Qt licenses may use this file in
## accordance with the commercial license agreement provided with the
## Software or, alternatively, in accordance with the terms contained in
## a written agreement between you and The Qt Company. For licensing terms
## and conditions see https://www.qt.io/terms-conditions. For further
## information use the contact form at https://www.qt.io/contact-us.
##
## GNU General Public License Usage
## Alternatively, this file may be used under the terms of the GNU
## General Public License version 3 as published by the Free Software
## Foundation with exceptions as appearing in the file LICENSE.GPL3-EXCEPT
## included in the packaging of this file. Please review the following
## information to ensure the GNU General Public License requirements will
## be met: https://www.gnu.org/licenses/gpl-3.0.html.
##
## $QT_END_LICENSE$
##
#############################################################################

import glob
import os
import subprocess
import concurrent.futures
import collections
import sys
import typing
import argparse
import qmake_parser
from argparse import ArgumentParser
from pro2cmake import do_include, Scope


def parse_command_line() -> argparse.Namespace:
    parser = ArgumentParser(
        description="Run pro2cmake on all .pro files recursively in given path. "
        "You can pass additional arguments to the pro2cmake calls by appending "
        "-- --foo --bar"
    )
    parser.add_argument(
        "--only-existing",
        dest="only_existing",
        action="store_true",
        help="Run pro2cmake only on .pro files that already have a CMakeLists.txt.",
    )
    parser.add_argument(
        "--only-missing",
        dest="only_missing",
        action="store_true",
        help="Run pro2cmake only on .pro files that do not have a CMakeLists.txt.",
    )
    parser.add_argument(
        "--only-qtbase-main-modules",
        dest="only_qtbase_main_modules",
        action="store_true",
        help="Run pro2cmake only on the main modules in qtbase.",
    )
    parser.add_argument(
        "--skip-subdirs-projects",
        dest="skip_subdirs_projects",
        action="store_true",
        help="Don't run pro2cmake on TEMPLATE=subdirs projects.",
    )
    parser.add_argument(
        "--skip-smarty-directory-filtering",
        dest="skip_smart_directory_filtering",
        action="store_true",
        help="Don't run pro2cmake on a pro file which is included in a subdir project in the same "
        "directory.",
    )
    parser.add_argument(
        "--count", dest="count", help="How many projects should be converted.", type=int
    )
    parser.add_argument(
        "--offset",
        dest="offset",
        help="From the list of found projects, from which project should conversion begin.",
        type=int,
    )
    parser.add_argument(
        "path", metavar="<path>", type=str, help="The path where to look for .pro files."
    )

    args, unknown = parser.parse_known_args()

    # Error out when the unknown arguments do not start with a "--",
    # which implies passing through arguments to pro2cmake.
    if len(unknown) > 0 and unknown[0] != "--":
        parser.error("unrecognized arguments: {}".format(" ".join(unknown)))
    else:
        args.pro2cmake_args = unknown[1:]

    return args


def find_all_pro_files(base_path: str, args: argparse.Namespace):
    def sorter(pro_file: str) -> str:
        """Sorter that tries to prioritize main pro files in a directory."""
        pro_file_without_suffix = pro_file.rsplit("/", 1)[-1][:-4]
        dir_name = os.path.dirname(pro_file)
        if dir_name == ".":
            dir_name = os.path.basename(os.getcwd())
        elif dir_name.startswith("./"):
            dir_name = os.path.basename(os.getcwd()) + "/" + dir_name[2:]
        if dir_name.endswith(pro_file_without_suffix):
            return dir_name
        return dir_name + "/__" + pro_file

    all_files = []
    previous_dir_name: typing.Optional[str] = None

    print("Finding .pro files.")
    glob_result = glob.glob(os.path.join(base_path, "**/*.pro"), recursive=True)

    def cmake_lists_exists_filter(path):
        path_dir_name = os.path.dirname(path)
        if os.path.exists(os.path.join(path_dir_name, "CMakeLists.txt")):
            return True
        return False

    def cmake_lists_missing_filter(path):
        return not cmake_lists_exists_filter(path)

    def qtbase_main_modules_filter(path):
        main_modules = [
            "corelib",
            "network",
            "gui",
            "widgets",
            "testlib",
            "printsupport",
            "opengl",
            "sql",
            "dbus",
            "concurrent",
            "xml",
        ]
        path_suffixes = [f"src/{m}/{m}.pro" for m in main_modules]

        for path_suffix in path_suffixes:
            if path.endswith(path_suffix):
                return True
        return False

    filter_result = glob_result
    filter_func = None
    if args.only_existing:
        filter_func = cmake_lists_exists_filter
    elif args.only_missing:
        filter_func = cmake_lists_missing_filter
    elif args.only_qtbase_main_modules:
        filter_func = qtbase_main_modules_filter

    if filter_func:
        print("Filtering.")
        filter_result = [p for p in filter_result if filter_func(p)]

    def read_file_contents(file_path):
        with open(file_path, "r") as file_fd:
            contents = file_fd.read()
        return contents

    def is_subdirs_project(file_path):
        file_contents = read_file_contents(file_path)
        parse_result, massaged_file_contents = qmake_parser.parseProFileContents(file_contents)
        file_scope = Scope.FromDict(
            None,
            file_path,
            parse_result.asDict().get("statements"),
            project_file_content=massaged_file_contents,
        )
        do_include(file_scope)
        return file_scope.get_string("TEMPLATE") == "subdirs"

    def filter_non_subdirs_pro_files_in_same_dir(pro_files):
        result = []
        pro_files_by_dir = collections.defaultdict(list)
        for f in pro_files:
            dir_path = os.path.dirname(f)
            pro_files_by_dir[dir_path].append(f)

        for one_dir, dir_files in pro_files_by_dir.items():
            if len(dir_files) <= 1:
                result += dir_files
                continue
            print(f"Multiple .pro files found in {one_dir}")
            subdirs_projects = set(filter(is_subdirs_project, dir_files))
            skipped_projects = []
            if len(subdirs_projects) == 0 or len(subdirs_projects) > 1:
                p = dir_files[0]
                result.append(p)
                skipped_projects = dir_files[:1]
                if len(subdirs_projects) == 0:
                    print(f"  No SUBDIRS project found.")
                else:
                    print(f"  Multiple SUBDIRS projects found")
                print(f"  Selecting the first .pro file {p}")
            if len(subdirs_projects) == 1:
                p = subdirs_projects.pop()
                print(f"  SUBDIRS project selected for conversion: {p}")
                result.append(p)
                skipped_projects = list(set(dir_files) - {p})
            for p in skipped_projects:
                print(f"  Skipping: {p}")
        return result

    if not args.skip_smart_directory_filtering:
        filter_result = filter_non_subdirs_pro_files_in_same_dir(filter_result)

    for pro_file in sorted(filter_result, key=sorter):
        dir_name = os.path.dirname(pro_file)
        if dir_name == previous_dir_name:
            print("Skipping:", pro_file)
        else:
            all_files.append(pro_file)
            previous_dir_name = dir_name
    return all_files


def run(all_files: typing.List[str], pro2cmake: str, args: argparse.Namespace) -> typing.List[str]:
    failed_files = []
    files_count = len(all_files)
    workers = os.cpu_count() or 1

    if args.only_qtbase_main_modules:
        # qtbase main modules take longer than usual to process.
        workers = 2

    def _process_a_file(
        data: typing.Tuple[str, int, int], direct_output: bool = False
    ) -> typing.Tuple[int, str, str]:
        filename, index, total = data
        pro2cmake_args = []
        if sys.platform == "win32":
            pro2cmake_args.append(sys.executable)
        pro2cmake_args.append(pro2cmake)
        if args.skip_subdirs_projects:
            pro2cmake_args.append("--skip-subdirs-project")
        pro2cmake_args.append(os.path.basename(filename))

        if args.pro2cmake_args:
            pro2cmake_args += args.pro2cmake_args

        if direct_output:
            stdout_arg = None
            stderr_arg = None
        else:
            stdout_arg = subprocess.PIPE
            stderr_arg = subprocess.STDOUT

        result = subprocess.run(
            pro2cmake_args,
            cwd=os.path.dirname(filename),
            stdout=stdout_arg,
            stderr=stderr_arg,
        )
        stdout = f"Converted[{index}/{total}]: {filename}\n"
        if direct_output:
            output_result = ""
        else:
            output_result = stdout + result.stdout.decode()
        return result.returncode, filename, output_result

    # Convert the main .pro file first to create the subdir markers.
    print(f"Converting the main project file {all_files[0]}")
    _process_a_file((all_files[0], 0, 1), direct_output=True)
    all_files = all_files[1:]

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, initargs=(10,)) as pool:
        print("Firing up thread pool executor.")

        for return_code, filename, stdout in pool.map(
            _process_a_file,
            zip(all_files, range(1, files_count + 1), (files_count for _ in all_files)),
        ):
            if return_code:
                failed_files.append(filename)
            print(stdout)

    return failed_files


def main() -> None:
    args = parse_command_line()

    script_path = os.path.dirname(os.path.abspath(__file__))
    pro2cmake = os.path.join(script_path, "pro2cmake.py")
    base_path = args.path

    all_files = find_all_pro_files(base_path, args)
    if args.offset:
        all_files = all_files[args.offset :]
    if args.count:
        all_files = all_files[: args.count]
    files_count = len(all_files)

    failed_files = run(all_files, pro2cmake, args)
    if len(all_files) == 0:
        print("No files found.")

    if failed_files:
        print(
            f"The following files were not successfully "
            f"converted ({len(failed_files)} of {files_count}):"
        )
        for f in failed_files:
            print(f'    "{f}"')


if __name__ == "__main__":
    main()
