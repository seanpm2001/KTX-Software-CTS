#!/usr/bin/python3
# Copyright 2022-2023 The Khronos Group Inc.
# Copyright 2022-2023 RasterGrid Kft.
# SPDX-License-Identifier: Apache-2.0

import argparse
import os
import subprocess
import re
import json
import itertools
import filecmp
import shutil


class SubcaseContext:
    def __init__(self, args):
        self.args = args
        self.pattern = re.compile("\$\{(\{[a-zA-Z0-9_\-\[\]']+\})\}")

    def eval(self, value):
        return self.pattern.sub(lambda m: m.group(1), value.replace('{', '{{').replace('}', '}}')).format(**self.args)

    def match(self, expected, actual):
        expected = self.eval(expected)
        inside_regex = False
        pos = expected.find('`')
        expected_regex = re.escape(expected[0:pos])
        while pos >= 0:
            inside_regex = not inside_regex
            start = pos + 1
            pos = expected.find('`', start)
            if inside_regex:
                expected_regex += expected[start:pos]
            else:
                expected_regex += re.escape(expected[start:pos])
        return re.match(expected_regex, actual)

    def cmdArgs(self):
        arg_list = []
        for arg in self.args:
            arg_list.append(f"--{arg} \"{str(self.args[arg])}\"")
        return ' '.join(arg_list)


