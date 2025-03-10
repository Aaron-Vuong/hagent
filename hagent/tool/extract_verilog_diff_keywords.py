#!/usr/bin/env python3
"""
Script: extract_verilog_diff_keywords.py

This script:
  1. Reads an original Verilog file and a fixed Verilog file.
  2. Generates a unified diff between the two.
  3. Extracts the main words (identifiers/keywords and important operators) from the diff while ignoring comments.
  4. Prints the extracted words.

Usage example:
  poetry run python3 extract_verilog_diff_keywords.py \
      --original_verilog ~/feri/original.v \
      --fixed_verilog ~/feri/fixed.v
"""

import argparse
import difflib
import re
from hagent.tool.fuzzy_grep import Fuzzy_grep


class Extract_verilog_diff_keywords:
    """
    Utility class to generate a unified diff and extract keywords from it.
    """

    @staticmethod
    def generate_diff(old_code: str, new_code: str) -> str:
        """
        Generate a unified diff string comparing old_code vs. new_code.

        :param old_code: Content of the original file.
        :param new_code: Content of the modified file.
        :return: A unified diff as a string.
        """
        old_lines = old_code.splitlines()
        new_lines = new_code.splitlines()
        diff_lines = difflib.unified_diff(
            old_lines, new_lines, fromfile='verilog_original.v', tofile='verilog_fixed.v', lineterm=''
        )
        return '\n'.join(diff_lines)

    @staticmethod
    def extract_variables(line: str) -> set:
        # Remove comments
        if '//' in line:
            line = line.split('//')[0]

        # Remove Verilog constants (e.g., 2'h1, 32'd5)
        line = re.sub(r"\d+'[bhd]\w+", '', line)

        # Remove curly braces and their contents
        line = re.sub(r'\{[^{}]*\}', '', line)

        # Handle signals with asterisks (like *GEN*1 or *signals*T_110)
        special_signals = re.findall(r'\*\w+\*(\w+)', line)

        # Find all potential variables - alphanumeric with underscores
        basic_vars = re.findall(r'[a-zA-Z_][\w_]*', line)
        
        # Additionally extract important operators such as |, &, ~
        operators = re.findall(r'[|&~]', line)

        # Combine all found tokens
        unique_vars = set(special_signals + basic_vars + operators)

        return unique_vars

    @staticmethod
    def get_words(diff_text: str) -> set:
        """
        Extract keywords from a unified diff. It scans changed lines (starting with '+' or '-' but ignoring file header lines)
        and also includes the line immediately preceding a changed line, extracting all alphanumeric words and important operators.
        Comments are removed before processing so that only meaningful code identifiers are retained.

        :param diff_text: The unified diff string.
        :return: A set of unique keywords.
        """
        keywords = set()
        lines = diff_text.splitlines()
        for i, line in enumerate(lines):
            if (line.startswith('+') and not line.startswith('+++')) or (line.startswith('-') and not line.startswith('---')):
                # Process current changed line (strip the diff marker)
                keywords.update(Extract_verilog_diff_keywords.extract_variables(line[1:]))
                # Also process the immediate previous line if it exists and is not a hunk header (e.g. @@ ... @@)
                if i > 0 and not lines[i-1].startswith('@@'):
                    prev_line = lines[i-1]
                    # For context lines (starting with a space), remove the leading space.
                    if prev_line.startswith(' '):
                        prev_line = prev_line[1:]
                    keywords.update(Extract_verilog_diff_keywords.extract_variables(prev_line))
        return keywords

    @staticmethod
    def get_user_variables(diff_text: str) -> list:
        """
        Similar to get_words, but remove autogenerated words
        """
        res = Extract_verilog_diff_keywords.get_words(diff_text)
        reserved_words = Fuzzy_grep.get_reserved_keywords('verilog')
        filtered_res = []
        for var_name in res:
            if not var_name.startswith('_T') and not var_name.startswith('_GEN') and var_name not in reserved_words:
                filtered_res.append(var_name)

        return filtered_res
