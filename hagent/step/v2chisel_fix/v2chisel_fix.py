#!/usr/bin/env python3
# See LICENSE for details

"""
# V2ChiselFix

**V2ChiselFix** is a step in the hardware design automation pipeline designed to refine Chisel code based on Logic Equivalence Check (LEC) results. It leverages a Language Model (LLM) to iteratively improve Chisel code when discrepancies are found between generated Verilog and fixed Verilog specifications.

## Usage

Run the pass using Poetry:

```bash
poetry run python3 hagent/step/v2chisel_fix/v2chisel_fix.py -o hagent/step/v2chisel_fix/out2.yaml hagent/step/v2chisel_pass1/out2.yaml

Process Overview
Reads 'chisel_pass1' data.
Calls LEC to verify if verilog_candidate is equivalent to verilog_fixed.
If LEC fails:
Attempt 1: Uses prompt3 for refinement.
Attempt 2: If prompt3 does not improve the code, uses prompt4 (which employs additional hints generated via Fuzzy_grep).
If prompt4 also fails to yield a fix, the test case is marked as a failure.
Returns final data with "chisel_fixed" in the YAML.
"""

import os
import re
import difflib
from hagent.core.step import Step
from hagent.core.llm_wrap import LLM_wrap
from hagent.core.llm_template import LLM_template

from hagent.tool.extract_code import Extract_code_verilog, Extract_code_chisel
from hagent.tool.equiv_check import Equiv_check
from hagent.tool.chisel2v import Chisel2v
from hagent.tool.chisel_diff_applier import ChiselDiffApplier
from hagent.tool.extract_verilog_diff_keywords import FuzzyGrepFilter
from hagent.tool.fuzzy_grep import Fuzzy_grep
from hagent.step.v2chisel_pass1.v2chisel_pass1 import V2Chisel_pass1

import subprocess
import tempfile
import os