if __name__ == '__main__':
    # Parse and check base arguments

    parser = argparse.ArgumentParser(
        description="KTX CLI Tool Test Runner",
        usage="clitest.py <json-test-file> [<args>]")

    parser.add_argument('json_test_file')
    parser.add_argument('-r', '--regen-golden', action='store_true',
                        help='Regenerate reference files')
    parser.add_argument('-e', '--executable-path', action='store', required=True,
                        help='Path to use for the executed command')
    parser.add_argument('--msvc', action='store_true',
                        help='Indicates that test runs against MSVC build')

    cli_args, unknown_args = parser.parse_known_args()


    # Load JSON test case description and check basic contents

    if not os.path.isfile(cli_args.json_test_file):
        print(f"ERROR: Cannot find JSON test file '{cli_args.json_test_file}'")
        exit(1)

    f = open(cli_args.json_test_file)
    testcase = json.load(f)
    f.close()

    if not 'description' in testcase:
        print("ERROR: Missing 'description' from JSON test case")
        exit(1)

    if not 'command' in testcase:
        print("ERROR: Missing 'command' from JSON test case")
        exit(1)

    if not 'status' in testcase:
        print("ERROR: Missing 'status' from JSON test case")
        exit(1)


    # Collect subcase arguments within the test case

    subcases = []
    arg_name_list = []
    arg_value_list = []

    if 'args' in testcase:
        for arg in testcase['args']:
            values = testcase['args'][arg]
            str_values = [str(value) for value in testcase['args'][arg]]
            parser.add_argument(f"--{arg}", action='store', choices=str_values)
            arg_name_list.append(arg)
            arg_value_list.append(values)

    cli_args = parser.parse_args()

    arg_value_combinations = list(itertools.product(*arg_value_list))
    for combination in arg_value_combinations:
        subcase = {}
        for index, arg in enumerate(arg_name_list):
            subcase[arg] = combination[index]
        subcases.append(subcase)


    # Execute subcases

    if not cli_args.regen_golden:
        print(f"Executing test case '{cli_args.json_test_file}'...")

    failed = False
    messages = []
    subcases_passed = 0
    subcases_skipped = 0
    subcases_failed = 0

    ref_not_updated = {}

    for subcase_index, subcase in enumerate(subcases):
        skip_subcase = False

        for arg in arg_name_list:
            arg_value = getattr(cli_args, arg)
            if arg_value is not None and arg_value != str(subcase[arg]):
                skip_subcase = True

        if skip_subcase:
            subcases_skipped += 1
            continue

        ctx = SubcaseContext(subcase)

        cmd_failed = False
        subcase_failed = False
        subcase_messages = []

        if not cli_args.regen_golden:
            messages.append(f"  {ctx.eval(testcase['command'])}")

        # Run command

        output = {
            'stdout': '',
            'stderr': ''
        }

        try:
            cmd_args = [ arg for arg in ctx.eval(testcase['command']).split(' ') if arg ]
            cmd_args[0] = cli_args.executable_path

            # Use a file as stdin if specified
            if 'stdin' in testcase:
                stdin_pipe = open(ctx.eval(testcase['stdin']), mode='rb')
            else:
                stdin_pipe = subprocess.DEVNULL

            proc = subprocess.Popen(
                cmd_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=stdin_pipe)

            stdout, stderr = proc.communicate()
            status = proc.returncode

            any_status = False
            if isinstance(testcase['status'], str):
                status_expr = ctx.eval(testcase['status'])
                if status_expr == "*":
                    any_status = True
                else:
                    expected_status = int(status_expr)
            else:
                expected_status = testcase['status']

            if not any_status and status != expected_status:
                subcase_messages.append(f"Expected return code '{expected_status}' but got '{status}'")
                subcase_failed = True

            output = {
                'stdout': stdout.decode('utf-8', 'replace').replace("\r\n", "\n"),
                'stderr': stderr.decode('utf-8', 'replace').replace("\r\n", "\n")
            }

        except Exception as excp:
            subcase_messages.append("Failed to execute command:")
            subcase_messages.append(f"  {ctx.eval(testcase['command'])}")
            subcase_messages.append(f"  Exception:")
            subcase_messages.append(str(excp))
            output = {
                'stdout': output['stdout'],
                'stderr': output['stderr'] + str(excp)
            }
            cmd_failed = True
            subcase_failed = True


        # Check stdout/stderr

        for stdfile in [ 'stdout', 'stderr' ]:
            output_ref = ''
            output_ref_filename = None

            if stdfile in testcase:
                output_ref_filename = ctx.eval(testcase[stdfile])

                if not cmd_failed and cli_args.regen_golden:
                    old_output_ref_contains_regex = False
                    if os.path.isfile(output_ref_filename):
                        old_output_ref_file = open(output_ref_filename, 'r', encoding='utf-8')
                        old_output_ref = old_output_ref_file.read()
                        old_output_ref_file.close()
                        old_output_ref_contains_regex = bool(re.findall(r'`.*`', old_output_ref))

                    if old_output_ref_contains_regex:
                        ref_not_updated[output_ref_filename] = f"NOTE: reference file '{output_ref_filename}' was not updated as it contains a regex"
                    else:
                        os.makedirs(os.path.dirname(output_ref_filename), exist_ok=True)
                        output_ref_file = open(output_ref_filename, 'w+', newline='\n', encoding='utf-8')
                        output_ref_file.write(output[stdfile])
                        output_ref_file.close()

                if not os.path.isfile(output_ref_filename):
                    subcase_messages.append(f"Cannot find reference {stdfile} file '{output_ref_filename}'")
                    subcase_failed = True
                    continue

                output_ref_file = open(output_ref_filename, 'r', encoding='utf-8')
                output_ref = output_ref_file.read()
                output_ref_file.close()

            if not ctx.match(output_ref, output[stdfile]):
                output_filename = f"output/{cli_args.json_test_file[len('tests/'):]}.{subcase_index + 1}.{stdfile[3:]}"
                output_file = open(output_filename, 'w+', newline='\n', encoding='utf-8')
                output_file.write(output[stdfile])
                output_file.close()
                if output_ref_filename:
                    subcase_messages.append(f"Mismatch between {stdfile} and reference file '{output_ref_filename}'")
                else:
                    subcase_messages.append(f"Expected no {stdfile} but {stdfile} is not empty")
                subcase_messages.append(f"  {stdfile} for subcase is written to '{output_filename}'")
                subcase_failed = True


        # Check output files
        if 'outputs' in testcase:
            for output_cur, output_ref in testcase['outputs'].items():
                files_found = True

                output_cur = ctx.eval(output_cur)
                output_ref = ctx.eval(output_ref)

                if not cmd_failed and cli_args.regen_golden:
                    os.makedirs(os.path.dirname(output_ref), exist_ok=True)
                    if os.path.isfile(output_cur):
                        if cli_args.msvc and 'allowOutputMismatchOnMSVC' in testcase:
                            ref_not_updated[output_ref] = f"NOTE: reference file '{output_ref}' was not updated on MSVC build as it has MSVC output mismatches allowed"
                        else:
                            shutil.copyfile(output_cur, output_ref)
                    else:
                        subcase_failed = True
                        subcase_messages.append(f"stdout:\n{output['stdout']}")
                        subcase_messages.append(f"stderr:\n{output['stderr']}")

                if not os.path.isfile(output_cur):
                    subcase_messages.append(f"Cannot find output file '{output_cur}'")
                    subcase_failed = True
                    files_found = False

                if not os.path.isfile(output_ref):
                    subcase_messages.append(f"Cannot find reference file '{output_ref}' for output file '{output_cur}'")
                    subcase_failed = True
                    files_found = False

                if files_found and not filecmp.cmp(output_cur, output_ref, shallow=False):
                    if cli_args.msvc and 'allowOutputMismatchOnMSVC' in testcase:
                        messages.append(f"    WARNING: allowed mismatch on MSVC build between output file '{output_cur}' and reference file '{output_ref}'")
                    else:
                        subcase_messages.append(f"Mismatch between output file '{output_cur}' and reference file '{output_ref}'")
                        subcase_failed = True


        # Handle subcase failure

        if subcase_failed:
            messages.append(f"    Subcase #{subcase_index + 1} FAILED:")
            messages.extend(["      " + msg for msg in subcase_messages])
            messages.append(f"      Run subcase with:")
            messages.append(f"        clitest.py -e {cli_args.executable_path} {cli_args.json_test_file} {ctx.cmdArgs()}")
            subcases_failed += 1
            failed = True

            if cmd_failed and cli_args.regen_golden:
                print("STOPPED: Reference data regeneration was requested but command execution failed.")
                break
        else:
            subcases_passed += 1

    for message in messages:
        print(message)

    for ref in ref_not_updated:
        print(ref_not_updated[ref])

    if not cli_args.regen_golden:
        print("Subcase summary:")
        print(f"  Passed:  {subcases_passed}")
        print(f"  Skipped: {subcases_skipped}")
        print(f"  Failed:  {subcases_failed}")
        print(f"  Total:   {len(subcases)}")

    if failed:
        exit(1)