def diff_code(text1: str, text2: str) -> str:
    """
    Create a diff of two text arguments using:
      diff -bBdNrw -U5
    """
    # Create temporary files to hold the texts
    with tempfile.NamedTemporaryFile('w+', delete=False) as f1, \
         tempfile.NamedTemporaryFile('w+', delete=False) as f2:
        f1.write(text1)
        f1.flush()  # Ensure data is written to disk
        f2.write(text2)
        f2.flush()
        file1_name = f1.name
        file2_name = f2.name

    try:
        # Execute the diff command with the given options
        result = subprocess.run(
            ['diff', '-bBdNrw', '-U5', file1_name, file2_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return result.stdout
    finally:
        # Clean up temporary files to avoid resource leakage
        os.unlink(file1_name)
        os.unlink(file2_name)

class V2chisel_fix(Step):
    def setup(self):
        self.overwrite_conf = {}
        conf_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'v2chisel_base_conf.yaml')
        if not os.path.exists(conf_file):
            self.error(f'Prompt file not found: {conf_file}')

        super().setup()  # Reads self.input_data from YAML

        # FIXME: rename all chisel_pass1 to v2chisel_fix for yaml files
        if 'chisel_pass1' not in self.input_data:
            self.error("Missing 'chisel_pass1' in input YAML (did you run v2chisel_pass1 first?)")

        self.verilog_fixed_str = self.input_data.get('verilog_fixed', '')
        self.verilog_original_str = self.input_data.get('verilog_original', '')
        self.verilog_diff_str = diff_code(self.verilog_original_str, self.verilog_fixed_str)
        
        self.template_config = LLM_template(conf_file)
        # llm_args = self.input_data['llm'] 
        llm_args = self.template_config.template_dict.get('v2chisel_pass1', {}).get('llm', {})

        self.refine_llm = LLM_wrap(name='v2chisel_fix', conf_file=conf_file, log_file="v2chisel_fix.log", overwrite_conf=llm_args)

        self.verilog_extractor = Extract_code_verilog()
        self.chisel_extractor = Extract_code_chisel()

        self.setup_called = True

    def run(self, data):
        """
        1) Reads 'chisel_pass1' data.
        2) Checks if verilog_candidate is equivalent to verilog_fixed.
        3) If LEC fails, performs refinement in two phases:
           - Phase 1: Uses prompt3 (up to 2 attempts) to generate a Chisel diff.
           - Phase 2: If prompt3 fails, uses prompt4 (up to 2 attempts) similarly.
           In each attempt, the generated diff is applied to the original Chisel code,
           the updated code is compiled (Chisel2v) and then checked via LEC.
        4) Returns final data with "chisel_fixed" in the YAML.
        """
        result = data.copy()
        pass1_info = data['chisel_pass1']
        chisel_changed = pass1_info.get('chisel_changed', '')
        verilog_candidate = pass1_info.get('verilog_candidate', '')
        was_valid = pass1_info.get('was_valid', False)
        # original_chisel = pass1_info.get('chisel_original', '')
        chisel_original = data.get('chisel_original', '')
        self.chisel_original = chisel_original
        self.chisel_subset = pass1_info.get('chisel_subset', chisel_changed)
        result['chisel_fixed'] = {
            'original_chisel': chisel_original,
            'refined_chisel': chisel_changed,  # to be updated upon successful refinement
            'chisel_diff': "",                 # the diff generated by the LLM
            'equiv_passed': False,
        }

        print(f'[INFO] Starting initial LEC check. was_valid={was_valid}')
        verilog_fixed = data.get('verilog_fixed', '')
        if not verilog_fixed.strip():
            print("[WARN] No 'verilog_fixed' provided. Skipping initial LEC check.")
            initial_equiv = False
            lec_error = 'No verilog_fixed provided'
        else:
            initial_equiv, lec_error = self._check_equivalence(verilog_fixed, verilog_candidate)

        if initial_equiv:
            print('[INFO] Designs are already equivalent; no refinement needed.')
            result['chisel_fixed']['refined_chisel'] = chisel_original
            result['chisel_fixed']['equiv_passed'] = True
            result['chisel_fixed']['chisel_diff'] = ""
            return result

        max_prompt3_attempts = 2
        max_prompt4_attempts = 2
        chisel_updated_final = None
        was_valid_refinement = False
        last_error_msg = ''
        candidate_diff_final = ""

        # Phase 1: Prompt3 attempts
        for attempt in range(1, max_prompt3_attempts + 1):
            print(f'[INFO] Prompt3 Attempt {attempt}')
            candidate_diff = self._refine_chisel_code(chisel_changed, lec_error, attempt)
            if not candidate_diff or not candidate_diff.strip():
                print('[WARN] Prompt3 LLM returned an empty diff.')
                continue
            print("Generated diff from prompt3:")
            print(candidate_diff)
            applier = ChiselDiffApplier()
            chisel_updated = applier.apply_diff(candidate_diff, self.chisel_original)
            print(f"===== FINAL CHISEL CODE AFTER DIFF APPLIER (Prompt3 Attempt {attempt}) =====")
            print(chisel_updated)
            is_valid, verilog_candidate_temp, error_msg = self._run_chisel2v(chisel_updated)
            if not is_valid:
                print(f'[WARN] Compilation failed on Prompt3 Attempt {attempt}: {error_msg}')
                last_error_msg = error_msg or 'Unknown compile error'
                continue
            is_equiv, lec_error = self._check_equivalence(verilog_fixed, verilog_candidate_temp)
            if is_equiv:
                print(f'[INFO] LEC passed on Prompt3 Attempt {attempt}.')
                chisel_updated_final = chisel_updated
                was_valid_refinement = True
                candidate_diff_final = candidate_diff
                break
            else:
                print(f'[WARN] LEC failed on Prompt3 Attempt {attempt}: {lec_error}')
                last_error_msg = lec_error

        # Phase 2: Prompt4 attempts (if needed)
        if not was_valid_refinement:
            for attempt in range(1, max_prompt4_attempts + 1):
                print(f'[INFO] Prompt4 Attempt {attempt}')
                candidate_diff = self._refine_chisel_code_with_prompt4(chisel_changed, lec_error, attempt)
                if not candidate_diff or not candidate_diff.strip():
                    print('[WARN] Prompt4 LLM returned an empty diff.')
                    continue
                print("Generated diff from prompt4:")
                print(candidate_diff)
                applier = ChiselDiffApplier()
                chisel_updated = applier.apply_diff(candidate_diff, chisel_original)
                print(f"===== FINAL CHISEL CODE AFTER DIFF APPLIER (Prompt4 Attempt {attempt}) =====")
                print(chisel_updated)
                is_valid, verilog_candidate_temp, error_msg = self._run_chisel2v(chisel_updated)
                if not is_valid:
                    print(f'[WARN] Compilation failed on Prompt4 Attempt {attempt}: {error_msg}')
                    last_error_msg = error_msg or 'Unknown compile error'
                    continue
                is_equiv, lec_error = self._check_equivalence(verilog_fixed, verilog_candidate_temp)
                if is_equiv:
                    print(f'[INFO] LEC passed on Prompt4 Attempt {attempt}.')
                    chisel_updated_final = chisel_updated
                    was_valid_refinement = True
                    candidate_diff_final = candidate_diff
                    break
                else:
                    print(f'[WARN] LEC failed on Prompt4 Attempt {attempt}: {lec_error}')
                    last_error_msg = lec_error

        if not was_valid_refinement:
            print(f'[ERROR] All refinement attempts failed. Last error: {last_error_msg}')
            result['chisel_fixed']['refined_chisel'] = chisel_original
            result['chisel_fixed']['equiv_passed'] = False
            result['chisel_fixed']['chisel_diff'] = candidate_diff_final if candidate_diff_final else ""
            return result
        else:
            result['chisel_fixed']['refined_chisel'] = chisel_updated_final
            result['chisel_fixed']['equiv_passed'] = True
            result['chisel_fixed']['chisel_diff'] = candidate_diff_final
            print("[INFO] Refinement successful. 'chisel_fixed' written to output YAML.")
            return result

    def _generate_diff(self, old_code: str, new_code: str) -> str:
        """
        Generate a unified diff string comparing old_code vs. new_code.
        """
        old_lines = old_code.splitlines()
        new_lines = new_code.splitlines()
        diff_lines = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile='verilog_original.v',
            tofile='verilog_fixed.v',
            lineterm=''
        )
        return '\n'.join(diff_lines)

    def _check_equivalence(self, gold_code: str, reference_code: str):
        if not gold_code.strip() or not reference_code.strip():
            return (False, 'Missing code for equivalence check')
        eq_checker = Equiv_check()
        if not eq_checker.setup():
            err = eq_checker.get_error() or 'Yosys not found'
            print(f'[ERROR] Equiv_check setup failed: {err}')
            return (False, err)
        try:
            result = eq_checker.check_equivalence(gold_code, reference_code)
            if result is True:
                print('[INFO] LEC check: Designs are equivalent!')
                return (True, None)
            elif result is False:
                err = eq_checker.get_error()
                cex_info = eq_checker.get_counterexample()
                print('[WARN] LEC check: Designs are NOT equivalent.')
                
                if err:
                    print(f'[ERROR] LEC error: {err}')
                if cex_info:
                    print(f'[DEBUG] LEC Counterexample info: {cex_info}')
                return (False, cex_info or 'LEC mismatch')
            else:
                err = eq_checker.get_error() or 'LEC result is None/inconclusive'
                print(f'[ERROR] LEC result is None. {err}')
                return (False, err)
        except Exception as e:
            print(f'[ERROR] LEC threw exception: {e}')
            return (False, str(e))

    def _refine_chisel_code(self, current_code: str, lec_error: str, attempt: int):
        """
        Uses prompt3 for LLM refinement to generate a Chisel diff.
        The LLM (via prompt3.yaml) is instructed to output only the diff in unified diff format.
        """
        prompt_dict = {
            'chisel_original': self.chisel_original,
            'chisel_subset': self.chisel_subset,
            'lec_output': lec_error or 'LEC failed',
            'verilog_diff': self.verilog_diff_str,
        }
        full_config = self.template_config.template_dict.get(self.refine_llm.name.lower(), {})
        prompt_template = LLM_template(full_config["prompt3"])
        self.refine_llm.chat_template = prompt_template
        formatted_prompt = self.refine_llm.chat_template.format(prompt_dict)
        print('\n================ LLM QUERY (prompt3, attempt {}) ================'.format(attempt))
        for chunk in formatted_prompt:
            print("Role: {}".format(chunk.get('role', '<no role>')))
            print("Content:")
            print(chunk.get('content', '<no content>'))
            print("------------------------------------------------")
        answers = self.refine_llm.inference(prompt_dict, prompt_index="prompt3", n=1)
        if not answers:
            print('\n=== LLM RESPONSE: EMPTY ===\n')
            last_error_msg = 'LLM gave empty response'

        print('\n================ LLM RESPONSE ================')
        print(answers[0])
        print('==============================================')

        for txt in answers:
            code = self.chisel_extractor.parse(txt)
            if code:
                return code

        return ""

    def _refine_chisel_code_with_prompt4(self, current_code: str, lec_error: str, attempt: int):
        """
        Uses prompt4 for LLM refinement to generate a Chisel diff.
        The LLM (via prompt4.yaml) is instructed to output only the diff in unified diff format.
        """
        v2c_pass1 = V2Chisel_pass1()
        new_hints = v2c_pass1._extract_chisel_subset(self.chisel_original, self.verilog_diff_str, threshold_override=50)

        prompt_dict = {
            'lec_output': lec_error or 'LEC failed',
            'verilog_diff': self.verilog_diff_str,
            #'chisel_diff': chisel_diff_placeholder,
            'new_hints': new_hints,
        }
        print('\n================ LLM QUERY (prompt4, attempt {}) ================'.format(attempt))
        for key, value in prompt_dict.items():
            print(f"{key}: {value}")
        print('==============================================')
        answers = self.refine_llm.inference(prompt_dict, 'prompt4', n=1)
        if not answers:
            print('\n=== LLM RESPONSE: EMPTY ===\n')
            return ""

        print('\n================ LLM RESPONSE (prompt4) ================')
        print(answers[0])
        print('==============================================')
        for txt in answers:
            code = self.chisel_extractor.parse(txt)
            if code:
                return code

        return ""

    def _run_chisel2v(self, chisel_code: str):
        """
        Runs the Chisel2v tool to generate Verilog from the given Chisel code and checks its validity.
        """
        if not chisel_code.strip():
            return (False, None, 'Chisel snippet is empty')
        c2v = Chisel2v()
        if not c2v.setup():
            return (False, None, 'chisel2v setup failed: ' + c2v.error_message)
        module_name = self._find_chisel_classname(chisel_code)
        if not module_name:
            module_name = 'MyModule'
        try:
            verilog_out = c2v.generate_verilog(chisel_code, module_name)
            if 'module' not in verilog_out:
                return (False, None, "Generated Verilog missing 'module' keyword.")
            return (True, verilog_out, '')
        except Exception as e:
            return (False, None, str(e))

    def _find_chisel_classname(self, chisel_code: str) -> str:
        match = re.search(r'class\s+([A-Za-z0-9_]+)\s+extends\s+Module', chisel_code)
        if match:
            return match.group(1)
        return 'MyModule'

    def _strip_markdown_fences(self, code_str: str) -> str:
        code_str = re.sub(r'```[a-zA-Z]*\n?', '', code_str)
        code_str = re.sub(r'\n?```', '', code_str)
        return code_str.strip()


if __name__ == '__main__':  # pragma: no cover
    step = V2chisel_fix()
    step.parse_arguments()
    step.setup()
    step.step()
